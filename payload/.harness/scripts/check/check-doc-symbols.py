#!/usr/bin/env python3
"""Имена из документов области существуют в коде.

Документ фичи называет классы, компоненты и константы обратными кавычками:
`OrderPricingPolicy`, `MAX_RETRY_COUNT`, `GROUP_CONFIG`. Код
переименовывают, документ остается прежним, и читатель ищет то, чего нет.
Расхождение это перечислимое: множество документов области конечно, а
существование имени проверяется поиском по дереву. Значит его держит гейт, а
не проход ревью - тот находил такие строки по одной за круг и не сходился.

Охват узкий намеренно: префиксы `docs.liveDocPrefixes` манифеста (по умолчанию
`docs/features/`) и `docs/` приложений манифеста, кроме документов, которые
ставит комплект, - документы, которые описывают действующий код. Спеки и ADR не проверяются: спека
описывает замысел, чей код может быть еще не написан, а ADR это решение с датой.

Правило существования мягкое: имя обязано встретиться подстрокой хоть в одном
отслеживаемом файле не-документе. Так `AGENT_RUNS_ON` находится внутри
`vars.AGENT_RUNS_ON`, а имя, живущее только во внешнем пакете, считается отсутствующим.

Источником имен идет рукописный код. Проза (`.md`, `.txt`) и сгенерированное
исключены: старое имя живет в миграции EF вечно и прикрыло бы переименование
сущности. У сгенерированного зачитывается имя файла - имя миграции живет в нем.

Прогон идет по всему охвату: диффового режима у гейта нет - имя пропадает из
кода правкой кода, а не правкой документа, и обход дерева стоит секунды.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-doc-symbols.py --all
    ./.harness/hooks/python.cmd .harness/scripts/check/check-doc-symbols.py --files docs/features/<фича>/README.md
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import LIVE_DOC_PREFIXES, ROOT, generated, get_list, kit_owned, msg, tracked  # noqa: E402
from generated_sources import authored  # noqa: E402

SCOPE = LIVE_DOC_PREFIXES + tuple(f"{root}/docs/" for path in get_list("apps[].path", [])
                                if (root := posixpath.normpath(path)) != ".")
SPAN = re.compile(r"`([^`\n]{2,80})`")
UPPER = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
PASCAL = re.compile(r"^(?=(?:[^A-Z]*[A-Z]){2})(?=.*[a-z])[A-Z][A-Za-z0-9]*$")


def read(path: str) -> str:
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError:
        return ""


def names(path: str) -> list[str]:
    found = []
    for match in SPAN.finditer(read(path)):
        token = match.group(1).strip()
        if UPPER.match(token) or PASCAL.match(token):
            found.append(token)
    return found


BINARY = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2",
          ".ttf", ".pdf", ".zip", ".gz", ".webp", ".mp4", ".drawio")
PROSE = (".md", ".txt")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def code_files(paths: list[str]) -> list[str]:
    return [p for p in paths if not p.endswith(PROSE) and not p.endswith(BINARY)]


def code_tokens(paths: list[str]) -> tuple[list[str], set[str]]:
    seen: set[str] = set()
    kept: list[str] = []
    for path in paths:
        text = read(path)
        if generated(path, text):
            seen.update(TOKEN.findall(re.sub(r"[^A-Za-z0-9]+", " ", path)))
            continue
        kept.append(path)
        seen.update(TOKEN.findall(text))
    return kept, seen


def inside_longer(paths: list[str], unknown: set[str]) -> set[str]:
    left = set(unknown)
    for path in paths:
        if not left:
            break
        text = read(path)
        for name in list(left):
            if name in text:
                left.discard(name)
    return left


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="все документы охвата")
    scope.add_argument("--files", nargs="+", metavar="PATH", help="явный список документов")
    args = parser.parse_args()

    files = tracked()
    owned = kit_owned()
    docs = [path for path in (args.files or files)
            if path.startswith(SCOPE) and path.endswith(".md") and authored(path, owned)]
    if not docs:
        if args.files:
            print(msg("symbols_nothing"))
            return 0
        print(msg("symbols_scope_empty"))
        return 2

    sources, known = code_tokens(code_files(files))
    wanted = {name for path in docs for name in names(path)}
    absent_all = inside_longer(sources, wanted - known)

    missing: dict[str, list[str]] = {}
    for path in docs:
        absent = sorted(set(names(path)) & absent_all)
        if absent:
            missing[path] = absent

    if missing:
        for path, absent in sorted(missing.items()):
            print(f"{path}: {len(absent)}")
            for name in absent:
                print(f"  {name}")
        print()
        print(msg("symbols_missing", len(missing)))
        print(msg("symbols_hint"))
        return 1

    print(msg("symbols_clean", len(docs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
