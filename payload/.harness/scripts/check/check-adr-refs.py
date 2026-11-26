#!/usr/bin/env python3
"""Ссылка на решение несет область: счетчик номеров сквозной, каталог областной.

Правило - реестр docs/platform/adr/README.md.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-adr-refs.py                 # все отслеживаемое дерево
    ./.harness/hooks/python.cmd .harness/scripts/check/check-adr-refs.py --files a.cs b.md

Номер без области указывает на два разных решения, если один номер занят в
двух областях. Область снимает догадку, а гейт не дает правилу остаться без
исполнения: разовый обход за полгода набрал бы те же сотни нарушений.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ADR_TREE, ROOT, generated_path, get_list, git, listed, msg, payload  # noqa: E402


def tree_areas() -> tuple[str, ...]:
    folder = os.path.join(ROOT, ADR_TREE)
    try:
        return tuple(sorted(name for name in os.listdir(folder) if os.path.isdir(os.path.join(folder, name))))
    except OSError:
        return ()


AREAS = tree_areas() or ("platform",)

SKIP = (f"{ADR_TREE}/", *get_list("docs.historicalPrefixes", ()))

CITE = re.compile(r"ADR-(\d{4})")
LINK = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
TARGET = re.compile(rf"adr/({'|'.join(AREAS)})/(\d{{4}})-")
AFTER = re.compile(
    rf"\A[\s,)]*(?:п\.\s?\d+[\s,)]*)?(?:области\s+)?({'|'.join(AREAS)})(?![\w-])")
BEFORE = re.compile(rf"({'|'.join(AREAS)})[\s/]$")
CONTINUED = re.compile(r"^\s*(?:///|//|#|--|\*)?\s*")


def areas() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for area in AREAS:
        folder = os.path.join(ROOT, ADR_TREE, area)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            head = re.match(r"(\d{4})-", name)
            if head:
                found.setdefault(head.group(1), set()).add(area)
    return found


def named(lines: list[str], index: int) -> list[tuple[int, str]]:
    line = lines[index]
    hits = [(m, TARGET.search(m.group(1))) for m in LINK.finditer(line)]
    spans = [(m.start(), m.end()) for m, target in hits if target]
    linked = {target.group(2) for _m, target in hits if target}

    bare = []
    for cite in CITE.finditer(line):
        if any(start <= cite.start() < end for start, end in spans):
            continue
        if cite.group(1) in linked:
            continue
        if BEFORE.search(line[: cite.start()]):
            continue
        rest = line[cite.end():]
        tail = rest
        if not rest.strip() or rest.rstrip().endswith(r"области"):
            following = lines[index + 1] if index + 1 < len(lines) else ""
            tail = f"{rest} {CONTINUED.sub(' ', following)}"
        if AFTER.match(tail):
            continue
        bare.append((cite.start(), cite.group(1)))
    return bare


def inspect(paths: list[str]) -> list[str]:
    area_of = areas()
    problems = []
    for path in paths:
        if path.startswith(SKIP) or payload(path) or generated_path(path):
            continue
        try:
            lines = open(os.path.join(ROOT, path), encoding="utf-8").read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for index in range(len(lines)):
            for _column, number in named(lines, index):
                where = sorted(area_of.get(number, []))
                hint = "/".join(where) if where else msg("adr_not_registered")
                problems.append(msg("adr_no_area", path, index + 1, number, hint))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--files", nargs="+", metavar="PATH", help="явный список файлов")
    scope.add_argument("--docs", action="store_true", help="только документы Markdown: этап 2 установки harness")
    args = parser.parse_args()

    paths = args.files or [path for path in listed() if not args.docs or path.endswith((".md", ".mdc"))]
    problems = inspect(paths)
    if not problems:
        print(msg("adr_clean", len(paths)))
        return 0

    for item in problems:
        print(item)
    print(msg("adr_failed", len(problems), ADR_TREE))
    return 1


if __name__ == "__main__":
    sys.exit(main())
