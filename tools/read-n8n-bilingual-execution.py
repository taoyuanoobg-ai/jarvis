#!/usr/bin/env python3
"""Read selected node outputs from one n8n execution via Public API.

Read-only. Uses GET /api/v1/executions/{id}?includeData=true and prints only
selected bilingual Intake node outputs plus compact derived checks. It never
mutates n8n, Google Drive, WordPress, or Polylang.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:5678/api/v1"
SECRET = "/opt/taoyuan/secrets/n8n-api-key"
WORKFLOW_ID = "SAjlpjx6cmwkfklv"

DEFAULT_NODES = [
    "Validate Article JSON",
    "Bilingual Prepare Dedupe",
    "Execute Existing Lashes Writer",
    "Verify Draft Writer Result",
    "Bilingual Language Decision",
    "Bilingual Set Language",
    "Bilingual Verify Language",
    "Persist Intake State",
    "Bilingual Link Decision",
    "Bilingual Link Translations",
    "Bilingual Verify EN Translation",
    "Bilingual Verify PT Translation",
    "Bilingual Verify Translation Link",
    "Route Intake Status",
    "Finalize Scan",
]


def load_key() -> str:
    key = Path(SECRET).read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("n8n API key file is empty")
    return key


def get_json(path: str, key: str, query: dict | None = None):
    url = BASE + path
    if query:
        url += "?" + urlencode(query)
    req = Request(url, headers={"X-N8N-API-KEY": key, "Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else None
        except Exception:
            body = {"raw": raw[:2000]}
        return exc.code, body
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def first_json_from_run(run: dict):
    data = run.get("data") if isinstance(run, dict) else None
    if not isinstance(data, dict):
        return None
    main = data.get("main")
    if not isinstance(main, list):
        return None
    for branch in main:
        if not isinstance(branch, list):
            continue
        for item in branch:
            if isinstance(item, dict) and isinstance(item.get("json"), dict):
                return item["json"]
    return None


def compact_run(run: dict) -> dict:
    out = {
        "startTime": run.get("startTime"),
        "executionTime": run.get("executionTime"),
        "source": run.get("source"),
        "error": run.get("error"),
        "json": first_json_from_run(run),
    }
    return {k: v for k, v in out.items() if v is not None}


def pick(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("execution_id")
    ap.add_argument("--expected-article-id")
    ap.add_argument("--expected-group-id")
    ap.add_argument("--expected-language", choices=["en", "pt"])
    ap.add_argument("--expected-post-id", type=int)
    ap.add_argument("--output")
    args = ap.parse_args()

    key = load_key()
    status, body = get_json(f"/executions/{args.execution_id}", key, {"includeData": "true"})
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"execution GET failed: HTTP {status}: {body}")

    workflow_id = str(body.get("workflowId") or pick(body, "workflowData", "id") or "")
    if workflow_id and workflow_id != WORKFLOW_ID:
        raise RuntimeError(f"execution belongs to unexpected workflow: {workflow_id}")

    run_data = pick(body, "data", "resultData", "runData")
    if not isinstance(run_data, dict):
        raise RuntimeError("execution response contains no data.resultData.runData; execution data may not be saved")

    selected = {}
    for name in DEFAULT_NODES:
        runs = run_data.get(name)
        if not isinstance(runs, list) or not runs:
            continue
        selected[name] = {
            "run_count": len(runs),
            "last_run": compact_run(runs[-1]),
        }

    validate = pick(selected, "Validate Article JSON", "last_run", "json") or {}
    dedupe = pick(selected, "Bilingual Prepare Dedupe", "last_run", "json") or {}
    writer = pick(selected, "Verify Draft Writer Result", "last_run", "json") or {}
    lang_verify = pick(selected, "Bilingual Verify Language", "last_run", "json") or {}
    persist = pick(selected, "Persist Intake State", "last_run", "json") or {}

    article = validate.get("article") if isinstance(validate, dict) else None
    article = article if isinstance(article, dict) else {}
    writer_result = writer.get("writer_result") if isinstance(writer, dict) else None
    writer_result = writer_result if isinstance(writer_result, dict) else {}

    derived = {
        "execution_status_success": body.get("status") == "success",
        "article_id": article.get("article_id"),
        "translation_group_id": article.get("translation_group_id") or writer.get("translation_group_id"),
        "language": article.get("language") or writer.get("language") or lang_verify.get("language"),
        "dedupe_route": dedupe.get("bilingual_route"),
        "dedupe_hit": dedupe.get("bilingual_dedupe_hit"),
        "post_id": writer_result.get("post_id") or lang_verify.get("post_id"),
        "writer_status": writer_result.get("status"),
        "writer_success": writer_result.get("success"),
        "polylang_verified": lang_verify.get("polylang_language_verified") if "polylang_language_verified" in lang_verify else lang_verify.get("verified"),
        "persist_status": persist.get("status"),
        "persist_attempts": persist.get("attempts"),
    }

    checks = {
        "execution_success": derived["execution_status_success"],
        "article_id_ok": args.expected_article_id is None or derived["article_id"] == args.expected_article_id,
        "group_id_ok": args.expected_group_id is None or derived["translation_group_id"] == args.expected_group_id,
        "language_ok": args.expected_language is None or derived["language"] == args.expected_language,
        "post_id_ok": args.expected_post_id is None or int(derived["post_id"] or 0) == args.expected_post_id,
        "writer_draft_ok": derived["writer_status"] == "draft",
        "dedupe_not_hit": derived["dedupe_hit"] is False,
        "persist_completed": derived["persist_status"] == "completed",
    }

    result = {
        "ok": all(v is True for v in checks.values()),
        "execution_id": str(body.get("id") or args.execution_id),
        "workflow_id": workflow_id or WORKFLOW_ID,
        "status": body.get("status"),
        "mode": body.get("mode"),
        "startedAt": body.get("startedAt"),
        "stoppedAt": body.get("stoppedAt"),
        "derived": derived,
        "checks": checks,
        "selected_nodes": selected,
    }

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
