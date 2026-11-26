#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, msg, workflow_files  # noqa: E402
from text_primitives import line_of  # noqa: E402

STEP = re.compile(r"^[ \t]*-[ \t]+(?:&[\w-]+[ \t]*\n[ \t]+)?name:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)


def key(name: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", name.lower())


def steps(path: str):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    for found in STEP.finditer(text):
        yield line_of(text, found.start(1)), found.group(1).strip("'\"")


spellings: dict[str, dict[str, list[str]]] = {}
total = 0
for path in workflow_files():
    total += 1
    name = os.path.relpath(path, ROOT)
    for number, step in steps(path):
        spellings.setdefault(key(step), {}).setdefault(step, []).append(f"{name}:{number}")

split = {k: v for k, v in spellings.items() if len(v) > 1}
for variants in split.values():
    print(msg("wfnames_split"))
    for spelling, places in sorted(variants.items()):
        print(f"    {spelling!r} - {', '.join(places)}")

if split:
    print()
    print(msg("wfnames_count", len(split)))
    print(msg("wfnames_doc"))
    sys.exit(1)

print(msg("wfnames_clean", total))
