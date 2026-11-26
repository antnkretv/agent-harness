#!/usr/bin/env python3
"""Счет новых тестовых сценариев в диффе.

Потолок бюджета - один сценарий на несводимое правило, и правила перечисляются
в PR по строке на сценарий (.harness/CONTRIBUTING.md §8). До этого гейта у правила не
было механической проверки вовсе: правило с потолком держалось одним суждением,
и строки набора уходили сверх бюджета по доводу об имени теста.

Считается то, что перечислимо по диффу, и счет у диалектов разный:

- .NET: `[Fact]`, `[InlineData]`, `[MemberData]`, NUnit `[Test]`, `[TestCase]`
  и `[TestCaseSource]` считаются поштучно. NUnit допускает суффикс `Attribute`
  и квалификацию `NUnit.Framework.` или `global::NUnit.Framework.`.
  C#-файлы выбираются по этим объявлениям независимо от имени каталога;
- фронтенд и сквозные сценарии: точка объявления `it`/`test`, включая `it.each`.
  Строки таблицы `each` лежат отдельными элементами массива, и по одному диффу
  без контекста они от любого другого литерала неотличимы;
- promtool: группа ряда и ожиданий по нему.

Гасится не разность количеств, а совпадение сценария: снятое объявление вычитает
добавленное, только если совпал весь блок - строка объявления и все строки до
следующего объявления. Одной строки мало: у `[Fact]` и у `- interval:`
аргументов нет, все вхождения дали бы один ключ, и взаимозачет выродился бы в ту
же разность количеств - снятый файл с тремя `[Fact]` погасил бы три новых
сценария, ровно тот случай, ради которого гейт заведен. Соседней строки тоже
мало: за `- interval:` у promtool всегда идет `input_series:`.

Блок несет то, чем сценарии различаются: сигнатуру метода и правленое тело у
xUnit и NUnit, ряд и ожидания у promtool. Подряд идущие объявления делят тело, и оно
достается каждому: иначе у всех строк набора, кроме последней, ключом была бы
одна строка атрибута, и `[InlineData(true)]` новой теории гасился бы таким же,
снятым в другом файле того же диффа. Перенос и переформатирование так уходят в
ноль, перепись дает разницу.

Строки динамических источников `[MemberData]` и `[TestCaseSource]`, таблицы
`each` и несколько кейсов внутри одного метода добирает аудит
тестов - стадия test-audit скилла issue-dev, и правило потолка считает их наравне
(`docs/platform/standards/test-usefulness.md`).

Границей число не объявляется ни снизу, ни сверху: неперечислимый остаток его
занижает, а перенос с правкой внутри блока не гасится и завышает. Это счет
новых объявлений с зачетом перенесенных - отправная точка для автора, а не
величина, от которой можно считать отклонение.

Полезность сценария гейт не судит и работу не блокирует: защищает ли сценарий
бизнес-логику из существующего кейса, из диффа не выводится.

Ноль новых сценариев - нормальный результат задачи, и на нем гейт печатает одну
строку.

`Architecture.Tests` не считаются: это исполняемые правила архитектуры, а не
сценарии бизнес-логики, и бюджет теста их не касается
(`docs/platform/standards/test-usefulness.md`).

    ./.harness/hooks/python.cmd .harness/scripts/check/check-test-budget.py --base origin/develop
    ./.harness/hooks/python.cmd .harness/scripts/check/check-test-budget.py --range <sha>~1 <sha>
"""

from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import has_ref  # noqa: E402

import argparse  # noqa: E402
import re  # noqa: E402
from collections import Counter  # noqa: E402

from ci_common import BASE_BRANCH, TEST_PATH, annotation, msg, payload, run_strict  # noqa: E402

ARCHITECTURE_GATES = re.compile(r"(^|/)Architecture\.Tests/")

DOTNET = re.compile(
    r"^\s*\[\s*(?:(?:Fact|InlineData|MemberData)\b|"
    r"(?:(?:global::)?NUnit\.Framework\.)?(?:Test|TestCase|TestCaseSource)"
    r"(?:Attribute)?(?=\s*(?:\(|\])))"
)
NODE = re.compile(r"^\s*(?:it|test)\s*(?:\.\s*each\s*(?:\(|`)|\()")
PROMTOOL = re.compile(r"^\s*-\s*interval\s*:")

HUNK = "\x01"

FILES = 20


def changes(diff: str) -> dict[str, tuple[list[str], list[str]]]:
    found: dict[str, tuple[list[str], list[str]]] = {}
    path = previous = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = previous = ""
        elif line.startswith("--- "):
            previous = line[6:] if line.startswith("--- a/") else ""
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else previous
        elif path and line.startswith("@@"):
            for side in found.setdefault(path, ([], [])):
                side.append(HUNK)
        elif path and line.startswith("+"):
            found.setdefault(path, ([], []))[0].append(line[1:])
        elif path and line.startswith("-"):
            found.setdefault(path, ([], []))[1].append(line[1:])
    return found


def scenarios(path: str, gained: list[str], lost: list[str]) -> tuple[list[str], list[str]]:
    if path.endswith(".cs"):
        pattern = DOTNET
    elif path.endswith((".yml", ".yaml")):
        pattern = PROMTOOL
    else:
        pattern = NODE

    def declared(lines: list[str]) -> list[str]:
        starts = [i for i, line in enumerate(lines) if pattern.match(line)]
        flat = ["".join(line.split()) for line in lines]
        keys = []
        for number, start in enumerate(starts):
            last = number
            while last + 1 < len(starts) and starts[last + 1] == starts[last] + 1:
                last += 1
            tail = starts[last] + 1
            end = starts[last + 1] if last + 1 < len(starts) else len(lines)
            stop = next((i for i in range(tail, end) if lines[i] == HUNK), end)
            keys.append("\x00".join([flat[start]] + flat[tail:stop]))
        return keys

    return declared(gained), declared(lost)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default=BASE_BRANCH, help="база сравнения диффа")
    parser.add_argument("--range", nargs=2, metavar=("FROM", "TO"), help="явный диапазон")
    args = parser.parse_args()

    if not args.range and not has_ref(args.base):
        print(annotation("warning", "budget_no_base", args.base))
        return 0

    span = args.range if args.range else [f"{args.base}...HEAD"]
    diff = run_strict(
        "git", "diff", "--unified=0", "--no-color",
        "--src-prefix=a/", "--dst-prefix=b/", *span,
        unread=msg("budget_diff_unread"),
    )

    counted = {
        path: pair
        for path, (gained, lost) in changes(diff).items()
        if (path.endswith(".cs") or TEST_PATH.search(path))
        and not ARCHITECTURE_GATES.search(path)
        and not payload(path)
        and any(pair := scenarios(path, gained, lost))
    }
    entered = Counter(name for gained, _ in counted.values() for name in gained)
    removed = Counter(name for _, lost in counted.values() for name in lost)
    total = sum((entered - (entered & removed)).values())

    if not total:
        print(msg("budget_none"))
        return 0

    print(annotation("notice", "budget_count", total))
    paths = sorted(counted)
    for path in paths[:FILES]:
        gained, lost = counted[path]
        print(f"  {len(gained):>3}{f' -{len(lost)}' if lost else '   '}  {path}")
    if len(paths) > FILES:
        print(msg("budget_more_files", len(paths) - FILES))
    print("\n" + msg("budget_hint"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
