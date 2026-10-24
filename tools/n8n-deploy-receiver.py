#!/usr/bin/env python3
"""Reliable file receiver for Jarvis/n8n deployment bootstrap.

This script deliberately does NOT modify n8n, Docker, WordPress, or databases.
It only receives chunked base64 payloads, verifies SHA-256 and JSON validity,
and materializes a verified file on the VPS.  Higher-level deployment tooling
can safely build on top of this once the transport is proven.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
ROOT = Path(os.environ.get("N8N_DEPLOY_ROOT", "/opt/taoyuan/runtime/n8n-deploy"))
SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
CHUNK_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def die(message: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def session_dir(session_id: str) -> Path:
    if not SESSION_RE.fullmatch(session_id):
        die("invalid session_id")
    return ROOT / session_id


def manifest_path(session_id: str) -> Path:
    return session_dir(session_id) / "manifest.json"


def load_manifest(session_id: str) -> dict[str, Any]:
    path = manifest_path(session_id)
    if not path.is_file():
        die("unknown session")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        die(f"manifest unreadable: {exc}")
    return data


def save_manifest(session_id: str, data: dict[str, Any]) -> None:
    path = manifest_path(session_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def cmd_init(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256):
        die("sha256 must be 64 hex characters")
    if args.chunks < 1 or args.chunks > 100000:
        die("invalid chunk count")
    filename = Path(args.filename).name
    if filename != args.filename or not filename:
        die("filename must be a basename")

    sdir = session_dir(args.session_id)
    expected = {
        "version": VERSION,
        "session_id": args.session_id,
        "filename": filename,
        "expected_sha256": args.sha256.lower(),
        "expected_chunks": args.chunks,
        "verified": False,
        "materialized": False,
        "last_error": None,
    }

    if sdir.exists():
        current = load_manifest(args.session_id)
        comparable = ("session_id", "filename", "expected_sha256", "expected_chunks")
        if all(current.get(k) == expected.get(k) for k in comparable):
            emit({"ok": True, "duplicate": True, "session_id": args.session_id})
            return
        die("session conflict", 3)

    (sdir / "chunks").mkdir(parents=True, exist_ok=False)
    save_manifest(args.session_id, expected)
    emit({"ok": True, "session_id": args.session_id, "expected_chunks": args.chunks})


def cmd_append(args: argparse.Namespace) -> None:
    manifest = load_manifest(args.session_id)
    total = int(manifest["expected_chunks"])
    if args.seq < 0 or args.seq >= total:
        die("chunk sequence out of range")
    if not args.chunk or not CHUNK_RE.fullmatch(args.chunk):
        die("chunk is not valid base64 text")

    cpath = session_dir(args.session_id) / "chunks" / f"{args.seq:06d}.b64"
    if cpath.exists():
        current = cpath.read_text(encoding="ascii")
        if current == args.chunk:
            emit({"ok": True, "duplicate": True, "seq": args.seq})
            return
        die("chunk conflict", 3)

    tmp = cpath.with_suffix(".tmp")
    tmp.write_text(args.chunk, encoding="ascii")
    os.replace(tmp, cpath)
    emit({"ok": True, "seq": args.seq})


def collect_chunks(session_id: str, total: int) -> tuple[list[int], bytes]:
    cdir = session_dir(session_id) / "chunks"
    missing: list[int] = []
    parts: list[str] = []
    for seq in range(total):
        path = cdir / f"{seq:06d}.b64"
        if not path.is_file():
            missing.append(seq)
        else:
            parts.append(path.read_text(encoding="ascii"))
    if missing:
        return missing, b""
    try:
        raw = base64.b64decode("".join(parts), validate=True)
    except Exception as exc:
        die(f"base64 decode failed: {exc}")
    return [], raw


def cmd_verify(args: argparse.Namespace) -> None:
    manifest = load_manifest(args.session_id)
    total = int(manifest["expected_chunks"])
    missing, raw = collect_chunks(args.session_id, total)
    if missing:
        manifest["verified"] = False
        manifest["last_error"] = f"missing chunks: {missing[:20]}"
        save_manifest(args.session_id, manifest)
        emit({"ok": False, "verified": False, "missing": missing})
        raise SystemExit(4)

    actual = hashlib.sha256(raw).hexdigest()
    manifest["actual_sha256"] = actual
    if actual != manifest["expected_sha256"]:
        manifest["verified"] = False
        manifest["last_error"] = "sha256 mismatch"
        save_manifest(args.session_id, manifest)
        die("sha256 mismatch", 5)

    try:
        json.loads(raw.decode("utf-8"))
    except Exception as exc:
        manifest["verified"] = False
        manifest["last_error"] = f"invalid json: {exc}"
        save_manifest(args.session_id, manifest)
        die(f"invalid json: {exc}", 6)

    payload_path = session_dir(args.session_id) / manifest["filename"]
    tmp = payload_path.with_suffix(payload_path.suffix + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, payload_path)

    manifest["verified"] = True
    manifest["materialized"] = True
    manifest["payload_path"] = str(payload_path)
    manifest["last_error"] = None
    save_manifest(args.session_id, manifest)
    emit({
        "ok": True,
        "verified": True,
        "sha256": actual,
        "payload_path": str(payload_path),
        "bytes": len(raw),
    })


def cmd_status(args: argparse.Namespace) -> None:
    manifest = load_manifest(args.session_id)
    total = int(manifest["expected_chunks"])
    cdir = session_dir(args.session_id) / "chunks"
    received = []
    if cdir.is_dir():
        for path in cdir.glob("*.b64"):
            try:
                received.append(int(path.stem))
            except ValueError:
                pass
    received.sort()
    emit({
        "ok": True,
        "session_id": args.session_id,
        "received_chunks": len(received),
        "expected_chunks": total,
        "missing_chunks": [i for i in range(total) if i not in set(received)][:100],
        "verified": bool(manifest.get("verified")),
        "materialized": bool(manifest.get("materialized")),
        "expected_sha256": manifest.get("expected_sha256"),
        "actual_sha256": manifest.get("actual_sha256"),
        "payload_path": manifest.get("payload_path"),
        "last_error": manifest.get("last_error"),
    })


def cmd_cleanup(args: argparse.Namespace) -> None:
    sdir = session_dir(args.session_id)
    if not sdir.exists():
        emit({"ok": True, "already_absent": True, "session_id": args.session_id})
        return
    shutil.rmtree(sdir)
    emit({"ok": True, "removed": True, "session_id": args.session_id})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Jarvis n8n deployment receiver")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("session_id")
    init.add_argument("filename")
    init.add_argument("sha256")
    init.add_argument("chunks", type=int)
    init.set_defaults(func=cmd_init)

    append = sub.add_parser("append")
    append.add_argument("session_id")
    append.add_argument("seq", type=int)
    append.add_argument("chunk")
    append.set_defaults(func=cmd_append)

    verify = sub.add_parser("verify")
    verify.add_argument("session_id")
    verify.set_defaults(func=cmd_verify)

    status = sub.add_parser("status")
    status.add_argument("session_id")
    status.set_defaults(func=cmd_status)

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("session_id")
    cleanup.set_defaults(func=cmd_cleanup)

    return p


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
