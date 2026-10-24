#!/usr/bin/env python3
"""Extract compact, security-conscious bilingual patch anchors from n8n workflow backups.

Read-only. It never modifies workflow files or calls n8n/WordPress APIs.
It identifies likely schema/dedupe/state/routing/WordPress/category/draft/output anchors,
returns exact node names/types, selected parameters, code, and incoming/outgoing edges.
Credentials are reduced to reference metadata only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

KEYWORDS = {
    "intake": [
        "schema", "validate", "validation", "dedupe", "duplicate", "article_id",
        "file_id", "version", "site", "route", "routing", "lashes", "additives",
        "completed", "failed", "manual_review", "manual review", "retry", "state",
        "status", "writer", "execute workflow", "language"
    ],
    "writer": [
        "wordpress", "wp-json", "post", "draft", "category", "categories", "tag",
        "tags", "yoast", "slug", "featured", "image", "status", "result", "output",
        "http", "create", "language", "polylang"
    ],
}

SENSITIVE_KEYS = {
    "password", "token", "apiKey", "api_key", "authorization", "secret", "clientSecret",
    "accessToken", "refreshToken", "privateKey"
}


def sha(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sanitize(obj: Any, key: str | None = None) -> Any:
    if key and key.lower() in {k.lower() for k in SENSITIVE_KEYS}:
        return "<redacted>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "credentials":
                if isinstance(v, dict):
                    out[k] = {
                        ck: ({"id": cv.get("id"), "name": cv.get("name")} if isinstance(cv, dict) else "<reference>")
                        for ck, cv in v.items()
                    }
                else:
                    out[k] = "<reference>"
                continue
            if k.lower() in {x.lower() for x in SENSITIVE_KEYS}:
                out[k] = "<redacted>"
            else:
                out[k] = sanitize(v, k)
        return out
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj


def text_blob(node: dict[str, Any]) -> str:
    pieces = [str(node.get("name", "")), str(node.get("type", ""))]
    params = node.get("parameters", {})
    try:
        pieces.append(json.dumps(params, ensure_ascii=False))
    except Exception:
        pieces.append(str(params))
    return " ".join(pieces).lower()


def score_node(node: dict[str, Any], profile: str) -> tuple[int, list[str]]:
    blob = text_blob(node)
    hits = [k for k in KEYWORDS[profile] if k.lower() in blob]
    return len(hits), hits


def edges_for(name: str, connections: dict[str, Any]) -> dict[str, Any]:
    outgoing = sanitize(connections.get(name, {}))
    incoming: list[dict[str, Any]] = []
    for src, branches in connections.items():
        if not isinstance(branches, dict):
            continue
        for channel, slots in branches.items():
            if not isinstance(slots, list):
                continue
            for slot_idx, slot in enumerate(slots):
                if not isinstance(slot, list):
                    continue
                for item in slot:
                    if isinstance(item, dict) and item.get("node") == name:
                        incoming.append({
                            "from": src,
                            "channel": channel,
                            "slot": slot_idx,
                            "type": item.get("type"),
                            "index": item.get("index"),
                        })
    return {"incoming": incoming, "outgoing": outgoing}


def interesting_params(node: dict[str, Any]) -> dict[str, Any]:
    p = node.get("parameters", {}) or {}
    keep = {}
    preferred = {
        "jsCode", "functionCode", "url", "method", "operation", "resource", "workflowId",
        "conditions", "rules", "filters", "values", "assignments", "options", "bodyParameters",
        "jsonBody", "sendBody", "specifyBody", "queryParameters", "headerParameters",
        "authentication", "contentType", "responseFormat"
    }
    for k, v in p.items():
        if k in preferred:
            keep[k] = sanitize(v, k)
    if not keep:
        for k in list(p.keys())[:12]:
            keep[k] = sanitize(p[k], k)
    return keep


def unwrap_workflow(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and "body" in raw and isinstance(raw["body"], dict) and "nodes" in raw["body"]:
        return raw["body"]
    if isinstance(raw, dict) and "nodes" in raw:
        return raw
    raise ValueError("Input does not look like an n8n workflow or saved API body")


def extract(path: Path, profile: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    wf = unwrap_workflow(raw)
    nodes = wf.get("nodes", []) or []
    connections = wf.get("connections", {}) or {}

    ranked = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        score, hits = score_node(node, profile)
        if score > 0:
            ranked.append((score, node, hits))
    ranked.sort(key=lambda x: (-x[0], str(x[1].get("name", ""))))

    anchors = []
    for score, node, hits in ranked:
        anchors.append({
            "name": node.get("name"),
            "type": node.get("type"),
            "typeVersion": node.get("typeVersion"),
            "id": node.get("id"),
            "position": node.get("position"),
            "score": score,
            "keyword_hits": hits,
            "node_fingerprint": sha({
                "name": node.get("name"),
                "type": node.get("type"),
                "parameters": node.get("parameters", {}),
                "credentials": sanitize(node.get("credentials", {})),
            }),
            "credentials": sanitize(node.get("credentials", {})),
            "parameters": interesting_params(node),
            "edges": edges_for(str(node.get("name")), connections),
        })

    return {
        "source": str(path),
        "profile": profile,
        "workflow": {
            "id": wf.get("id"),
            "name": wf.get("name"),
            "active": wf.get("active"),
            "node_count": len(nodes),
            "connection_sources": len(connections),
            "fingerprint": sha({
                "id": wf.get("id"), "name": wf.get("name"), "active": wf.get("active"),
                "nodes": nodes, "connections": connections, "settings": wf.get("settings", {})
            }),
        },
        "anchors": anchors,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake", required=True)
    ap.add_argument("--lashes", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    result = {
        "ok": True,
        "intake": extract(Path(args.intake), "intake"),
        "lashes": extract(Path(args.lashes), "writer"),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(out),
        "intake_anchors": len(result["intake"]["anchors"]),
        "lashes_anchors": len(result["lashes"]["anchors"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
