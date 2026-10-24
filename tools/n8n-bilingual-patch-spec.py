#!/usr/bin/env python3
"""Create a minimal bilingual patch spec from compact n8n workflow anchors.

Read-only. This tool does not call n8n, WordPress, or modify workflows. It keeps
only the exact source/HTTP/branching/edge fields needed to generate a deterministic
patcher while avoiding credential secrets.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

INTAKE_PRIORITY = (
    "schema", "validate", "dedupe", "duplicate", "state", "persist", "route",
    "routing", "language", "writer", "handoff", "retry", "manual_review",
    "manual review", "completed", "failed", "lashes",
)

LASHES_PRIORITY = (
    "wordpress", "draft", "category", "slug", "tag", "yoast", "result",
    "output", "http", "request", "language", "guard", "normalize", "create",
)

PARAM_KEYS = (
    "jsCode", "functionCode", "url", "method", "operation", "resource",
    "workflowId", "conditions", "rules", "filters", "values", "assignments",
    "options", "bodyParameters", "jsonBody", "sendBody", "specifyBody",
    "queryParameters", "headerParameters", "authentication", "contentType",
    "responseFormat",
)


def blob(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True).lower()


def score(anchor: dict[str, Any], terms: tuple[str, ...]) -> tuple[int, list[str]]:
    b = blob(anchor)
    hits = [t for t in terms if t in b]
    t = str(anchor.get("type", "")).lower()
    bonus = 0
    if "code" in t:
        bonus += 5
    if "httprequest" in t:
        bonus += 4
    if any(k in t for k in ("if", "switch", "filter", "merge", "execute")):
        bonus += 3
    return len(hits) + bonus, hits


def slim(anchor: dict[str, Any], hits: list[str]) -> dict[str, Any]:
    params = anchor.get("parameters", {}) or {}
    kept_params = {k: params[k] for k in PARAM_KEYS if k in params}
    if not kept_params:
        # Preserve only small scalar/simple parameters as fallback.
        for k, v in params.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                kept_params[k] = v
    return {
        "name": anchor.get("name"),
        "type": anchor.get("type"),
        "typeVersion": anchor.get("typeVersion"),
        "node_fingerprint": anchor.get("node_fingerprint") or anchor.get("fingerprint"),
        "selector_hits": hits,
        "parameters": kept_params,
        "credentials": anchor.get("credentials", {}),
        "incoming": anchor.get("incoming") or anchor.get("incoming_edges") or ((anchor.get("edges") or {}).get("incoming")),
        "outgoing": anchor.get("outgoing") or anchor.get("outgoing_edges") or ((anchor.get("edges") or {}).get("outgoing")),
    }


def select(items: list[dict[str, Any]], terms: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    ranked = []
    for a in items:
        s, hits = score(a, terms)
        if s > 0:
            ranked.append((s, str(a.get("name", "")).lower(), slim(a, hits)))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in ranked[:limit]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-intake", type=int, default=6)
    ap.add_argument("--max-lashes", type=int, default=8)
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    intake = data.get("intake_selected", [])
    lashes = data.get("lashes_selected", [])
    if not isinstance(intake, list) or not isinstance(lashes, list):
        raise SystemExit("input missing intake_selected/lashes_selected arrays")

    out = {
        "source": str(Path(args.input)),
        "intake": select(intake, INTAKE_PRIORITY, args.max_intake),
        "lashes": select(lashes, LASHES_PRIORITY, args.max_lashes),
    }
    p = Path(args.output)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    p.write_text(text, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(p),
        "intake": len(out["intake"]),
        "lashes": len(out["lashes"]),
        "size_bytes": len(text.encode("utf-8")),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
