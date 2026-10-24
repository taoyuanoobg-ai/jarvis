#!/usr/bin/env python3
"""One-shot guarded repair for Lashes bilingual Intake verifier/recovery.

Purpose:
- Read execution 194 to capture the real HTTP node response shape.
- Patch only the Intake workflow.
- Make Polylang language/link verifiers accept both direct JSON and {body:{...}}.
- Add a recovery guard so a prior failed file with an already-created WP draft is
  reused instead of calling the Writer again.
- Backup before mutation, verify after PUT, rollback on any failure.
- Confirm Lashes/Additives workflows are unchanged.

This tool does NOT touch Google Drive or WordPress directly.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:5678/api/v1"
SECRET = "/opt/taoyuan/secrets/n8n-api-key"
INTAKE_ID = "SAjlpjx6cmwkfklv"
INTAKE_NAME = "Drive Content Intake V1"
LASHES_ID = "lashesProdDraftV1"
LASHES_NAME = "Lashes - Production Draft VPS"
ADDITIVES_ID = "additivesDraftV1"
ADDITIVES_NAME = "Additives - Production Draft VPS"
WRITABLE = ("name", "nodes", "connections", "settings")

VERIFY_LANGUAGE_JS = r"""
const base = $('Verify Draft Writer Result').first().json;
const raw = $input.first().json ?? {};
const resp = raw && typeof raw.body === 'object' && raw.body !== null ? raw.body : raw;
const expectedLanguage = String(base.language || base.article?.language || '').toLowerCase();
const expectedPostId = Number(base.writer_result?.post_id || 0);
const ok = resp.ok === true && resp.verified === true && Number(resp.post_id) === expectedPostId && String(resp.language || '').toLowerCase() === expectedLanguage;
return [{json:{...base,status:ok ? base.status : 'failed',error_code:ok ? base.error_code : 'POLYLANG_LANGUAGE_VERIFY_FAILED',failure_reason:ok ? base.failure_reason : String(resp.error?.message || resp.message || raw.error?.message || raw.message || 'Polylang set-language verification failed'),polylang_language_verified:ok}}];
""".strip()

VERIFY_LINK_JS = r"""
const base = $('Persist Intake State').first().json;
const unwrap = (value) => value && typeof value.body === 'object' && value.body !== null ? value.body : (value ?? {});
const linkResp = unwrap($('Bilingual Link Translations').first().json);
const enResp = unwrap($('Bilingual Verify EN Translation').first().json);
const ptResp = unwrap($input.first().json);
const enId = Number(base.en_post_id || 0);
const ptId = Number(base.pt_post_id || 0);
const enTranslations = enResp.translations ?? {};
const ptTranslations = ptResp.translations ?? {};
const verified = Boolean(linkResp.ok === true && linkResp.verified === true && Number(linkResp.translations?.en || 0) === enId && Number(linkResp.translations?.pt || 0) === ptId && enResp.ok === true && String(enResp.language || '') === 'en' && Number(enTranslations.en || 0) === enId && Number(enTranslations.pt || 0) === ptId && ptResp.ok === true && String(ptResp.language || '') === 'pt' && Number(ptTranslations.en || 0) === enId && Number(ptTranslations.pt || 0) === ptId);
const state = $getWorkflowStaticData('global');
state.translation_groups ??= {};
if (base.translation_group_key && state.translation_groups[base.translation_group_key]) { const group = state.translation_groups[base.translation_group_key]; group.linked = verified; group.link_verified = verified; group.updated_at = new Date().toISOString(); }
if (verified) return [{json:{...base,status:'completed',linked:true,link_verified:true,needs_translation_link:false}}];
state.files ??= {};
const prior = state.files[base.file_id] ?? {};
const attempts = Number(prior.attempts ?? 0) + 1;
const status = attempts >= 3 ? 'manual_review' : 'failed';
state.files[base.file_id] = {...prior,status,attempts,last_error:'Polylang translation link verification failed',updated_at:new Date().toISOString()};
return [{json:{...base,status,attempts,linked:false,link_verified:false,needs_translation_link:false,error:'Polylang translation link verification failed'}}];
""".strip()

PREPARE_DEDUPE_JS = r"""
const state = $getWorkflowStaticData('global');
state.translation_groups ??= {};
state.files ??= {};
return items.map(item => {
  const r = item.json ?? {};
  if (r.status !== 'validated') return {json:{...r,bilingual_route:'write',bilingual_dedupe_hit:false}};
  const article = r.article ?? {};
  const site = String(article.site || '').toLowerCase();
  const language = String(article.language || '').toLowerCase();
  const groupId = typeof article.translation_group_id === 'string' ? article.translation_group_id.trim() : '';
  if (site !== 'pingdulashes.com' || !groupId || !['en','pt'].includes(language)) return {json:{...r,bilingual_route:'write',bilingual_dedupe_hit:false}};
  const groupKey = `${site}::${groupId}`;
  const group = state.translation_groups[groupKey] ?? {};
  const existingPostId = Number(group[`${language}_post_id`] ?? 0);
  if (existingPostId > 0) {
    return {json:{file_id:r.file_id,file_name:r.file_name,version:r.version,checksum:r.checksum,article,status:'completed',error_code:null,failure_reason:null,writer_result:{success:true,post_id:existingPostId,status:'draft',dedupe_hit:true,language,translation_group_id:groupId},requested_publish:false,completed_at:new Date().toISOString(),bilingual_route:'skip',bilingual_dedupe_hit:true,translation_group_id:groupId,language}};
  }
  const prior = state.files[r.file_id] ?? {};
  const priorPostId = Number(prior.wordpress_post_id || 0);
  const priorGroup = String(prior.translation_group_id || '');
  const priorLanguage = String(prior.language || '').toLowerCase();
  const priorArticle = String(prior.article_id || '');
  const sameArticle = !priorArticle || priorArticle === String(article.article_id || '');
  const recoverableError = String(prior.last_error || '').includes('Polylang set-language verification failed');
  if (priorPostId > 0 && priorGroup === groupId && priorLanguage === language && sameArticle && recoverableError) {
    return {json:{file_id:r.file_id,file_name:r.file_name,version:r.version,checksum:r.checksum,article,status:'completed',error_code:null,failure_reason:null,writer_result:{success:true,post_id:priorPostId,status:String(prior.wordpress_status || 'draft'),dedupe_hit:true,recovered_existing_draft:true,language,translation_group_id:groupId},requested_publish:false,completed_at:new Date().toISOString(),bilingual_route:'skip',bilingual_dedupe_hit:true,recovered_existing_draft:true,translation_group_id:groupId,language}};
  }
  return {json:{...r,bilingual_route:'write',bilingual_dedupe_hit:false,translation_group_id:groupId,language}};
});
""".strip()


def load_key():
    value = Path(SECRET).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("empty n8n api key")
    return value


def api(method, path, token, payload=None, query=None):
    url = BASE + path
    if query:
        url += "?" + urlencode(query)
    data = None
    headers = {"X-N8N-API-KEY": token, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else None
        except Exception:
            body = {"raw": raw[:1500]}
        return exc.code, body


def get_workflow(token, workflow_id, expected_name):
    status, body = api("GET", f"/workflows/{workflow_id}", token)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET {workflow_id} failed HTTP {status}")
    if str(body.get("id")) != workflow_id or body.get("name") != expected_name:
        raise RuntimeError(f"workflow identity mismatch: {workflow_id}")
    return body


def writable(obj):
    return {k: obj[k] for k in WRITABLE}


def fingerprint(obj):
    raw = json.dumps(writable(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def node_map(wf):
    return {str(n.get("name")): n for n in wf.get("nodes", []) if isinstance(n, dict)}


def first_json(run):
    main = (run.get("data") or {}).get("main") if isinstance(run, dict) else None
    if not isinstance(main, list):
        return None
    for branch in main:
        if isinstance(branch, list):
            for item in branch:
                if isinstance(item, dict) and isinstance(item.get("json"), dict):
                    return item["json"]
    return None


def rollback(token, before):
    status, _ = api("PUT", f"/workflows/{INTAKE_ID}", token, writable(before))
    if status not in (200, 201):
        return False
    try:
        return fingerprint(get_workflow(token, INTAKE_ID, INTAKE_NAME)) == fingerprint(before)
    except Exception:
        return False


def main():
    token = load_key()
    before = get_workflow(token, INTAKE_ID, INTAKE_NAME)
    lashes_before = get_workflow(token, LASHES_ID, LASHES_NAME)
    additives_before = get_workflow(token, ADDITIVES_ID, ADDITIVES_NAME)

    exec_status, execution = api("GET", "/executions/194", token, query={"includeData":"true"})
    if exec_status != 200 or not isinstance(execution, dict):
        raise RuntimeError(f"execution 194 read failed HTTP {exec_status}")
    run_data = (((execution.get("data") or {}).get("resultData") or {}).get("runData") or {})
    set_runs = run_data.get("Bilingual Set Language") or []
    set_raw = first_json(set_runs[-1]) if set_runs else None

    nodes = node_map(before)
    required = ["Bilingual Prepare Dedupe","Bilingual Verify Language","Bilingual Verify Translation Link"]
    missing = [name for name in required if name not in nodes]
    if missing:
        raise RuntimeError(f"missing repair nodes: {missing}")

    candidate = copy.deepcopy(before)
    cnodes = node_map(candidate)
    cnodes["Bilingual Prepare Dedupe"]["parameters"]["jsCode"] = PREPARE_DEDUPE_JS
    cnodes["Bilingual Verify Language"]["parameters"]["jsCode"] = VERIFY_LANGUAGE_JS
    cnodes["Bilingual Verify Translation Link"]["parameters"]["jsCode"] = VERIFY_LINK_JS

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = Path(f"/opt/taoyuan/backups/n8n/lashes-bilingual-20261024/final-repair/{stamp}")
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / "intake-before.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "intake-candidate.json").write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "execution-194-set-language.json").write_text(json.dumps(set_raw, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "ok": False,
        "backup_dir": str(outdir),
        "execution_194_set_language_raw": set_raw,
        "patched_nodes": required,
        "intake_before_fingerprint": fingerprint(before),
        "intake_after_fingerprint": None,
        "lashes_unchanged": None,
        "additives_unchanged": None,
        "rollback": {"attempted": False, "ok": None},
    }
    try:
        status, _ = api("PUT", f"/workflows/{INTAKE_ID}", token, writable(candidate))
        if status not in (200, 201):
            raise RuntimeError(f"Intake PUT failed HTTP {status}")
        after = get_workflow(token, INTAKE_ID, INTAKE_NAME)
        if fingerprint(after) != fingerprint(candidate):
            raise RuntimeError("Intake GET verify fingerprint mismatch")
        result["intake_after_fingerprint"] = fingerprint(after)
        result["lashes_unchanged"] = fingerprint(get_workflow(token, LASHES_ID, LASHES_NAME)) == fingerprint(lashes_before)
        result["additives_unchanged"] = fingerprint(get_workflow(token, ADDITIVES_ID, ADDITIVES_NAME)) == fingerprint(additives_before)
        if not result["lashes_unchanged"] or not result["additives_unchanged"]:
            raise RuntimeError("non-target workflow changed unexpectedly")
        result["ok"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        result["error"] = str(exc)
        result["rollback"]["attempted"] = True
        result["rollback"]["ok"] = rollback(token, before)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
