#!/usr/bin/env python3
"""Guarded same-ID n8n workflow deployment helper.

Flow:
GET current -> backup -> validate expected identity -> validate payload -> PUT same ID
-> GET verify -> automatic rollback on verification failure.

Production workflows require --allow-production and --expected-fingerprint.
The helper never creates workflows and never deletes workflows.
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
WRITABLE_FIELDS = ("name", "nodes", "connections", "settings")


def load_key() -> str:
    value = Path(SECRET).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("API key file is empty")
    return value


def api(method: str, path: str, token: str, payload=None):
    data = None
    headers = {"X-N8N-API-KEY": token, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
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


def get_workflow(token: str, workflow_id: str) -> dict:
    status, body = api("GET", f"/workflows/{workflow_id}", token)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET workflow failed: HTTP {status}")
    return body


def writable(obj: dict) -> dict:
    missing = [k for k in WRITABLE_FIELDS if k not in obj]
    if missing:
        raise RuntimeError(f"workflow missing writable field(s): {missing}")
    return {k: obj[k] for k in WRITABLE_FIELDS}


def canonical_hash(obj: dict) -> str:
    payload = writable(obj)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("workflow_id")
    p.add_argument("payload_file")
    p.add_argument("--expected-name", required=True)
    p.add_argument("--expected-fingerprint")
    p.add_argument("--allow-production", action="store_true")
    p.add_argument("--test-only", action="store_true")
    args = p.parse_args()

    if args.allow_production and args.test_only:
        raise RuntimeError("choose either --allow-production or --test-only")
    if not args.allow_production and not args.test_only:
        raise RuntimeError("must explicitly choose --allow-production or --test-only")
    if args.allow_production and not args.expected_fingerprint:
        raise RuntimeError("production deployment requires --expected-fingerprint")

    token = load_key()
    before = get_workflow(token, args.workflow_id)
    if str(before.get("id")) != args.workflow_id:
        raise RuntimeError("workflow ID mismatch")
    if before.get("name") != args.expected_name:
        raise RuntimeError(f"workflow name mismatch: {before.get('name')!r}")
    if args.test_only:
        if "TEST" not in args.expected_name.upper():
            raise RuntimeError("--test-only requires TEST in workflow name")
        if before.get("active") is True:
            raise RuntimeError("refusing to modify active TEST workflow")

    before_fp = canonical_hash(before)
    if args.expected_fingerprint and before_fp != args.expected_fingerprint:
        raise RuntimeError(f"fingerprint mismatch: actual={before_fp}")

    payload_obj = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    payload = writable(payload_obj)
    if payload.get("name") != args.expected_name:
        raise RuntimeError("payload name does not match expected workflow name")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(f"/opt/taoyuan/backups/n8n/deployments/{stamp}-{args.workflow_id}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_file = backup_dir / "before.json"
    backup_file.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "ok": False,
        "workflow_id": args.workflow_id,
        "workflow_name": args.expected_name,
        "backup": str(backup_file),
        "before_fingerprint": before_fp,
        "put_same_id": False,
        "verify": False,
        "rollback_attempted": False,
        "rollback_ok": None,
        "after_fingerprint": None,
    }

    status, body = api("PUT", f"/workflows/{args.workflow_id}", token, payload)
    if status not in (200, 201) or not isinstance(body, dict):
        raise RuntimeError(f"PUT failed: HTTP {status}")
    result["put_same_id"] = str(body.get("id", args.workflow_id)) == args.workflow_id
    if not result["put_same_id"]:
        raise RuntimeError("PUT did not preserve workflow ID")

    after = get_workflow(token, args.workflow_id)
    expected_fp = canonical_hash(payload)
    after_fp = canonical_hash(after)
    result["after_fingerprint"] = after_fp
    result["verify"] = (
        str(after.get("id")) == args.workflow_id
        and after.get("name") == args.expected_name
        and after_fp == expected_fp
    )

    if not result["verify"]:
        result["rollback_attempted"] = True
        rollback_payload = writable(before)
        rstatus, _ = api("PUT", f"/workflows/{args.workflow_id}", token, rollback_payload)
        if rstatus in (200, 201):
            restored = get_workflow(token, args.workflow_id)
            result["rollback_ok"] = canonical_hash(restored) == before_fp
        else:
            result["rollback_ok"] = False
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
