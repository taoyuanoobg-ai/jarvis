#!/usr/bin/env python3
"""Read-only n8n workflow structure inspector.

Produces a compact structural manifest from exported/API workflow JSON without
modifying the source. Intended to help build safe deterministic workflow patches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

CODE_KEYS = {"jsCode", "code", "functionCode"}
HTTP_KEYS = {
    "method", "url", "authentication", "sendQuery", "queryParameters",
    "sendHeaders", "headerParameters", "sendBody", "contentType",
    "specifyBody", "jsonBody", "bodyParameters", "options",
}
FLOW_KEYS = {"conditions", "rules", "value1", "value2", "mode", "combineOperation", "options"}


def short_hash(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def clip(value, limit=12000):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n...<truncated {len(value)-limit} chars>"
    return value


def select_params(node):
    p = node.get("parameters") or {}
    t = node.get("type", "")
    out = {"parameter_keys": sorted(p.keys())}
    if "code" in t.lower() or "function" in t.lower():
        for k in CODE_KEYS:
            if k in p:
                out[k] = clip(p[k])
    if "httpRequest" in t or "http" in t.lower():
        for k in HTTP_KEYS:
            if k in p:
                out[k] = clip(p[k])
    if any(x in t.lower() for x in ("if", "switch", "filter", "merge")):
        for k in FLOW_KEYS:
            if k in p:
                out[k] = clip(p[k])
    if "executeWorkflow" in t or "workflow" in t.lower():
        for k in ("workflowId", "source", "options", "mode"):
            if k in p:
                out[k] = clip(p[k])
    return out


def manifest(path: Path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    # tolerate wrappers from helper output
    if isinstance(doc, dict) and "body" in doc and isinstance(doc["body"], dict) and "nodes" in doc["body"]:
        doc = doc["body"]
    nodes = []
    for n in doc.get("nodes", []):
        nodes.append({
            "name": n.get("name"),
            "type": n.get("type"),
            "typeVersion": n.get("typeVersion"),
            "position": n.get("position"),
            "disabled": n.get("disabled", False),
            "credential_types": sorted((n.get("credentials") or {}).keys()),
            "parameters": select_params(n),
            "node_hash": short_hash({
                "name": n.get("name"),
                "type": n.get("type"),
                "parameters": n.get("parameters"),
            }),
        })
    return {
        "source": str(path),
        "id": doc.get("id"),
        "name": doc.get("name"),
        "active": doc.get("active"),
        "settings": doc.get("settings"),
        "node_count": len(doc.get("nodes", [])),
        "connection_source_count": len(doc.get("connections", {})),
        "workflow_hash": short_hash({
            "name": doc.get("name"),
            "nodes": doc.get("nodes"),
            "connections": doc.get("connections"),
            "settings": doc.get("settings"),
        }),
        "nodes": nodes,
        "connections": doc.get("connections", {}),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--output")
    args = ap.parse_args()
    result = [manifest(Path(x)) for x in args.files]
    payload = result[0] if len(result) == 1 else result
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        p = Path(args.output)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(json.dumps({"ok": True, "output": str(p), "items": len(result)}))
    else:
        print(text)


if __name__ == "__main__":
    main()
