#!/usr/bin/env python3
"""Записи реестра требований, чье свидетельство лежит в диффе.

Реестр `docs/platform/requirements/registry.md` объявлен живым: PR,
меняющий поведение, правит соответствующие записи. Проход требований находит
такой пропуск по одной записи за круг, хотя PR правит ровно те файлы, которыми
записи доказаны.

Связь перечислима: колонка "Свидетельство" называет пути, а диф называет
измененные файлы. Значит ее держит гейт, а не проход.

Разбор идет по всей строке записи, а не по одной колонке: путь, названный в
тексте требования или в примечании, - такой же повод открыть запись. Гейт
отчитывается, поэтому лишний повод дешевле пропущенного.

Гейт не блокирует и не судит: файл в свидетельстве - повод открыть запись, а не
доказательство, что она разошлась. Правка комментария и рефакторинг без смены
поведения запись не трогают. Удаление и переименование - трогают всегда: путь в
колонке свидетельства после них оборван. Решение остается за автором, и
разбирается оно стадией finding-audit скилла issue-dev.

Пути свидетельства пишутся от разных корней - от корня репозитория, решения или
приложения, - поэтому путь резолвится суффиксом по
отслеживаемому дереву, а не склейкой с фиксированным префиксом. Каталога в
токене может не быть вовсе: половина записей называет свидетельство одним именем
файла. Имя, которому в дереве отвечает больше одного файла, пропускается:
назвать запись по нему нельзя, и гейт называет такие записи отдельно.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-requirements-registry.py --base origin/develop
    ./.harness/hooks/python.cmd .harness/scripts/check/check-requirements-registry.py --changed src/App/X.cs
"""

from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import has_ref  # noqa: E402

import argparse  # noqa: E402
import re  # noqa: E402
import textwrap  # noqa: E402

from ci_common import BASE_BRANCH, ROOT, annotation, msg, run_strict, tracked  # noqa: E402

REGISTRY = "docs/platform/requirements/registry.md"

ROW = re.compile(r"^\|\s*\*\*(N?FR-\d+)\*\*\s*\|")

EVIDENCE = re.compile(
    r"[A-Za-z0-9_.\-{},]+(?:/[A-Za-z0-9_.\-{},]+)*"
    r"\.(?:csproj|razor|yaml|json|html|tsx|jsx|mjs|cjs|sql|yml|cs|ts|js|py|sh)"
    r"(?![A-Za-z0-9])"
)
PATHLIKE = re.compile(r"(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_\-]+(?![A-Za-z0-9_.\-/])")

BRACES = re.compile(r"\{([A-Za-z0-9_.\-]+(?:,[A-Za-z0-9_.\-]+)+)\}")

SYMBOL = re.compile(r"`([A-Z][A-Za-z0-9]*)(?:\.[A-Za-z0-9_]+)*`")
SYMBOL_EXTS = (".cs", ".ts", ".tsx")

ROWS, PATHS = 20, 4

AGGREGATE = 4


def suffix_index(files: list[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for path in files:
        index.setdefault(os.path.basename(path), []).append(path)
    return index


def expand(token: str) -> list[str]:
    group = BRACES.search(token)
    if not group:
        return [token]
    head, tail = token[: group.start()], token[group.end() :]
    return [path for name in group.group(1).split(",") for path in expand(head + name + tail)]


def resolve(token: str, index: dict[str, list[str]]) -> tuple[str | None, bool]:
    hits = [
        path
        for path in index.get(os.path.basename(token), [])
        if path == token or path.endswith("/" + token)
    ]
    if len(hits) == 1:
        return hits[0], False
    return None, len(hits) > 1


def evidence_map(
    index: dict[str, list[str]], dropped: dict[str, list[str]]
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    declared: dict[str, set[str]] = {}
    guessed: dict[str, set[str]] = {}
    absent: dict[str, set[str]] = {}
    unclear: dict[str, set[str]] = {}
    try:
        with open(os.path.join(ROOT, REGISTRY), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {}, absent, unclear, {}

    for line in lines:
        row = ROW.match(line)
        if not row:
            continue
        for token in EVIDENCE.findall(line):
            for candidate in expand(token):
                path, many = resolve(candidate, index)
                if not path and not many:
                    path, many = resolve(candidate, dropped)
                if path:
                    declared.setdefault(path, set()).add(row.group(1))
                else:
                    where = unclear if many else absent
                    where.setdefault(row.group(1), set()).add(candidate)

        for token in PATHLIKE.findall(line):
            path, _ = resolve(token, index)
            if not path:
                path, _ = resolve(token, dropped)
            if path:
                declared.setdefault(path, set()).add(row.group(1))

        for name in SYMBOL.findall(line):
            for extension in SYMBOL_EXTS:
                path, _ = resolve(name + extension, index)
                if not path:
                    path, _ = resolve(name + extension, dropped)
                if path:
                    guessed.setdefault(path, set()).add(row.group(1))

    aggregate = {path: rows for path, rows in declared.items() if len(rows) > AGGREGATE}
    found: dict[str, set[str]] = {}
    for source in (declared, guessed):
        for path, rows in source.items():
            if path not in aggregate:
                found.setdefault(path, set()).update(rows)
    return found, absent, unclear, aggregate


def changed_files(base: str, explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    diff = run_strict("git", "diff", "--name-only", f"{base}...HEAD", unread=msg("gate_tree_unread"))
    return [path for path in diff.splitlines() if path]


def gone(base: str, explicit: list[str] | None) -> list[str]:
    if explicit:
        return []
    old = []
    diff = run_strict("git", "diff", "--name-status", "-M", f"{base}...HEAD", unread=msg("gate_tree_unread"))
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("D"):
            old.append(parts[1])
        elif len(parts) >= 3 and parts[0].startswith("R"):
            old.append(parts[1])
    return old


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default=BASE_BRANCH, help="база сравнения диффа")
    parser.add_argument("--changed", nargs="+", metavar="PATH", help="явный список файлов")
    args = parser.parse_args()

    if not os.path.exists(os.path.join(ROOT, REGISTRY)):
        return 0

    if not args.changed and not has_ref(args.base):
        print(annotation("warning", "reqs_no_base", args.base))
        return 0

    dropped = gone(args.base, args.changed)
    evidence, absent, unclear, aggregate = evidence_map(
        suffix_index(tracked()), suffix_index(dropped)
    )
    if not evidence:
        print(annotation("warning", "reqs_unparsed", REGISTRY))
        return 0

    changed = list(dict.fromkeys(changed_files(args.base, args.changed) + dropped))
    hits: dict[str, list[str]] = {}
    for path in changed:
        for requirement in evidence.get(path, ()):
            hits.setdefault(requirement, []).append(path)

    touched = sorted(path for path in aggregate if path in set(changed))
    for path in touched:
        print(msg("reqs_aggregate", path, len(aggregate[path])))

    if not hits:
        if touched:
            print(msg("reqs_only_aggregates"))
        else:
            print(msg("reqs_untouched"))
        return 0

    outside = sum(len(names) for names in absent.values())
    shared = sum(len(names) for names in unclear.values())
    if outside or shared:
        print(msg("reqs_unresolved", outside + shared, outside, shared))
    if aggregate:
        print(msg("reqs_aggregates_dropped", len(aggregate), AGGREGATE))
    if unclear:
        names = ", ".join(sorted(unclear, key=lambda name: (len(name), name)))
        print(textwrap.fill(msg("reqs_shared_names", names), 96, initial_indent="  ", subsequent_indent="    "))

    print(annotation("notice", "reqs_touched"))
    names = sorted(hits, key=lambda name: (len(name), name))
    for requirement in names[:ROWS]:
        paths = sorted(hits[requirement])
        for number, path in enumerate(paths[:PATHS]):
            print(f"  {requirement if number == 0 else ' ' * len(requirement)}  {path}")
        if len(paths) > PATHS:
            print(msg("reqs_more_paths", ' ' * len(requirement), len(paths) - PATHS))
    if len(names) > ROWS:
        print(msg("reqs_more_rows", len(names) - ROWS))

    if REGISTRY in changed:
        print("\n" + msg("reqs_registry_in_diff", REGISTRY))
    else:
        print("\n" + msg("reqs_registry_not_in_diff", REGISTRY))
    print(msg("reqs_hint"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
