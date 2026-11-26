#!/usr/bin/env python3
"""Закрытие задач после мержа: переход Jira, GitHub Issues и выбор запросов коммита.

Ошибка здесь молча оставляет задачу открытой после выката или закрывает чужую,
поэтому таблица держит выбор перехода Jira, пропуск уже закрытой задачи,
пропуск закрытия, которое делает сам GitHub, и запросы, принесенные обратным слиянием.

    python3 .harness/tests/tracker-close.py
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_root = Path(__file__).resolve().parents[2]
if not (_root / ".harness").is_dir():
    _root = _root.parent
LIB = _root / ".harness" / "lib"
for _path in ("tracker.py", "forge.py", "manifest.py"):
    if not (LIB / _path).is_file():
        raise SystemExit(f"Installed harness file is missing: {LIB / _path}")
sys.path.insert(0, str(LIB))
import forge  # noqa: E402
import tracker  # noqa: E402
from manifest import get  # noqa: E402

BASE = get("git.baseBranch", "develop")
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


def refused(call) -> str:
    try:
        call()
    except tracker.TrackerError as error:
        return str(error)
    return ""


def jira_cases() -> None:
    if not (LIB / "jira_adapter.py").is_file():
        print("  SKIP  Jira: расширение tracker/jira не установлено")
        return
    import jira_adapter

    def jira(category: str, transitions: list[dict], named: str = "") -> tuple[jira_adapter.JiraTracker, list]:
        settings = {"url": "https://jira.example.com", "projects": ["PROJ"], "doneTransition": named}
        adapter = jira_adapter.JiraTracker(settings)
        calls: list = []

        def request(method, path, query=None, payload=None, ok=(200,)):
            calls.append((method, path, payload))
            if path.endswith("/transitions") and method == "GET":
                return {"transitions": transitions}
            if method == "GET":
                return {"key": "PROJ-1", "fields": {"status": {"statusCategory": {"key": category}}}}
            return {}

        adapter._request = request
        return adapter, calls

    def step(ident: str, name: str, category: str) -> dict:
        return {"id": ident, "name": name, "to": {"statusCategory": {"key": category}}}

    adapter, calls = jira("indeterminate", [step("11", "В работу", "indeterminate"), step("31", "Готово", "done")])
    report("Jira: открытая задача закрыта", True, adapter.close_issue("PROJ-1"))
    report("Jira: переход в статус категории done", ("POST", "issue/PROJ-1/transitions", {"transition": {"id": "31"}}),
           calls[-1])

    adapter, calls = jira("done", [step("41", "Переоткрыть", "new")])
    report("Jira: закрытая задача не меняется", False, adapter.close_issue("PROJ-1"))
    report("Jira: у закрытой задачи переходы не читаются", ["GET"], [method for method, _, _ in calls])

    adapter, calls = jira("new", [step("11", "В работу", "indeterminate")])
    report("Jira: без перехода в done - отказ", True,
           "В работу" in refused(lambda: adapter.close_issue("PROJ-1")) and calls[-1][0] == "GET")

    ambiguous = [step("31", "Готово", "done"), step("51", "Не будет сделано", "done")]
    adapter, calls = jira("new", ambiguous)
    report("Jira: два перехода в done - отказ с настройкой", True,
           "doneTransition" in refused(lambda: adapter.close_issue("PROJ-1")) and calls[-1][0] == "GET")

    adapter, calls = jira("new", ambiguous, named="Не будет сделано")
    adapter.close_issue("PROJ-1")
    report("Jira: doneTransition выбирает переход", {"transition": {"id": "51"}}, calls[-1][2])

    adapter, calls = jira("new", ambiguous, named="Закрыть")
    report("Jira: переход doneTransition не найден - отказ", True,
           "Закрыть" in refused(lambda: adapter.close_issue("PROJ-1")))


def github_cases() -> None:
    if not (LIB / "github_adapter.py").is_file():
        print("  SKIP  GitHub Issues: адаптер GitHub не установлен")
        return
    import github_adapter

    class Code:
        def __init__(self, repo: str, default: str):
            self.code_repo, self.default = repo, default

        def repo(self) -> str:
            return self.code_repo

        def default_branch(self) -> str:
            return self.default

    def issues(state: str, code: Code | None) -> tuple[github_adapter.GitHubIssues, list]:
        adapter = github_adapter.GitHubIssues({"repo": "acme/app"})
        calls: list = []

        def rest(method, path, payload=None, ok=(200,)):
            calls.append((method, payload))
            return {"number": 7, "state": state, "title": "", "body": ""}

        adapter.api.rest = rest
        adapter._code = lambda: code
        return adapter, calls

    adapter, calls = issues("open", None)
    report("GitHub Issues: задача закрыта как выполненная", (True, ("PATCH", {"state": "closed", "state_reason": "completed"})),
           (adapter.close_issue("7"), calls[-1]))
    adapter, calls = issues("closed", None)
    report("GitHub Issues: закрытая задача не меняется", (False, ["GET"]), (adapter.close_issue("7"), [m for m, _ in calls]))

    adapter, _ = issues("open", Code("ACME/app", BASE))
    report("GitHub Issues: тот же репозиторий и база по умолчанию - закрывает GitHub", True, adapter.closes_natively(BASE))
    adapter, _ = issues("open", Code("acme/app", "main" if BASE != "main" else "trunk"))
    report("GitHub Issues: база не ветка по умолчанию - закрывает harness", False, adapter.closes_natively(BASE))
    adapter, _ = issues("open", Code("acme/other", BASE))
    report("GitHub Issues: задачи другого репозитория - закрывает harness", False, adapter.closes_natively(BASE))


class Forge(forge.Forge):
    name = "fake"

    def __init__(self, changes: dict[int, forge.Change]):
        self.changes = changes

    def change_in_subject(self, subject: str) -> int | None:
        found = re.search(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+) from ", subject)
        return int(found.group(1) or found.group(2)) if found else None

    def change(self, number: int) -> forge.Change | None:
        return self.changes.get(number)

    def change_ref(self, number: int | str) -> str:
        return f"#{number}"


class Tracker(tracker.Tracker):
    name = "fake"

    def __init__(self, links: dict[int, set[str]], native: bool = False, opened: set[str] = frozenset()):
        self.links, self.native, self.opened, self.closed, self.read = links, native, set(opened), [], set()
        self.asked, self.failing, self.code = 0, set(), None

    def linked_issues(self, changes: set[int], *, closing: bool) -> set[str]:
        self.read |= changes
        return {key for number in changes for key in self.links.get(number, ())}

    def close_issue(self, key: str) -> bool:
        if key in self.failing:
            raise tracker.TrackerError("HTTP 503")
        if key not in self.opened:
            return False
        self.opened.discard(key)
        self.closed.append(key)
        return True

    def closes_natively(self, base: str) -> bool:
        self.asked += 1
        return self.native


def git(repo: str, *args: str) -> str:
    identity = ["-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid", "-c", "commit.gpgsign=false"]
    return subprocess.run(["git", "-C", repo, *identity, *args], capture_output=True, text=True, check=True).stdout.strip()


def change(number: int, head: str, sha: str, base: str = BASE, state: str = "merged") -> forge.Change:
    return forge.Change(number=number, title="", body="", state=state, draft=False, labels=(), base=base, head=head,
                        head_sha=sha, fetch_ref=f"refs/pull/{number}/head", author="", url="", files=None)


def close(repo: str, sha: str, changes: dict[int, forge.Change], adapter: Tracker, before: str = "") -> str:
    forge.active = lambda: Forge(changes)
    output = io.StringIO()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            adapter.code = tracker.close_merged(adapter, sha, before)
    finally:
        os.chdir(cwd)
    return output.getvalue()


def merge_cases() -> None:
    with tempfile.TemporaryDirectory() as repo:
        git(repo, "init", "-q", "-b", BASE)
        git(repo, "commit", "-q", "--allow-empty", "-m", "chore: начало")
        start = git(repo, "rev-parse", "HEAD")
        branches = {}
        for name, subject in (("release", "fix: хотфикс (#45)"), ("feature", "feat: работа"),
                              ("stage", "fix: подзадача (#47)")):
            git(repo, "checkout", "-q", "-b", name, start)
            git(repo, "commit", "-q", "--allow-empty", "-m", subject)
            branches[name] = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", BASE)
        git(repo, "commit", "-q", "--allow-empty", "-m", "feat: задача (#46)")
        squashed = git(repo, "rev-parse", "HEAD")
        git(repo, "commit", "-q", "--allow-empty", "-m", "feat: этап (#48)")
        staged = git(repo, "rev-parse", "HEAD")
        git(repo, "merge", "-q", "--no-ff", branches["release"], "-m", "Merge pull request #50 from acme/back-merge/v1.0.1")
        merged = git(repo, "rev-parse", "HEAD")

        changes = {46: change(46, "feature/issue-12", branches["feature"]), 48: change(48, "stage/1", branches["stage"]),
                   50: change(50, "back-merge/v1.0.1", branches["release"])}
        links = {45: {"30"}, 46: {"12"}, 47: {"40"}, 48: set(), 50: set()}
        unread = tracker.msg("tracker_close_commits_unread", "\0", "").split("\0")[1].split(":")[0]

        adapter = Tracker(links, opened={"12", "30"})
        output = close(repo, squashed, changes, adapter)
        report("squash закрывает ссылки влитого запроса без предупреждения", (["12"], False),
               (adapter.closed, unread in output))
        report("сообщение называет запрос со ссылкой и мерж",
               True, tracker.msg("tracker_closed", adapter.issue_ref("12"), "#46", "#46") in output)

        adapter = Tracker(links, opened={"40"})
        output = close(repo, staged, changes, adapter)
        report("squash ветки этапа закрывает задачи ее запросов", (["40"], {47, 48}, False),
               (adapter.closed, adapter.read, unread in output))
        report("сообщение называет запрос этапа и его мерж",
               True, tracker.msg("tracker_closed", adapter.issue_ref("40"), "#47", "#48") in output)

        adapter = Tracker(links, opened={"12", "40"})
        close(repo, staged, changes, adapter, before=start)
        report("push принес два мержа - закрыты ссылки обоих", (["12", "40"], 0), (sorted(adapter.closed), adapter.code))

        adapter = Tracker(links, opened={"12", "40"})
        adapter.failing = {"12"}
        close(repo, staged, changes, adapter, before=start)
        report("отказ трекера - код 3, остальные задачи закрыты", (["40"], tracker.PARTIAL), (adapter.closed, adapter.code))

        adapter = Tracker(links, opened={"12", "40"})
        close(repo, staged, changes, adapter, before="0" * 40)
        report("нулевая голова до push - разбирается только последний коммит", ["40"], adapter.closed)

        adapter = Tracker(links, opened={"30"})
        output = close(repo, merged, changes, adapter)
        report("обратное слияние закрывает задачи хотфикса", (["30"], {45, 50}, False),
               (adapter.closed, adapter.read, unread in output))

        adapter = Tracker(links, native=True, opened={"12"})
        close(repo, squashed, changes, adapter)
        report("ссылки, которые закрывает GitHub, пропущены", ([], {46}), (adapter.closed, adapter.read))

        adapter = Tracker(links, native=True, opened={"30"})
        close(repo, merged, changes, adapter)
        report("принесенные запросы закрываются и без своих ссылок", (["30"], {45, 50}), (adapter.closed, adapter.read))
        report("без ссылок у влитого запроса поставщика кода не спрашивают", 0, adapter.asked)

        adapter = Tracker(links)
        output = close(repo, squashed, changes, adapter)
        report("закрытая задача не меняется", ([], True), (adapter.closed, adapter.issue_ref("12") in output))

        adapter = Tracker(links, opened={"12"})
        output = close(repo, squashed, {**changes, 46: change(46, "feature/issue-12", "0" * 40)}, adapter)
        report("голова запроса не читается - закрываются его ссылки с предупреждением и кодом 3",
               (["12"], True, tracker.PARTIAL), (adapter.closed, unread in output, adapter.code))

        for title, other in (("мерж в другую ветку задач не закрывает", change(46, "feature/issue-12", branches["feature"], base="release")),
                             ("незавершенный запрос задач не закрывает", change(46, "feature/issue-12", branches["feature"], state="open"))):
            adapter = Tracker(links, opened={"12"})
            close(repo, squashed, {**changes, 46: other}, adapter)
            report(title, ([], set(), 0), (adapter.closed, adapter.read, adapter.code))


print("tracker-close.py - закрытие задач после мержа:")
original = forge.active
try:
    jira_cases()
    github_cases()
    merge_cases()
finally:
    forge.active = original

print()
if failed:
    print(f"tracker-close: провалено {failed} из {checked}", file=sys.stderr)
    sys.exit(1)
print(f"tracker-close: {checked} проверок пройдено")
