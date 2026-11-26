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

# Правка после апрува всех проходов: без нового круга проходит только маленькая
# правка файлов, которые назвал упавший Check, или файлов сборки и тестов.
import subprocess
import tempfile

import post_approval


def committed(repo: str, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = Path(repo, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message],
                   check=True, capture_output=True)
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def approved_rows(sha: str) -> list[tuple[str, str, str]]:
    return [(OK, f"{entry['marker']}: {sha}", sha) for entry in plan.passes()]


def git_in(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def planned(repo: str, rows: list, files: list[str], head: str, log: str | None, fetch: str = "",
            branch: str = "") -> dict:
    post_approval.ROOT, post_approval.named = repo, lambda *_: log
    post_approval.judge.cache_clear()
    with contextlib.redirect_stdout(io.StringIO()):
        routes = plan.route([plan.Mark(*row) for row in rows], files, head, fetch=fetch, branch=branch)
    return {item.name: item for item in routes}


def fixed(files: dict[str, str], log: str | None) -> dict:
    with tempfile.TemporaryDirectory() as repo:
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
        approved = committed(repo, {"src/App/Foo.cs": "a\n", "src/App/Bar.cs": "b\n"}, "base")
        head = committed(repo, files, "fix")
        return planned(repo, approved_rows(approved), list(files), head, log)


def running(routes: dict) -> set[str]:
    return {name for name, item in routes.items() if item.run}


everyone = {entry["id"] for entry in plan.passes()}
limit = post_approval.policy()["lines"]
named_log = "error src/App/Foo.cs:1: CS1002"
routes = fixed({"src/App/Foo.cs": "a;\n"}, named_log)
report("правка названного Check файла апрувы не снимает", set(), running(routes))
routes = fixed({"src/App/Bar.cs": "b;\n"}, named_log)
report("правка неназванного файла поднимает все проходы", everyone, running(routes))
report("новый круг разбирает дифф от одобренной головы", True, all(item.since for item in routes.values()))
routes = fixed({"src/App/Foo.cs": "".join(f"{n}\n" for n in range(limit))}, named_log)
report("правка от предела строк поднимает проходы", True, all(item.run for item in routes.values()))
routes = fixed({"tests/App/FooTests.cs": "t\n"}, named_log)
report("правка теста без упоминания в журнале апрувы не снимает", False, any(item.run for item in routes.values()))
routes = fixed({"src/App/Foo.cs": "a;\n"}, None)
report("непрочитанный журнал Check правку не принимает", everyone, running(routes))

with tempfile.TemporaryDirectory() as repo:
    subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
    approved = committed(repo, {"src/App/Foo.cs": "a\n", "src/App/Bar.cs": "b\n"}, "base")
    head = committed(repo, {"src/App/Bar.cs": "b;\n"}, "fix")
    first = plan.passes()[0]
    rows = approved_rows(approved) + [(OK, f"{first['marker']}: {head}", head)]
    routes = planned(repo, rows, ["src/App/Bar.cs"], head, named_log)
    report("апрув новой головы одним проходом апрувы других не продлевает", everyone - {first["id"]}, running(routes))
    rows = [(OK, f"{first['marker']}: {approved}", approved)] + [
        (OK, f"{entry['marker']}: {head}", head) for entry in plan.passes()[1:]]
    routes = planned(repo, rows, ["src/App/Bar.cs"], head, named_log)
    report("апрув, выданный до апрува всех проходов, покрывает голову общего апрува", set(), running(routes))

with tempfile.TemporaryDirectory() as root:
    origin, clone = os.path.join(root, "origin"), os.path.join(root, "clone")
    subprocess.run(["git", "init", "-q", "-b", "develop", origin], check=True, capture_output=True)
    base = committed(origin, {"src/App/Foo.cs": "a\n", "src/App/Bar.cs": "b\n"}, "base")
    git_in(origin, "checkout", "-q", "-b", "feature")
    approved = committed(origin, {"src/App/Foo.cs": "a1\n"}, "change")
    head = committed(origin, {"src/App/Foo.cs": "a1;\n"}, "fix")
    git_in(origin, "checkout", "-q", "develop")
    subprocess.run(["git", "clone", "-q", "--depth=1", "--branch=develop", f"file://{origin}", clone], check=True,
                   capture_output=True)
    routes = planned(clone, approved_rows(approved), ["src/App/Foo.cs"], head, named_log, "refs/heads/feature")
    report("план в CI на мелком клоне базы дозагружает коммиты PR и правку принимает", set(), running(routes))

    full = os.path.join(root, "full")
    subprocess.run(["git", "clone", "-q", f"file://{origin}", full], check=True, capture_output=True)
    committed(origin, {"src/App/Other.cs": "".join(f"{n}\n" for n in range(limit))}, "base moved")
    git_in(origin, "checkout", "-q", "feature")
    git_in(origin, "merge", "-q", "--no-edit", "develop")
    merged = git_in(origin, "rev-parse", "HEAD")
    routes = planned(full, approved_rows(approved), ["src/App/Foo.cs"], merged, named_log, "refs/heads/feature",
                     "develop")
    report("слияние базы без ручного разрешения в правку не входит", set(), running(routes))
    report("дозагрузка в полном клоне не делает его мелким", "false",
           git_in(full, "rev-parse", "--is-shallow-repository"))

    git_in(origin, "checkout", "-q", "-b", "side", base)
    committed(origin, {"src/App/Evil.cs": "".join(f"{n}\n" for n in range(limit * 10))}, "side")
    git_in(origin, "checkout", "-q", "feature")
    git_in(origin, "merge", "-q", "--no-edit", "side")
    smuggled = git_in(origin, "rev-parse", "HEAD")
    routes = planned(full, approved_rows(approved), ["src/App/Evil.cs"], smuggled, named_log, "refs/heads/feature",
                     "develop")
    report("слияние ветки не из базы после апрува поднимает проходы", everyone, running(routes))

events = [(entry["id"], "approve", "H1") for entry in plan.passes()]
first, second = plan.passes()[0]["id"], plan.passes()[1]["id"]
events += [(first, "changes", "H2"), (second, "approve", "H2"), (first, "approve", "H3")]
report("правки другого прохода не возвращают снятые апрувы", ("H3", "H2", "H1"),
       tuple(plan.covered(events).get(entry["id"]) for entry in plan.passes()))

log = "\n".join(["== text-registry: check-text-registry.py", "src/App/Bar.cs: unused key",
                 "== pipeline: check-pipeline.py", "error src/App/Foo.cs:3", "",
                 "OK   text-registry: check-text-registry.py (1 s)", "FAIL pipeline: check-pipeline.py (2 s)"])
text = post_approval.failed_output(log)
report("из журнала берется вывод только упавших гейтов", (True, False),
       (post_approval.mentioned("src/App/Foo.cs", text), post_approval.mentioned("src/App/Bar.cs", text)))
report("путь в журнале совпадает только целиком", (False, True, True),
       (post_approval.mentioned("README.md", "docs/README.md:1"), post_approval.mentioned("README.md", "./README.md:2:"),
        post_approval.mentioned("src/a.cs", "/home/runner/work/x/src/a.cs(3,1)")))

post_approval.ROOT = ROOT
post_approval.judge.cache_clear()
with contextlib.redirect_stdout(io.StringIO()):
    routes = plan.route([plan.Mark(*row) for row in approved_rows("aaa1111")], ["src/App/Foo.cs"], HEAD)
report("непрочитанный дифф правки поднимает проходы", True, all(item.run for item in routes))
decided, _ = run([(OK, "aaa1111")], ["src/App/Foo.cs"])
report("апрув одного прохода правилом правки не задет", "false", decided.get("run_architecture"))

print()
if failed:
    print(f"review-plan: провалено {failed} из {checked}", file=sys.stderr)
    sys.exit(1)
print(f"review-plan: {checked} проверок пройдено")
