from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from commit_text import TYPES as COMMIT_TYPES  # noqa: E402
from manifest import adapter, get  # noqa: E402
from platform_messages import msg  # noqa: E402
from rest import emit  # noqa: E402


class TrackerError(RuntimeError):
    def __init__(self, why: str, status: int | None = None):
        super().__init__(why)
        self.status = status


class TrackerUnavailable(TrackerError):
    pass


FLOWS = {"feat": "feature", "fix": "bugfix"}
SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SLUG_LIMIT = 64
CYRILLIC = dict(zip(map(chr, (*range(0x430, 0x450), 0x451)),
                    ("a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
                     "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "e")))


def gitflow() -> bool:
    return get("git.branchConvention", "conventional") == "gitflow"


def branch_kinds() -> tuple[str, ...]:
    return ("feature", "bugfix", "hotfix", "chore") if gitflow() else COMMIT_TYPES


def branch_kind(commit_type: str, base: str = "") -> str:
    if not gitflow():
        return commit_type
    if commit_type == "fix" and base.removeprefix("origin/") == get("git.releaseBranch", ""):
        return "hotfix"
    return FLOWS.get(commit_type, "chore")


def valid_slug(slug: str) -> bool:
    return re.fullmatch(SLUG, slug) is not None and len(slug) <= SLUG_LIMIT


def branch_name(commit_type: str, slug: str, issue: str = "", base: str = "") -> str:
    return f"{branch_kind(commit_type, base)}/{issue + '-' if issue else ''}{slug}"


def slug_of(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", "".join(CYRILLIC.get(letter, letter) for letter in text.lower()))
    slug = ""
    for word in words:
        if len(slug) + len(word) + bool(slug) > SLUG_LIMIT:
            break
        slug = f"{slug}-{word}" if slug else word
    return slug


@dataclass(frozen=True)
class Reference:
    number: str
    closing: bool
    phrase: str


@dataclass(frozen=True)
class IssueBranches:
    id: str
    linked: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Issue:
    key: str
    title: str
    body: str
    url: str
    labels: tuple[str, ...]
    created: str
    open: bool
    assignees: tuple[str, ...] = ()
    author: str = ""


@dataclass(frozen=True)
class IssueNote:
    id: str
    author: str
    created: str
    body: str
    bot: bool = False
    system: bool = False
    updated: str = ""


class Tracker:
    name = ""
    html_comments = True
    key = ""

    def missing(self) -> str:
        return msg("tracker_off")

    def _refuse(self):
        raise TrackerUnavailable(self.missing())

    def repo(self) -> str:
        return ""

    def issue_url(self, number: int | str) -> str:
        return ""

    def issue_ref(self, number: int | str) -> str:
        return str(number)

    def references(self, text: str) -> list[Reference]:
        return []

    def link_phrase(self, number: int | str, closing: bool) -> str:
        return f"{'Closes' if closing else 'Refs'} {self.issue_ref(number)}" if self.name else ""

    def valid_key(self, issue: str) -> bool:
        return (bool(self.key) and re.fullmatch(self.key, issue) is not None
                and not (issue.isdecimal() and (int(issue) == 0 or str(int(issue)) != issue)))

    def branch_parts(self, branch: str) -> tuple[str, str] | None:
        kind, _, rest = branch.partition("/")
        if kind not in branch_kinds():
            return None
        found = re.fullmatch(rf"({self.key})-({SLUG})", rest) if self.key else None
        if found and self.valid_key(found.group(1)) and valid_slug(found.group(2)):
            return found.group(1), found.group(2)
        return ("", rest) if valid_slug(rest) else None

    def branch_issue(self, branch: str) -> str | None:
        parts = self.branch_parts(branch)
        return parts[0] if parts and parts[0] else None

    def issueless_branch(self, commit_type: str, slug: str, base: str = "") -> str:
        name = branch_name(commit_type, slug, base=base)
        return name if valid_slug(slug) and self.branch_issue(name) is None else ""

    def sample_key(self) -> str:
        return ""

    def closing_issues(self, changes: set[int]) -> set[str]:
        return self.linked_issues(changes, closing=True)

    def linked_issues(self, changes: set[int], *, closing: bool) -> set[str]:
        if not self.name:
            return set()
        import forge
        from markdown_text import without_code

        found: set[str] = set()
        for change in sorted(changes):
            try:
                body = forge.change_body(change)
            except forge.ForgeError as error:
                if error.status != 404:
                    raise
                continue
            found |= {reference.number for reference in self.references(without_code(body)) if reference.closing == closing}
        return found

    def issue_branches(self, number: str) -> IssueBranches | None:
        self._refuse()

    def link_branch(self, issue: str, sha: str, name: str) -> tuple[str, str]:
        self._refuse()

    def unlink_branch(self, link: str) -> None:
        self._refuse()

    def issue(self, key: str) -> Issue | None:
        self._refuse()

    def close_issue(self, key: str) -> bool:
        found = self.issue(key)
        if found is None:
            raise TrackerError(msg("tracker_issue_missing", key), 404)
        if not found.open:
            return False
        self._close(key)
        return True

    def _close(self, key: str) -> None:
        self._refuse()

    def closes_natively(self, base: str) -> bool:
        return False

    def find_issues(self, label: str, open_only: bool = True, closed_pages: int | None = None) -> list[Issue]:
        self._refuse()

    def search_issues(self, label: str, text: str) -> tuple[list[Issue], bool]:
        self._refuse()

    def create_issue(self, title: str, body: str, labels: Sequence[str]) -> Issue:
        self._refuse()

    def update_issue(self, key: str, body: str, reopen: bool = False) -> None:
        self._refuse()

    def comment_issue(self, key: str, body: str) -> None:
        self._refuse()

    def assign(self, key: str) -> None:
        self._refuse()

    def issue_notes(self, key: str, since: str = "") -> list[IssueNote]:
        self._refuse()

    def login(self) -> str:
        self._refuse()


@functools.lru_cache(maxsize=None)
def active() -> Tracker:
    return adapter("tracker") or Tracker()


def __getattr__(name: str):
    if name.startswith("_") or not hasattr(Tracker, name):
        raise AttributeError(name)
    return getattr(active(), name)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


PARTIAL = 3


def problem(problems: list[str], text: str) -> None:
    import forge

    print(forge.annotation("warning", text), file=sys.stderr)
    problems.append(text)


def brought(sha: str, change, problems: list[str]) -> set[int]:
    import forge

    if not change.head_sha:
        return set()
    if git("cat-file", "-e", f"{change.head_sha}^{{commit}}").returncode:
        git("fetch", "--quiet", "--no-tags", "origin", change.fetch_ref)
    log = git("log", "--format=%B%x00", f"{sha}^1..{change.head_sha}")
    if log.returncode:
        problem(problems, msg("tracker_close_commits_unread", forge.change_ref(change.number), log.stderr.strip()))
        return set()
    return {found for message in log.stdout.split("\0") if (found := forge.change_in_message(message)) is not None}


def pushed(sha: str, before: str) -> list[str]:
    if not before.strip("0") or git("merge-base", "--is-ancestor", before, sha).returncode:
        return [sha]
    listed = git("rev-list", "--first-parent", "--reverse", f"{before}..{sha}")
    return listed.stdout.split() if listed.returncode == 0 and listed.stdout.split() else [sha]


def close_merged(adapter: Tracker, sha: str, before: str = "") -> int:
    import forge

    problems: list[str] = []
    code = max(close_commit(adapter, commit, problems) for commit in pushed(sha, before))
    if not problems:
        return code
    forge.step_summary("\n".join([msg("tracker_close_summary"), "", *(f"- {text}" for text in problems), ""]))
    return code or PARTIAL


def close_commit(adapter: Tracker, sha: str, problems: list[str]) -> int:
    import forge

    base = get("git.baseBranch", "develop")
    message = git("log", "-1", "--format=%B", sha)
    if message.returncode:
        print(message.stderr.strip(), file=sys.stderr)
        return 1
    number = forge.change_in_message(message.stdout)
    try:
        change = forge.change(number) if number is not None else None
        if change is None or change.state != "merged" or change.base != base:
            print(msg("tracker_close_none", sha[:12], base))
            return 0
        carriers: dict[str, int] = {}
        for carrier in sorted(brought(sha, change, problems) | {number}):
            for key in adapter.linked_issues({carrier}, closing=True):
                carriers.setdefault(key, carrier)
        if number in carriers.values() and adapter.closes_natively(base):
            carriers = {key: carrier for key, carrier in carriers.items() if carrier != number}
    except (forge.ForgeError, TrackerError) as error:
        problem(problems, msg("tracker_close_unread", sha[:12], error))
        return 0
    merged = forge.change_ref(number)
    for key, carrier in sorted(carriers.items()):
        try:
            closed = adapter.close_issue(key)
        except TrackerError as error:
            problem(problems, msg("tracker_close_failed", adapter.issue_ref(key), error))
            continue
        ref, source = adapter.issue_ref(key), forge.change_ref(carrier)
        print(msg("tracker_closed", ref, source, merged) if closed else msg("tracker_close_already", ref, source, merged))
    return 0


def operate(adapter: Tracker, args: argparse.Namespace) -> int:
    if args.command == "issue":
        found = adapter.issue(args.key)
        if found is None:
            print(msg("tracker_issue_missing", args.key), file=sys.stderr)
            return 1
        emit(asdict(found))
    elif args.command == "notes":
        emit([asdict(note) for note in adapter.issue_notes(args.key)])
    elif args.command == "find":
        emit([asdict(issue) for issue in adapter.find_issues(args.label)])
    elif args.command == "login":
        print(adapter.login())
    elif args.command == "close-merged":
        return close_merged(adapter, args.sha, args.before)
    elif args.command == "assign":
        adapter.assign(args.key)
        print(msg("tracker_assigned", adapter.issue_ref(args.key), adapter.login()))
    else:
        from authorship import sign, unsigned
        from text_data import unmarked
        from text_gate import refusal

        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()
        if args.command == "comment":
            body = sign(body)
        if reason := refusal(body, document=args.command != "comment"):
            print(reason, file=sys.stderr)
            return 1
        if not adapter.html_comments:
            body = unmarked(unsigned(body))
        if args.command == "comment":
            adapter.comment_issue(args.key, body)
        elif args.command == "create":
            created = adapter.create_issue(args.title, body, args.label)
            print(msg("tracker_created", adapter.issue_ref(created.key), created.url))
        else:
            adapter.update_issue(args.key, body)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Трекер задач для shell, установщика, агентов и CI.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe-branch", help="ветка задачи по конвенции веток для пробы гейта; пусто - трекера нет")
    branch = commands.add_parser("branch-issue", help="ключ задачи, которую называет ветка; пусто - ветка не задачи")
    branch.add_argument("branch")
    commands.add_parser("issue", help="задача объектом JSON").add_argument("key")
    commands.add_parser("notes", help="комментарии задачи списком JSON, от раннего к позднему").add_argument("key")
    commands.add_parser("find", help="открытые задачи с меткой списком JSON").add_argument("--label", required=True)
    commands.add_parser("login", help="учетная запись токена трекера в форме автора комментария")
    commands.add_parser("assign", help="назначить задачу учетной записи токена трекера").add_argument("key")
    merged = commands.add_parser("close-merged", help="закрыть задачи по закрывающим ссылкам запросов, которые push влил в базовую ветку")
    merged.add_argument("--sha", required=True, help="коммит базовой ветки после push")
    merged.add_argument("--before", default="", help="голова ветки до push: разбираются коммиты первого родителя после нее")
    for write in (commands.add_parser("comment", help="комментарий к задаче"),
                  commands.add_parser("update", help="тело задачи взамен прежнего")):
        write.add_argument("key")
        write.add_argument("--body-file", required=True, help="файл с текстом в UTF-8")
    create = commands.add_parser("create", help="новая задача; печатает ее ключ и адрес")
    create.add_argument("--title", required=True)
    create.add_argument("--body-file", required=True, help="файл с текстом в UTF-8")
    create.add_argument("--label", action="append", default=[], help="метка задачи; можно несколько")
    args = parser.parse_args()
    adapter = active()
    if args.command in ("issue", "notes", "find", "comment", "update", "create", "login", "assign", "close-merged"):
        try:
            return operate(adapter, args)
        except (TrackerError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
    if args.command == "branch-issue":
        found = adapter.branch_issue(args.branch) or ""
    else:
        key = adapter.sample_key()
        found = branch_name("feat", "probe", key) if key else ""
    if not found:
        return 1
    print(found)
    return 0


if __name__ == "__main__":
    import tracker

    sys.exit(tracker.main())
