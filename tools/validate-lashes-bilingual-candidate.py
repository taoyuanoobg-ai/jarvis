#!/usr/bin/env python3
"""Static validator for Lashes EN/PT bilingual n8n candidates.

No API calls and no writes to n8n/WordPress. It compares original snapshots with
candidate files, validates identities, exact safety anchors, graph rewiring,
bridge configuration, draft-only guarantees, and JavaScript syntax.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

INTAKE_NAME = "Drive Content Intake V1"
LASHES_NAME = "Lashes - Production Draft"
EXPECTED_UNTOUCHED = {
    "Verify Additives Draft Writer Result": "2cf1532d5b6a537442b731c7d60489a0b8f7954b6f7d0f96e7c319f7f2e6448e",
    "VPS Draft Guard": "74226ef33ccc1d6352300c21b7d9193e3854ab1e2ecd3b9d0d909ed929943013",
    "先创建WordPress草稿": "632e918e4bf12f76f7dd044de680ca83a666ab9778e9012bd43ece084dfac0a7",
}
NEW_INTAKE_NODES = [
    "Bilingual Prepare Dedupe", "Bilingual Writer Decision", "Bilingual Language Decision",
    "Bilingual Set Language", "Bilingual Verify Language", "Bilingual Link Decision",
    "Bilingual Link Translations", "Bilingual Verify EN Translation",
    "Bilingual Verify PT Translation", "Bilingual Verify Translation Link",
]


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def sanitize_creds(value: Any) -> Any:
    if not isinstance(value, dict): return value
    out = {}
    for k, v in value.items():
        out[k] = {"id": v.get("id"), "name": v.get("name")} if isinstance(v, dict) else "<reference>"
    return out


def node_fp(node: dict) -> str:
    return sha({"name":node.get("name"),"type":node.get("type"),"parameters":node.get("parameters", {}),"credentials":sanitize_creds(node.get("credentials", {}))})


def unwrap(raw: Any) -> dict:
    if isinstance(raw, dict) and isinstance(raw.get("body"), dict) and "nodes" in raw["body"]: return copy.deepcopy(raw["body"])
    if isinstance(raw, dict) and "nodes" in raw: return copy.deepcopy(raw)
    raise RuntimeError("not an n8n workflow")


def load(path: str) -> dict:
    return unwrap(json.loads(Path(path).read_text(encoding="utf-8")))


def nmap(wf: dict) -> dict[str, dict]:
    return {str(n.get("name")): n for n in wf.get("nodes", []) if isinstance(n, dict)}


def writable_fp(wf: dict) -> str:
    return sha({k:wf[k] for k in ("name","nodes","connections","settings")})


def main_branch(wf: dict, source: str, index: int = 0) -> list[dict]:
    main = wf.get("connections", {}).get(source, {}).get("main", [])
    if not isinstance(main, list) or index >= len(main) or not isinstance(main[index], list): return []
    return main[index]


def target_names(edges: list[dict]) -> list[str]:
    return [str(x.get("node")) for x in edges if isinstance(x, dict)]


def check_no_dangling(wf: dict) -> list[str]:
    names = set(nmap(wf)); errors = []
    for src, channels in wf.get("connections", {}).items():
        if src not in names: errors.append(f"connection source missing node: {src}")
        if not isinstance(channels, dict): continue
        for channel, branches in channels.items():
            if not isinstance(branches, list): continue
            for branch in branches:
                if not isinstance(branch, list): continue
                for edge in branch:
                    if isinstance(edge, dict) and edge.get("node") not in names: errors.append(f"{src}/{channel} -> missing {edge.get('node')}")
    return errors


def credential_refs(wf: dict) -> dict[str, Any]:
    return {name:sanitize_creds(node.get("credentials", {})) for name,node in nmap(wf).items()}


def js_check(code: str) -> tuple[bool, str]:
    wrapped = "async function __n8n_wrapper__(){\n" + code + "\n}\n"
    if shutil.which("node"):
        p = subprocess.run(["node","--check","-"], input=wrapped, text=True, capture_output=True)
        return p.returncode == 0, (p.stderr or p.stdout).strip()
    if shutil.which("docker"):
        p = subprocess.run(["docker","exec","-i","taoyuan-n8n","node","--check","-"], input=wrapped, text=True, capture_output=True)
        return p.returncode == 0, (p.stderr or p.stdout).strip()
    return False, "neither node nor docker is available for JavaScript syntax checking"


def add(checks: list[dict], name: str, ok: bool, detail: str = "") -> None:
    checks.append({"check":name,"ok":bool(ok),"detail":detail})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake-before", required=True); ap.add_argument("--lashes-before", required=True)
    ap.add_argument("--intake-candidate", required=True); ap.add_argument("--lashes-candidate", required=True)
    ap.add_argument("--output", required=True); args = ap.parse_args()
    ib, lb, ic, lc = load(args.intake_before), load(args.lashes_before), load(args.intake_candidate), load(args.lashes_candidate)
    checks = []

    add(checks,"intake_name_unchanged", ib.get("name") == INTAKE_NAME == ic.get("name")); add(checks,"lashes_name_unchanged", lb.get("name") == LASHES_NAME == lc.get("name"))
    add(checks,"intake_id_unchanged", ib.get("id") == ic.get("id"), f"{ib.get('id')} -> {ic.get('id')}"); add(checks,"lashes_id_unchanged", lb.get("id") == lc.get("id"), f"{lb.get('id')} -> {lc.get('id')}")
    add(checks,"intake_active_preserved", ib.get("active") == ic.get("active")); add(checks,"lashes_active_preserved", lb.get("active") == lc.get("active"))
    add(checks,"intake_settings_preserved", ib.get("settings") == ic.get("settings")); add(checks,"lashes_settings_preserved", lb.get("settings") == lc.get("settings"))

    ibm, lbm, icm, lcm = nmap(ib), nmap(lb), nmap(ic), nmap(lc)
    add(checks,"intake_original_nodes_preserved", set(ibm).issubset(icm), str(sorted(set(ibm)-set(icm)))); add(checks,"lashes_original_nodes_preserved", set(lbm).issubset(lcm), str(sorted(set(lbm)-set(lcm))))
    add(checks,"intake_expected_new_node_count", len(icm) == len(ibm)+len(NEW_INTAKE_NODES), f"{len(ibm)} -> {len(icm)}"); add(checks,"lashes_node_count_unchanged", len(lcm) == len(lbm), f"{len(lbm)} -> {len(lcm)}")
    add(checks,"all_bilingual_nodes_present", all(x in icm for x in NEW_INTAKE_NODES))

    before_icreds, after_icreds = credential_refs(ib), credential_refs(ic); before_lcreds, after_lcreds = credential_refs(lb), credential_refs(lc)
    add(checks,"intake_original_credentials_preserved", all(after_icreds.get(k)==v for k,v in before_icreds.items())); add(checks,"lashes_original_credentials_preserved", all(after_lcreds.get(k)==v for k,v in before_lcreds.items()))

    for name, fp in EXPECTED_UNTOUCHED.items():
        wfmap = icm if name == "Verify Additives Draft Writer Result" else lcm
        actual = node_fp(wfmap.get(name, {})) if name in wfmap else ""
        add(checks,f"untouched_anchor:{name}", actual == fp, actual)

    add(checks,"lashes_connections_unchanged", lb.get("connections") == lc.get("connections"))
    orig_validate_targets = target_names(main_branch(ib,"Validate Article JSON")); writer_targets = target_names(main_branch(ic,"Bilingual Writer Decision",0)); dedupe_targets = target_names(main_branch(ic,"Bilingual Writer Decision",1))
    add(checks,"legacy_writer_path_preserved", writer_targets == orig_validate_targets, f"{writer_targets} vs {orig_validate_targets}"); add(checks,"dedupe_skip_routes_to_persist", dedupe_targets == ["Persist Intake State"], str(dedupe_targets))
    orig_verify_targets = target_names(main_branch(ib,"Verify Draft Writer Result")); lang_skip_targets = target_names(main_branch(ic,"Bilingual Language Decision",1)); add(checks,"language_skip_preserves_legacy_path", lang_skip_targets == orig_verify_targets)
    orig_persist_targets = target_names(main_branch(ib,"Persist Intake State")); link_skip_targets = target_names(main_branch(ic,"Bilingual Link Decision",1)); link_done_targets = target_names(main_branch(ic,"Bilingual Verify Translation Link"))
    add(checks,"link_skip_preserves_status_path", link_skip_targets == orig_persist_targets); add(checks,"link_verify_returns_status_path", link_done_targets == orig_persist_targets)
    dangling = check_no_dangling(ic) + check_no_dangling(lc); add(checks,"no_dangling_connections", not dangling, "; ".join(dangling))

    validate_js = icm["Validate Article JSON"]["parameters"].get("jsCode", ""); prepare_js = icm["Bilingual Prepare Dedupe"]["parameters"].get("jsCode", ""); persist_js = icm["Persist Intake State"]["parameters"].get("jsCode", ""); normalizer_js = lcm["校验并标准化输入"]["parameters"].get("jsCode", "")
    add(checks,"translation_group_optional", "translation_group_id" in validate_js and "translation_group_id','" not in validate_js)
    add(checks,"language_enum_en_pt", "['en','pt']" in validate_js and "['en','pt']" in normalizer_js)
    add(checks,"group_language_dedupe_present", "state.translation_groups" in prepare_js and "existingPostId" in prepare_js)
    add(checks,"group_state_persisted", "state.translation_groups" in persist_js and "en_post_id" in persist_js and "pt_post_id" in persist_js)
    add(checks,"en_category_default", "Lash Guide" in validate_js and "Lash Guide" in normalizer_js); add(checks,"pt_category_default", "Guia de Cílios" in validate_js and "Guia de Cílios" in normalizer_js)

    create_json = str(lcm["先创建WordPress草稿"].get("parameters", {}).get("jsonBody", "")); guard_js = str(lcm["VPS Draft Guard"].get("parameters", {}).get("jsCode", ""))
    add(checks,"wp_create_is_draft", "status: 'draft'" in create_json or '"draft"' in create_json); add(checks,"draft_guard_forces_draft", "status: 'draft'" in guard_js and "publish: false" in guard_js)

    set_node, link_node = icm["Bilingual Set Language"], icm["Bilingual Link Translations"]; en_node, pt_node = icm["Bilingual Verify EN Translation"], icm["Bilingual Verify PT Translation"]
    add(checks,"set_language_method", set_node["parameters"].get("method") == "POST"); add(checks,"set_language_endpoint", set_node["parameters"].get("url") == "https://pingdulashes.com/wp-json/pingdu-automation/v1/set-language")
    set_body = str(set_node["parameters"].get("jsonBody", "")); add(checks,"set_language_body_contract", "post_id" in set_body and "language" in set_body and " lang:" not in set_body)
    add(checks,"link_method", link_node["parameters"].get("method") == "POST"); add(checks,"link_endpoint", link_node["parameters"].get("url") == "https://pingdulashes.com/wp-json/pingdu-automation/v1/link-translations")
    link_body = str(link_node["parameters"].get("jsonBody", "")); add(checks,"link_body_contract", "en_post_id" in link_body and "pt_post_id" in link_body)
    add(checks,"verify_en_endpoint", "/translations/" in str(en_node["parameters"].get("url", ""))); add(checks,"verify_pt_endpoint", "/translations/" in str(pt_node["parameters"].get("url", "")))
    wp_ref = sanitize_creds(lcm["先创建WordPress草稿"].get("credentials", {}))
    for node_name in ("Bilingual Set Language","Bilingual Link Translations","Bilingual Verify EN Translation","Bilingual Verify PT Translation"):
        add(checks,f"credential_reuse:{node_name}", sanitize_creds(icm[node_name].get("credentials", {})) == wp_ref)

    js_nodes = [(icm,"Validate Article JSON"),(icm,"Bilingual Prepare Dedupe"),(icm,"Verify Draft Writer Result"),(icm,"Bilingual Verify Language"),(icm,"Persist Intake State"),(icm,"Bilingual Verify Translation Link"),(lcm,"校验并标准化输入")]
    for wfmap, name in js_nodes:
        ok, detail = js_check(str(wfmap[name].get("parameters", {}).get("jsCode", ""))); add(checks,f"js_syntax:{name}",ok,detail)

    changed_lashes = []
    for name, before_node in lbm.items():
        after_node = lcm.get(name)
        if after_node is not None and canonical(before_node) != canonical(after_node): changed_lashes.append(name)
    add(checks,"lashes_only_normalizer_changed", changed_lashes == ["校验并标准化输入"], str(changed_lashes))

    failed = [c for c in checks if not c["ok"]]
    result = {"ok":not failed,"status":"PASS" if not failed else "FAIL","checks_total":len(checks),"checks_failed":len(failed),"failed":failed,"intake_before_fingerprint":writable_fp(ib),"intake_candidate_fingerprint":writable_fp(ic),"lashes_before_fingerprint":writable_fp(lb),"lashes_candidate_fingerprint":writable_fp(lc),"intake_nodes_before":len(ibm),"intake_nodes_after":len(icm),"lashes_nodes_before":len(lbm),"lashes_nodes_after":len(lcm),"checks":checks}
    out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ok"] else 2


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok":False,"status":"ERROR","error":str(exc)}, ensure_ascii=False)); raise SystemExit(1)
