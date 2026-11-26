#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, msg, workflow_files  # noqa: E402

SEARCH = re.compile(r"(?:^|[|(;&]\s*)(grep|find)\b")
GUARDED = re.compile(r"\|\|\s*(true|:|echo)\b")
FUNCTION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")
RUN_BLOCK = re.compile(r"^\s*(?:-\s+)?run: \|\s*$|^\s*-\s+\|\s*$")
CUSTOM_SHELL = re.compile(r"^\s*shell:\s*(?!.*-\w*e)(.*\{0\})")


def blocks(path: str):
    lines = open(path, encoding="utf-8").read().splitlines()
    index = 0
    while index < len(lines):
        if not RUN_BLOCK.match(lines[index]):
            index += 1
            continue
        indent = len(lines[index]) - len(lines[index].lstrip())
        body, cursor = [], index + 1
        while cursor < len(lines):
            line = lines[cursor]
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            body.append(line)
            cursor += 1
        window = lines[max(0, index - 12):index]
        if not any(CUSTOM_SHELL.match(l) for l in window):
            yield index + 2, body
        index = cursor


def statements(body: list[str]):
    buffer, first, depth = "", 0, 0
    for offset, line in enumerate(body):
        text = line.strip()
        if not buffer:
            first = offset
        buffer = f"{buffer} {text}".strip() if buffer else text
        depth += buffer.count("$(") - buffer.count(")") if not depth else 0
        continued = text.endswith("\\")
        unbalanced = buffer.count("$(") > buffer.count(")")
        if continued or unbalanced:
            buffer = buffer.rstrip("\\").strip()
            continue
        yield first, buffer
        buffer = ""


def searching(statement: str) -> bool:
    return bool(SEARCH.search(statement)) and not GUARDED.search(statement)


def risky_functions(items: list[tuple[int, str]]) -> set[str]:
    risky, name, body = set(), None, []
    for _, statement in items:
        if name is None:
            match = FUNCTION.match(statement)
            if not match:
                continue
            name, inner = match.group(1), statement[match.end():].strip()
            if inner.endswith("}"):
                body = [inner[:-1].strip().rstrip(";")]
                if body and searching(body[-1]):
                    risky.add(name)
                name = None
            else:
                body = []
            continue
        if statement.strip() == "}":
            if body and searching(body[-1]):
                risky.add(name)
            name = None
            continue
        body.append(statement)
    return risky


def problems(path: str) -> list[tuple[int, str]]:
    found = []
    for start, body in blocks(path):
        if any(re.match(r"\s*set\s+\+e\b", line) for line in body):
            continue
        items = list(statements(body))
        risky = risky_functions(items)
        for offset, statement in items:
            if "$(" not in statement or GUARDED.search(statement):
                continue
            if SEARCH.search(statement):
                kind = msg("wfshell_assignment")
            else:
                called = sorted(n for n in risky if re.search(rf"\$\(.*\b{re.escape(n)}\b", statement))
                if not called:
                    continue
                kind = msg("wfshell_function", called[0])
            found.append((start + offset, f"{kind}: {statement[:110]}"))
    return found


total = 0
failed = 0
for path in workflow_files():
    total += 1
    name = os.path.relpath(path, ROOT)
    for line, message in problems(path):
        failed += 1
        print(f"{name}:{line}: {message}")

if failed:
    print()
    print(msg("wfshell_failed", failed))
    print(msg("wfshell_hint"))
    sys.exit(1)

print(msg("wfshell_clean", total))
