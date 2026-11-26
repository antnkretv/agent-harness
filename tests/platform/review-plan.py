#!/usr/bin/env python3
"""Состав круга ревью: таблица сценариев маршрутизатора.

Маршрутизатор способен снять обязательный проход, а снятый проход автору ждать
не нужно - цена ошибки здесь молчаливо непроверенная работа. Таблица держит его
поведение: отбор по прозе, удержание блокировки, вырождение на непрочитанном
списке файлов, бюджет на текущей голове, счет серии и закрытие прохода апрувом.

    python3 .harness/tests/review-plan.py
"""

from __future__ import annotations

import contextlib
import io
import os
import runpy
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_root = Path(__file__).resolve().parents[2]
if not (_root / ".harness").is_dir():
    _root = _root.parent
for _path in (".harness/project/manifest.json", ".harness/methodology.json", ".harness/pipelines.json",
              ".harness/lib/manifest.py"):
    if not (_root / _path).is_file():
        raise SystemExit(f"Installed harness file is missing: {_root / _path}")
ROOT = str(_root)
runpy.run_path(os.path.join(ROOT, ".harness", "scripts", "script_paths.py"))
from ci_common import get
import review_policy as plan
import review_verdict as verdict
import review_local as local
import session_cost

BLOCK = "changes"
OK = "approved"
HEAD = "d0f5e7c1a2b3c4d5e6f708192a3b4c5d6e7f8091"

# Перечня путей здесь нет: таблица, повторяющая манифест, проверяла бы саму себя.
CASES = [
    ("проза круг не поднимает", [], ["docs/features/orders/README.md"], False),
    ("проза и код - поднимает", [], ["docs/README.md", "src/App/Foo.cs"], True),
    ("машинный текст прозой не считается", [], [".agents/skills/issue-dev/SKILL.md"], True),
    ("файл круга прозой не считается", [], ["REVIEW.md"], True),
    ("свое исключение прохода", [], ["AGENTS.md"], True),
    ("пустой список файлов - диф непрозаический", [], [], True),
    ("удержание сильнее отбора", [(BLOCK, "aaa1111")], ["docs/README.md"], True),
    ("снятая блокировка отбор не переживает", [(BLOCK, "a"), (OK, "b")], ["docs/README.md"], False),
]

failed = 0
checked = 0


def report(name: str, want, got) -> None:
    global failed, checked
    checked += 1
    if want == got:
        print(f"  ok    {name}")
        return
    failed += 1
    print(f"  FAIL  {name}\n        ждали: {want}\n        было:  {got}", file=sys.stderr)


def run(marks: list[tuple[str, str]], changed: list[str]) -> tuple[dict, str]:
    return run_rows([(state, f"Ревью архитектуры: {sha}", sha) for state, sha in marks], changed)


def run_rows(rows: list[tuple[str, str, str]], changed: list[str]) -> tuple[dict, str]:
    with contextlib.redirect_stdout(io.StringIO()):
        routes = plan.route([plan.Mark(*row) for row in rows], changed, HEAD)
    decided = {f"run_{item.name}": "true" if item.run else "false" for item in routes}
    return decided, "\n".join(item.stalled for item in routes if item.stalled)


print("review-plan.py - отбор проходов:")
for name, marks, changed, expected in CASES:
    decided, _ = run(marks, changed)
    report(name, "true" if expected else "false", decided.get("run_architecture"))

# Документационный диф оставляет круг одному проходу требований: кода и
# архитектуры в нем нет, а живой документ разбирают именно требования.
decided, _ = run([], ["docs/features/orders/README.md"])
report("проза проход кода снимает", "false", decided.get("run_code"))
report("проза проход требований не снимает", "true", decided.get("run_requirements"))

# Спека - проза отбора: общий с линтом список звал ее машинным текстом.
# Каталога трека тут нет: путь specs/NNN-слаг падет после его архивации.
decided, _ = run([], [".specify/feature.json", "specs/README.md"])
report("спека прозой считается", "false", decided.get("run_architecture"))
report("спека проход кода снимает", "false", decided.get("run_code"))
report("спека проход требований не снимает", "true", decided.get("run_requirements"))

# Файл поведения агента прозой не становится: правка конституции меняет разбор.
decided, _ = run([], [".specify/memory/constitution.md"])
report("файл поведения прозой не считается", "true", decided.get("run_architecture"))

# Апрув постоянен: новые коммиты прохода с апрувом не поднимают.
decided, _ = run([(OK, "aaa1111")], ["src/App/Foo.cs"])
report("апрув на прошлой голове проход не поднимает", "false", decided.get("run_architecture"))
report("апрув одного прохода соседей не трогает", "true", decided.get("run_code"))

ok_word = verdict.STATE["approve"][1]
block_word = verdict.STATE["changes"][1]

# Отзыв площадка уводит в комментарий, когда учетной записи прогона он запрещен:
# такой апрув удаленный, и проход он закрывает. Локальный разобран ниже.
decided, _ = run_rows([("commented", f"Ревью архитектуры. {ok_word}.", HEAD)],
                      ["src/App/Foo.cs"])
report("удаленный апрув комментарием закрывает проход", "false", decided.get("run_architecture"))

decided, _ = run([(OK, "aaa1111"), (BLOCK, "bbb2222")], ["src/App/Foo.cs"])
report("правки после апрува проход поднимают", "true", decided.get("run_architecture"))

# Вердикт без слова решения и без апрува: отказ остается только от бюджета.
# С апрувом сценарий закрывался бы вторым правилом, и регрессия бюджета
# прошла бы молча.
decided, _ = run_rows([("commented", f"Ревью архитектуры: {HEAD}", HEAD)],
                      ["src/App/Foo.cs"])
report("проход сверх бюджета на этой голове не идет", "false", decided.get("run_architecture"))

decided, _ = run([(OK, HEAD)], ["src/App/Foo.cs"])
report("апрув текущей головы закрывает проход", "false", decided.get("run_architecture"))
marker = next(entry["marker"] for entry in plan.passes() if entry["id"] == "architecture")
current = plan.Mark(OK, marker, HEAD, base="base-a", policy="policy-a", task="task-a")
report("другие база, политика и постановка наследуют апрув", OK,
       plan.counts([current], marker, HEAD, "base-b", "policy-b", "task-b")[1])

series = [(BLOCK, f"sha{n}") for n in range(5)]
_, stalled = run(series, ["src/App/Foo.cs"])
report("серия правок дает сообщение о застое", True, "застоем считается" in stalled)
report("голова сообщения несет sha эпизода", True, 'Проход **Ревью архитектуры** просит правки в каждом круге начиная с коммита **sha0**' in stalled)

_, stalled = run([(BLOCK, "a"), (OK, "b")] + [(BLOCK, f"sha{n}") for n in range(5)],
                 ["src/App/Foo.cs"])
report("апрув в середине счет серии обнуляет", True, 'Проход **Ревью архитектуры** просит правки в каждом круге начиная с коммита **sha0**' in stalled)

# Отказ площадки в запросе правок уводит вердикт в комментарий: удержание держит
# слово в первой строке тела, иначе круг снял бы блокировку без разбора.
decided, _ = run_rows([("commented", f"Ревью архитектуры. {block_word}.", HEAD)], ["src/App/Foo.cs"])
report("правки сверх бюджета не выдают апрув", "false", decided.get("run_architecture"))

# Локальный круг прошлых вердиктов не знает и поднимает все проходы, поэтому его
# апрув выдан не глядя на удержание: закрыть им проход значит потерять находку.
MARK = get("processes.review.fallback.mark", "", file=plan.PIPELINE)


def local_head(state: str) -> str:
    """Заголовок локального вердикта, как его видят читатели, у того, кто вердикт пишет.

    Своей копии таблица не держит: разойдясь с публикатором, копия разбирала бы
    вердикт, которого круг уже не пишет.
    """
    stamp = {"SHA": HEAD, "Task": "0" * 64, "Provider": "claude-code"}
    return plan.headline(local.verdict_body({"marker": "Ревью архитектуры"}, MARK, "", state, "", stamp,
                                                     session_cost.values({}, None), "TestResults"))


head_ok = local_head("approve")
head_block = local_head("changes")

report("решение стоит в хвосте головы - там, где его ищет маршрутизатор", True,
       bool(plan.APPROVE.search(head_ok)) and bool(plan.CHANGES.search(head_block)))
report("локальный вердикт опознан", True, plan.local(head_ok))
report("удаленный вердикт локальным не считается", False, plan.local(f"Ревью архитектуры. {ok_word}."))

decided, _ = run_rows([("commented", head_ok, "aaa1111")], ["src/App/Foo.cs"])
report("локальный апрув проход не закрывает", "true", decided.get("run_architecture"))

decided, _ = run_rows([(BLOCK, "Ревью архитектуры: aaa1111", "aaa1111"),
                       ("commented", head_ok, "bbb2222")], ["src/App/Foo.cs"])
report("локальный апрув удержание не снимает", "true", decided.get("run_architecture"))

decided, _ = run_rows([("commented", head_block, "aaa1111")], ["src/App/Foo.cs"])
report("локальные правки держат", "true", decided.get("run_architecture"))

# Слова решения объявляет манифест, а читают четверо: таблица держит, что
# никто из них не завел свою копию.
report("слова у локального круга те же", {ok_word, block_word}, set(local.STATE_WORD.values()))
report("слово правок у маршрутизатора то же", True, block_word in plan.CHANGES.pattern)
report("слово апрува у маршрутизатора то же", True, ok_word in plan.APPROVE.pattern)

print()
if failed:
    print(f"review-plan: провалено {failed} из {checked}", file=sys.stderr)
    sys.exit(1)
print(f"review-plan: {checked} проверок пройдено")
