#!/usr/bin/env python3
"""Safe local n8n Public API helper.

Reads the API key from a root-only file on the VPS so shell commands never need
header interpolation. Supports verify/list/get/put/delete. It never prints the
API key or request headers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE = "http://127.0.0.1:5678/api/v1"
DEFAULT_SECRET = "/opt/taoyuan/secrets/n8n-api-key"


def load_key(path: str) -> str:
    key = Path(path).read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("API key file is empty")
    return key


def request_json(method: str, url: str, key: str, payload=None):
    data = None
    headers = {
        "X-N8N-API-KEY": key,
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            return resp.status, json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = {"raw": body[:1000]}
        return exc.code, parsed
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def save_json(path: str, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--secret-file", default=DEFAULT_SECRET)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify")

    p_list = sub.add_parser("list")
    p_list.add_argument("--output")

    p_get = sub.add_parser("get")
    p_get.add_argument("workflow_id")
    p_get.add_argument("--output")

    p_put = sub.add_parser("put")
    p_put.add_argument("workflow_id")
    p_put.add_argument("payload_file")
    p_put.add_argument("--output")

    p_delete = sub.add_parser("delete")
    p_delete.add_argument("workflow_id")
    p_delete.add_argument("--output")

    args = parser.parse_args()
    key = load_key(args.secret_file)
    base = args.base_url.rstrip("/")

    if args.cmd == "verify":
        status, body = request_json("GET", f"{base}/workflows?limit=1", key)
        ok = status == 200
        print(json.dumps({"ok": ok, "status": status, "api_key_valid": ok}))
        return 0 if ok else 2

    if args.cmd == "list":
        status, body = request_json("GET", f"{base}/workflows", key)
    elif args.cmd == "get":
        status, body = request_json("GET", f"{base}/workflows/{args.workflow_id}", key)
    elif args.cmd == "put":
        payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
        status, body = request_json("PUT", f"{base}/workflows/{args.workflow_id}", key, payload)
    elif args.cmd == "delete":
        status, body = request_json("DELETE", f"{base}/workflows/{args.workflow_id}", key)
    else:
        raise AssertionError(args.cmd)

    out_path = getattr(args, "output", None)
    if out_path:
        save_json(out_path, {"status": status, "body": body})
        print(json.dumps({"ok": 200 <= status < 300, "status": status, "saved": out_path}))
    else:
        safe = {"ok": 200 <= status < 300, "status": status}
        if isinstance(body, dict):
            for k in ("id", "name", "active"):
                if k in body:
                    safe[k] = body[k]
        print(json.dumps(safe, ensure_ascii=False))
    return 0 if 200 <= status < 300 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)
