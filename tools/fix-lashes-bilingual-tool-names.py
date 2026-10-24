#!/usr/bin/env python3
"""Patch construction-time bilingual tools to the real VPS Lashes workflow name.

This modifies only the local helper scripts on the VPS. It does not call n8n or
WordPress and does not modify any workflow.
"""

from pathlib import Path

TARGETS = [
    Path('/opt/taoyuan/bin/build-lashes-bilingual-candidate.py'),
    Path('/opt/taoyuan/bin/validate-lashes-bilingual-candidate.py'),
]
OLD = 'LASHES_NAME = "Lashes - Production Draft"'
NEW = 'LASHES_NAME = "Lashes - Production Draft VPS"'

for path in TARGETS:
    text = path.read_text(encoding='utf-8')
    count = text.count(OLD)
    if count == 1:
        text = text.replace(OLD, NEW, 1)
        path.write_text(text, encoding='utf-8')
        print(f'PATCHED {path}')
    elif NEW in text:
        print(f'ALREADY_PATCHED {path}')
    else:
        raise SystemExit(f'PRECONDITION_FAILED {path}: expected exact old name once, found {count}')

print('LASHES_BILINGUAL_TOOL_NAME_FIX=PASS')
