#!/usr/bin/env python3
"""Guarded cleanup for duplicate TEST ONLY n8n workflows.

Safety properties:
- reads API key only from a local root-only file
- only considers workflows whose name exactly matches --exact-name
- refuses active workflows
- always preserves --keep-id
- dry-run by default; deletion requires --execute
- backs up each candidate before deletion
- never touches workflows with other names
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:5678/api/v1"
SECRET = "/opt/taoyuan/secrets/n8n-api-key"
DEFAULT_NAME = "TEST ONLY - Deployment Channel Verification"


def key() -> str:
    value = Path(SECRET).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("API key file is empty")
    return value


def api(method: str, path: str, token: str):
    req = Request(
        BASE + path,
        headers={"X-N8N-API-KEY": token, "Accept": "application/json"},
        method=method,
    )
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else None
        except Exception:
            body = {"raw": raw[:500]}
        return exc.code, body
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def list_all(token: str):
    status, body = api("GET", "/workflows?limit=250", token)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"list workflows failed: HTTP {status}")
    data = body.get("data")
    if not isinstance(data, list):
        raise RuntimeError("unexpected workflow list response")
    return data


def get_one(token: str, workflow_id: str):
    status, body = api("GET", f"/workflows/{workflow_id}", token)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET {workflow_id} failed: HTTP {status}")
    return body


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--keep-id", required=True)
    p.add_argument("--exact-name", default=DEFAULT_NAME)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()

    token = key()
    workflows = list_all(token)
    matches = [w for w in workflows if w.get("name") == args.exact_name]

    if not any(w.get("id") == args.keep_id for w in matches):
        raise RuntimeError("keep-id is not present among exact-name TEST workflows")

    active = [w.get("id") for w in matches if w.get("active") is True]
    if active:
        raise RuntimeError(f"refusing cleanup: exact-name TEST workflow(s) active: {active}")

    candidates = [w for w in matches if w.get("id") != args.keep_id]
    result = {
        "ok": True,
        "mode": "execute" if args.execute else "dry-run",
        "exact_name": args.exact_name,
        "keep_id": args.keep_id,
        "matching_count": len(matches),
        "candidate_ids": [w.get("id") for w in candidates],
        "deleted_ids": [],
        "backup_dir": None,
    }

    if not args.execute:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(f"/opt/taoyuan/backups/n8n/deployment-channel-test/cleanup-{stamp}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    result["backup_dir"] = str(backup_dir)

    for item in candidates:
        wid = str(item.get("id"))
        full = get_one(token, wid)
        if full.get("name") != args.exact_name or full.get("active") is True:
            raise RuntimeError(f"safety gate failed before delete for {wid}")
        (backup_dir / f"{wid}.json").write_text(
            json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        status, _ = api("DELETE", f"/workflows/{wid}", token)
        if status not in (200, 204):
            raise RuntimeError(f"DELETE {wid} failed: HTTP {status}")
        result["deleted_ids"].append(wid)

    remaining = list_all(token)
    remaining_matches = [w for w in remaining if w.get("name") == args.exact_name]
    ids = [w.get("id") for w in remaining_matches]
    if ids != [args.keep_id] and set(ids) != {args.keep_id}:
        raise RuntimeError(f"post-delete verification failed; remaining exact-name ids={ids}")

    result["remaining_exact_name_ids"] = ids
    result["workflow_count_after"] = len(remaining)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
