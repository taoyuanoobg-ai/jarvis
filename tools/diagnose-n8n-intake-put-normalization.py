#!/usr/bin/env python3
"""Diagnose n8n normalization after PUT for the Intake workflow.

Safety:
- Only operates on exact Intake workflow ID/name.
- Backs up before mutation.
- PUTs the candidate, GETs the saved workflow, computes a deep diff of writable fields.
- Always rolls back to the exact pre-change workflow before exiting.
- Does not touch Lashes, Additives, WordPress, or Polylang.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:5678/api/v1"
SECRET = "/opt/taoyuan/secrets/n8n-api-key"
WORKFLOW_ID = "SAjlpjx6cmwkfklv"
WORKFLOW_NAME = "Drive Content Intake V1"
WRITABLE = ("name", "nodes", "connections", "settings")


def key() -> str:
    value = Path(SECRET).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("empty n8n api key")
    return value


def api(method: str, path: str, token: str, payload=None):
    data = None
    headers = {"X-N8N-API-KEY": token, "Accept": "application/json"}
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


def get_workflow(token: str) -> dict:
    status, body = api("GET", f"/workflows/{WORKFLOW_ID}", token)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET failed HTTP {status}")
    if str(body.get("id")) != WORKFLOW_ID or body.get("name") != WORKFLOW_NAME:
        raise RuntimeError("workflow identity mismatch")
    return body


def writable(obj: dict) -> dict:
    return {k: obj[k] for k in WRITABLE}


def unwrap(obj: dict) -> dict:
    if isinstance(obj.get("body"), dict) and "nodes" in obj["body"]:
        return obj["body"]
    return obj


def shorten(value, limit=300):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "..."


def deep_diff(expected, actual, path="$", out=None):
    if out is None:
        out = []
    if type(expected) is not type(actual):
        out.append({"path": path, "kind": "type", "expected": shorten(expected), "actual": shorten(actual)})
        return out
    if isinstance(expected, dict):
        ek, ak = set(expected), set(actual)
        for k in sorted(ek - ak):
            out.append({"path": f"{path}.{k}", "kind": "missing_actual", "expected": shorten(expected[k]), "actual": None})
        for k in sorted(ak - ek):
            out.append({"path": f"{path}.{k}", "kind": "extra_actual", "expected": None, "actual": shorten(actual[k])})
        for k in sorted(ek & ak):
            deep_diff(expected[k], actual[k], f"{path}.{k}", out)
        return out
    if isinstance(expected, list):
        if len(expected) != len(actual):
            out.append({"path": path, "kind": "list_length", "expected": len(expected), "actual": len(actual)})
        for i, (e, a) in enumerate(zip(expected, actual)):
            deep_diff(e, a, f"{path}[{i}]", out)
        return out
    if expected != actual:
        out.append({"path": path, "kind": "value", "expected": shorten(expected), "actual": shorten(actual)})
    return out


def rollback(token: str, before: dict) -> bool:
    status, _ = api("PUT", f"/workflows/{WORKFLOW_ID}", token, writable(before))
    if status not in (200, 201):
        return False
    restored = get_workflow(token)
    # Exact comparison may still include n8n normalization; verify all pre-change writable fields semantically.
    return deep_diff(writable(before), writable(restored)) == []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    args = ap.parse_args()

    candidate = unwrap(json.loads(Path(args.candidate).read_text(encoding="utf-8")))
    if candidate.get("name") != WORKFLOW_NAME:
        raise RuntimeError("candidate name mismatch")

    token = key()
    before = get_workflow(token)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = Path(f"/opt/taoyuan/backups/n8n/lashes-bilingual-20261024/normalization-diagnostic/{stamp}")
    outdir.mkdir(parents=True, exist_ok=False)
    (outdir / "before.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "candidate.json").write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"ok": False, "backup_dir": str(outdir), "put_status": None, "diff_count": None, "diffs": [], "rollback_ok": False}
    try:
        status, body = api("PUT", f"/workflows/{WORKFLOW_ID}", token, writable(candidate))
        result["put_status"] = status
        if status not in (200, 201):
            result["error"] = f"PUT failed HTTP {status}"
            return 2
        saved = get_workflow(token)
        (outdir / "after-get.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        diffs = deep_diff(writable(candidate), writable(saved))
        result["diff_count"] = len(diffs)
        result["diffs"] = diffs[:200]
        (outdir / "diff.json").write_text(json.dumps(diffs, ensure_ascii=False, indent=2), encoding="utf-8")
        result["ok"] = True
        return 0
    finally:
        result["rollback_ok"] = rollback(token, before)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
