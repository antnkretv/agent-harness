#!/usr/bin/env python3
"""Диспетчер областей: путь => область, по карте .harness/project/area-map.json.

Читает список измененных путей (по одному на строку, из stdin или --file) и
печатает `<область>=true|false` для каждой области карты: строки вида
`ключ=значение` шаг CI отдает в свои выходы как есть.

Правило сопоставления - из контракта area-map.md: путь принадлежит области,
чей ПРЕФИКС он продолжает; выигрывает самый длинный совпавший префикс, а не
первый по порядку, а равный у нескольких областей - общий каталог приложений -
поднимает их все. Масок по расширению в карте нет: маска перехватывает файлы
чужих областей и делает разбор зависимым от порядка строк.

    git diff --name-only origin/develop...HEAD | ./.harness/hooks/python.cmd .harness/scripts/check/areas.py
    ./.harness/hooks/python.cmd .harness/scripts/check/areas.py --file changed.txt --explain
"""

from __future__ import annotations

import argparse
import json
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import msg  # noqa: E402

MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "project", "area-map.json")


def load_map() -> dict:
    with open(os.path.normpath(MAP_PATH), encoding="utf-8") as handle:
        return json.load(handle)


def longest(path: str, entries: list[tuple]) -> list:
    hits = [(len(prefix), entry) for entry, prefixes in entries for prefix in prefixes if path.startswith(prefix)]
    top = max((size for size, _ in hits), default=-1)
    return [entry for size, entry in hits if size == top]


def owners(path: str, areas: dict) -> set[str]:
    return set(longest(path, [(name, spec["prefixes"]) for name, spec in areas.items()]))


def shared_hits(path: str, shared: list) -> list[dict]:
    return longest(path, [(entry, entry["prefixes"]) for entry in shared])


def expand(consumers, areas: dict, deploy: set[str]) -> set[str]:
    if consumers == "all":
        return set(areas)
    if consumers == "deploy":
        return set(deploy)
    if isinstance(consumers, str):
        raise ValueError(msg("areas_unknown_consumers", repr(consumers)))
    unknown = set(consumers) - set(areas)
    if unknown:
        raise ValueError(msg("areas_unknown_areas", sorted(unknown)))
    return set(consumers)


def deploy_areas(carte: dict) -> list[str]:
    return [name for name, spec in carte["areas"].items() if spec.get("deploy")]


def resolve(changed: list[str], carte: dict) -> tuple[set[str], list[str]]:
    areas = carte["areas"]
    deploy = set(deploy_areas(carte))
    raised: set[str] = set()
    trace: list[str] = []

    for path in changed:
        if not path:
            continue
        shared = shared_hits(path, carte["shared"])
        if shared:
            targets = set().union(*(expand(entry["consumers"], areas, deploy) for entry in shared))
            raised |= targets
            named = ", ".join(sorted(targets)) if targets else msg("areas_no_consumers")
            trace.append(msg("areas_shared_layer", path, named))
            continue
        found = owners(path, areas)
        if not found:
            raised |= set(deploy)
            trace.append(msg("areas_unmapped", path))
            continue
        raised |= found
        trace.append(msg("areas_area", path, ", ".join(sorted(found))))

    return raised, trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="файл со списком путей; по умолчанию stdin")
    parser.add_argument("--explain", action="store_true", help="печатать разбор каждого пути в stderr")
    args = parser.parse_args()

    source = open(args.file, encoding="utf-8") if args.file else sys.stdin
    changed = [line.strip() for line in source if line.strip()]
    if args.file:
        source.close()

    carte = load_map()
    raised, trace = resolve(changed, carte)

    if args.explain:
        for line in trace:
            print(line, file=sys.stderr)

    for name in carte["areas"]:
        print(f"{name}={'true' if name in raised else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
