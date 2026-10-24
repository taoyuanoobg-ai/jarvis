#!/usr/bin/env python3
"""Atomically deploy validated Lashes bilingual Intake + Writer candidates.

Safety properties:
- Requires static-validation.json status PASS / ok true.
- Verifies exact workflow IDs and names before writes.
- Backs up Intake, Lashes, and Additives before any mutation.
- PUTs Intake then Lashes using same IDs only.
- GET-verifies canonical writable payloads after each PUT.
- If either deployment or verification fails, rolls both workflows back.
- Verifies Additives workflow fingerprint is unchanged.
- Never creates or deletes workflows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:5678/api/v1"
SECRET = "/opt/taoyuan/secrets/n8n-api-key"
WRITABLE = ("name", "nodes", "connections", "settings")

INTAKE_ID = "SAjlpjx6cmwkfklv"
INTAKE_NAME = "Drive Content Intake V1"
LASHES_ID = "lashesProdDraftV1"
LASHES_NAME = "Lashes - Production Draft VPS"
ADDITIVES_ID = "additivesDraftV1"
ADDITIVES_NAME = "Additives - Production Draft VPS"


def load_key() -> str:
    key = Path(SECRET).read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("n8n API key file is empty")
    return key


def api(method: str, path: str, key: str, payload=None):
    data = None
    headers = {"X-N8N-API-KEY": key, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else None
        except Exception:
            body = {"raw": raw[:1000]}
        return exc.code, body
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def get_workflow(key: str, workflow_id: str, expected_name: str) -> dict:
    status, body = api("GET", f"/workflows/{workflow_id}", key)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET {workflow_id} failed: HTTP {status}")
    if str(body.get("id")) != workflow_id:
        raise RuntimeError(f"workflow ID mismatch for {workflow_id}")
    if body.get("name") != expected_name:
        raise RuntimeError(f"workflow name mismatch for {workflow_id}: {body.get('name')!r}")
    return body


def writable(obj: dict) -> dict:
    missing = [k for k in WRITABLE if k not in obj]
    if missing:
        raise RuntimeError(f"workflow missing writable fields: {missing}")
    return {k: obj[k] for k in WRITABLE}


def fp(obj: dict) -> str:
    raw = json.dumps(writable(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_candidate(path: Path, expected_name: str) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(obj, dict) and isinstance(obj.get("body"), dict) and "nodes" in obj["body"]:
        obj = obj["body"]
    if not isinstance(obj, dict):
        raise RuntimeError(f"invalid candidate: {path}")
    if obj.get("name") != expected_name:
        raise RuntimeError(f"candidate name mismatch: expected {expected_name!r}, got {obj.get('name')!r}")
    return obj


def put_verify(key: str, workflow_id: str, expected_name: str, candidate: dict) -> dict:
    payload = writable(candidate)
    status, body = api("PUT", f"/workflows/{workflow_id}", key, payload)
    if status not in (200, 201) or not isinstance(body, dict):
        raise RuntimeError(f"PUT {workflow_id} failed: HTTP {status}")
    if str(body.get("id", workflow_id)) != workflow_id:
        raise RuntimeError(f"PUT changed workflow ID for {workflow_id}")
    after = get_workflow(key, workflow_id, expected_name)
    if fp(after) != fp(candidate):
        raise RuntimeError(f"GET verify fingerprint mismatch for {workflow_id}")
    return after


def rollback_one(key: str, workflow_id: str, expected_name: str, before: dict) -> bool:
    status, _ = api("PUT", f"/workflows/{workflow_id}", key, writable(before))
    if status not in (200, 201):
        return False
    try:
        restored = get_workflow(key, workflow_id, expected_name)
        return fp(restored) == fp(before)
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake-candidate", required=True)
    ap.add_argument("--lashes-candidate", required=True)
    ap.add_argument("--static-validation", required=True)
    ap.add_argument("--confirm-production", action="store_true")
    args = ap.parse_args()

    if not args.confirm_production:
        raise RuntimeError("refusing production deployment without --confirm-production")

    validation = json.loads(Path(args.static_validation).read_text(encoding="utf-8"))
    if validation.get("ok") is not True or str(validation.get("status", "")).upper() != "PASS":
        raise RuntimeError("static validation is not PASS")
    if int(validation.get("checks_failed", 0)) != 0:
        raise RuntimeError("static validation reports failed checks")

    intake_candidate = load_candidate(Path(args.intake_candidate), INTAKE_NAME)
    lashes_candidate = load_candidate(Path(args.lashes_candidate), LASHES_NAME)

    key = load_key()
    before_intake = get_workflow(key, INTAKE_ID, INTAKE_NAME)
    before_lashes = get_workflow(key, LASHES_ID, LASHES_NAME)
    before_additives = get_workflow(key, ADDITIVES_ID, ADDITIVES_NAME)
    add_fp_before = fp(before_additives)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(f"/opt/taoyuan/backups/n8n/lashes-bilingual-20261024/deployment/{stamp}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    for name, obj in (("intake-before.json", before_intake), ("lashes-before.json", before_lashes), ("additives-before.json", before_additives)):
        (backup_dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "ok": False,
        "backup_dir": str(backup_dir),
        "intake": {"id": INTAKE_ID, "before_fingerprint": fp(before_intake), "candidate_fingerprint": fp(intake_candidate), "deployed": False, "verified": False},
        "lashes": {"id": LASHES_ID, "before_fingerprint": fp(before_lashes), "candidate_fingerprint": fp(lashes_candidate), "deployed": False, "verified": False},
        "additives": {"id": ADDITIVES_ID, "before_fingerprint": add_fp_before, "after_fingerprint": None, "unchanged": None},
        "rollback": {"attempted": False, "intake_ok": None, "lashes_ok": None},
    }

    try:
        put_verify(key, INTAKE_ID, INTAKE_NAME, intake_candidate)
        result["intake"]["deployed"] = True
        result["intake"]["verified"] = True

        put_verify(key, LASHES_ID, LASHES_NAME, lashes_candidate)
        result["lashes"]["deployed"] = True
        result["lashes"]["verified"] = True

        after_additives = get_workflow(key, ADDITIVES_ID, ADDITIVES_NAME)
        add_fp_after = fp(after_additives)
        result["additives"]["after_fingerprint"] = add_fp_after
        result["additives"]["unchanged"] = add_fp_after == add_fp_before
        if not result["additives"]["unchanged"]:
            raise RuntimeError("Additives workflow changed unexpectedly")

        result["ok"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        result["error"] = str(exc)
        result["rollback"]["attempted"] = True
        result["rollback"]["lashes_ok"] = rollback_one(key, LASHES_ID, LASHES_NAME, before_lashes)
        result["rollback"]["intake_ok"] = rollback_one(key, INTAKE_ID, INTAKE_NAME, before_intake)
        try:
            add_now = get_workflow(key, ADDITIVES_ID, ADDITIVES_NAME)
            result["additives"]["after_fingerprint"] = fp(add_now)
            result["additives"]["unchanged"] = fp(add_now) == add_fp_before
        except Exception:
            pass
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
