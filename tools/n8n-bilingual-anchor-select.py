#!/usr/bin/env python3
"""Select the small set of workflow anchors needed for bilingual patch construction.

Reads bilingual-anchors.json produced by n8n-bilingual-anchor-extract.py and
writes a compact patch-anchor bundle. This is read-only and never mutates n8n.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

INTAKE_TERMS = (
    "schema", "validate", "dedupe", "duplicate", "state", "status", "route",
    "routing", "lash", "site", "language", "article", "completed", "failed",
    "manual_review", "manual review", "retry", "writer", "handoff", "persist",
)
LASHES_TERMS = (
    "wordpress", "wp", "post", "draft", "category", "tag", "slug", "yoast",
    "image", "featured", "result", "output", "create", "http", "request",
    "language", "publish", "guard", "normalize",
)

def text_blob(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        return str(obj).lower()


def iter_anchor_lists(obj, path="root"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                yield p, v
            yield from iter_anchor_lists(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_anchor_lists(v, f"{path}[{i}]")


def score(anchor: dict, terms: tuple[str, ...]) -> tuple[int, list[str]]:
    blob = text_blob(anchor)
    hits = [t for t in terms if t in blob]
    node_type = str(anchor.get("type", "")).lower()
    # Code / HTTP / branching nodes are especially important for patch planning.
    bonus = 0
    for key in ("code", "httprequest", "if", "switch", "filter", "merge", "execute"):
        if key in node_type:
            bonus += 2
    return len(hits) + bonus, hits


def compact(anchor: dict, hits: list[str]) -> dict:
    keep = {
        "name", "type", "typeVersion", "node_fingerprint", "fingerprint",
        "keyword_hits", "parameters", "incoming", "incoming_edges", "outgoing",
        "outgoing_edges", "connections", "credentials", "id", "position",
    }
    out = {k: v for k, v in anchor.items() if k in keep}
    out["selector_hits"] = hits
    # Preserve full parameters for selected anchors because Code/HTTP node source
    # and exact conditions live there and are required to build deterministic patches.
    return out


def choose_section(data: dict, label: str) -> list[dict]:
    label_l = label.lower()
    candidates = []
    for p, lst in iter_anchor_lists(data):
        pl = p.lower()
        if label_l in pl and "anchor" in pl:
            candidates.extend(lst)
    if candidates:
        return candidates
    # Fallback: inspect all dict lists and use workflow name/path hints.
    for p, lst in iter_anchor_lists(data):
        pl = p.lower()
        if label_l in pl:
            candidates.extend(lst)
    return candidates


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-intake", type=int, default=10)
    ap.add_argument("--max-lashes", type=int, default=14)
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))

    intake = choose_section(data, "intake")
    lashes = choose_section(data, "lashes")

    def select(items, terms, limit):
        ranked = []
        for a in items:
            s, hits = score(a, terms)
            if s > 0:
                ranked.append((s, str(a.get("name", "")), compact(a, hits)))
        ranked.sort(key=lambda x: (-x[0], x[1].lower()))
        return [x[2] for x in ranked[:limit]]

    out = {
        "source": str(Path(args.input)),
        "intake_total_anchors": len(intake),
        "lashes_total_anchors": len(lashes),
        "intake_selected": select(intake, INTAKE_TERMS, args.max_intake),
        "lashes_selected": select(lashes, LASHES_TERMS, args.max_lashes),
    }

    p = Path(args.output)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(p),
        "intake_selected": len(out["intake_selected"]),
        "lashes_selected": len(out["lashes_selected"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
