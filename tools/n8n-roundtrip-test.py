#!/usr/bin/env python3
"""TEST-only n8n Public API same-ID PUT/GET/rollback verifier.

Reads the API key from /opt/taoyuan/secrets/n8n-api-key.
Safety gates:
- workflow name must contain TEST (case-insensitive)
- workflow must be inactive
- backs up full GET response before write
- modifies only the workflow name temporarily
- verifies same ID and workflow count
- always attempts rollback after a successful PUT
- never deletes workflows
"""

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

BASE = "http://127.0.0.1:5678/api/v1"
SECRET = "/opt/taoyuan/secrets/n8n-api-key"
BACKUP_ROOT = "/opt/taoyuan/backups/n8n/deployment-channel-test"
SUFFIX = " [API-ROUNDTRIP-V2]"


def key():
    value = Path(SECRET).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("API key file is empty")
    return value


def request(method, path, api_key, payload=None):
    data = None
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else None
        except Exception:
            body = {"raw": raw[:500]}
        return exc.code, body


def must(method, path, api_key, payload=None):
    status, body = request(method, path, api_key, payload)
    if not 200 <= status < 300:
        msg = body.get("message") if isinstance(body, dict) else None
        raise RuntimeError(f"HTTP {status} {method} {path}" + (f": {msg}" if msg else ""))
    return body


def writable(wf):
    required = ("name", "nodes", "connections", "settings")
    missing = [k for k in required if k not in wf]
    if missing:
        raise RuntimeError("missing required fields: " + ",".join(missing))
    return {
        "name": wf["name"],
        "nodes": copy.deepcopy(wf["nodes"]),
        "connections": copy.deepcopy(wf["connections"]),
        "settings": copy.deepcopy(wf["settings"]),
    }


def list_all(api_key):
    items = []
    cursor = None
    for _ in range(100):
        path = "/workflows?limit=100"
        if cursor:
            path += "&cursor=" + quote(cursor, safe="")
        body = must("GET", path, api_key)
        if isinstance(body, dict):
            items.extend(body.get("data", []))
            cursor = body.get("nextCursor")
        elif isinstance(body, list):
            items.extend(body)
            cursor = None
        else:
            raise RuntimeError("unexpected workflows list response")
        if not cursor:
            return items
    raise RuntimeError("pagination limit exceeded")


def get_wf(api_key, workflow_id):
    body = must("GET", f"/workflows/{workflow_id}", api_key)
    if not isinstance(body, dict):
        raise RuntimeError("unexpected GET response")
    return body


def main():
    if len(sys.argv) != 2:
        print("usage: n8n-roundtrip-test.py <TEST_WORKFLOW_ID>", file=sys.stderr)
        return 64

    workflow_id = sys.argv[1]
    api_key = key()
    original = get_wf(api_key, workflow_id)
    name = original.get("name", "")

    if "test" not in name.lower():
        raise RuntimeError("safety gate: workflow name does not contain TEST")
    if original.get("active") is True:
        raise RuntimeError("safety gate: TEST workflow is active")
    if str(original.get("id")) != workflow_id:
        raise RuntimeError("workflow ID mismatch")

    before_count = len(list_all(api_key))
    original_payload = writable(original)
    original_active = original.get("active")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(BACKUP_ROOT) / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{workflow_id}-before.json"
    backup_path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

    test_payload = copy.deepcopy(original_payload)
    test_payload["name"] = name + SUFFIX if not name.endswith(SUFFIX) else name

    put_done = False
    v2_ok = False
    rollback_ok = False
    rollback_error = None

    try:
        must("PUT", f"/workflows/{workflow_id}", api_key, test_payload)
        put_done = True
        v2 = get_wf(api_key, workflow_id)
        v2_ok = (
            str(v2.get("id")) == workflow_id
            and v2.get("name") == test_payload["name"]
            and v2.get("nodes") == test_payload["nodes"]
            and v2.get("connections") == test_payload["connections"]
            and v2.get("settings") == test_payload["settings"]
            and v2.get("active") == original_active
            and len(list_all(api_key)) == before_count
        )
        if not v2_ok:
            raise RuntimeError("v2 verification failed")
    finally:
        if put_done:
            try:
                must("PUT", f"/workflows/{workflow_id}", api_key, original_payload)
                rolled = get_wf(api_key, workflow_id)
                rollback_ok = (
                    str(rolled.get("id")) == workflow_id
                    and writable(rolled) == original_payload
                    and rolled.get("active") == original_active
                    and len(list_all(api_key)) == before_count
                )
                if not rollback_ok:
                    rollback_error = "rollback verification failed"
            except Exception as exc:
                rollback_error = str(exc)

    result = {
        "ok": bool(put_done and v2_ok and rollback_ok),
        "workflow_id": workflow_id,
        "workflow_name": name,
        "backup": str(backup_path),
        "put_same_id": put_done,
        "v2_verify": v2_ok,
        "rollback": rollback_ok,
        "workflow_count_before": before_count,
        "workflow_count_after": len(list_all(api_key)),
        "rollback_error": rollback_error,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
