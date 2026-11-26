from __future__ import annotations

import argparse
import base64
import functools
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from manifest import adapter, get, root as tree_root  # noqa: E402
from platform_messages import msg  # noqa: E402
from rest import emit  # noqa: E402

LOG_LINES = 400
CLOSE_NOTE = "comment/close-note"
MENTION = re.compile(r"@(?:claude|codex|cursor)\b")


def observed_head(sha: str, created: str, before: int) -> str:
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", sha) is None:
        return ""
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return sha.lower() if stamp.tzinfo is not None and stamp.timestamp() < before else ""
    except (AttributeError, ValueError, OverflowError, OSError):
        return ""


class ForgeError(RuntimeError):
    def __init__(self, why: str, status: int | None = None):
        super().__init__(why)
        self.status = status


class ForgeUnavailable(ForgeError):
    pass


class ForgeUnsettled(ForgeError):
    pass


class ForgeExpired(ForgeError):
    pass


@dataclass(frozen=True)
class MergeTool:
    action: str
    number: str
    body: str
    method: str = ""
    no_commit: str = ""


@dataclass(frozen=True)
class CloseTool:
    action: str
    field: str
    value: str


@dataclass(frozen=True)
class AgentTools:
    prefix: str
    comments: tuple[str, ...]
    documents: tuple[str, ...]
    merge: MergeTool | None = None
    opens: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    close: CloseTool | None = None

    def closes(self, tool: str, inputs: Mapping[str, object]) -> bool:
        return bool(self.close and tool == self.prefix + self.close.action
                    and str(inputs.get(self.close.field) or "").lower() == self.close.value)


@dataclass(frozen=True)
class Thread:
    id: str
    path: str
    line: int | None
    resolved: bool
    outdated: bool
    author: str
    body: str
    url: str


@dataclass(frozen=True)
class Change:
    number: int
    title: str
    body: str
    state: str
    draft: bool
    labels: tuple[str, ...]
    base: str
    head: str
    head_sha: str
    fetch_ref: str
    author: str
    url: str
    files: int | None
    base_sha: str = ""
    merge_sha: str = ""
    created: str = ""
    cross_repo: bool = False
    squash_sha: str = ""


@dataclass(frozen=True)
class Note:
    id: str
    author: str
    member: bool
    created: str
    body: str
    bot: bool = False
    system: bool = False
    path: str = ""
    line: int | None = None


@dataclass(frozen=True)
class Review:
    created: str
    state: str
    sha: str
    author: str
    body: str


ROUND_MARK = "<!-- harness-round:"
RIGHT, LEFT = "RIGHT", "LEFT"
SIDES = (RIGHT, LEFT)


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    body: str
    key: str = ""
    side: str = RIGHT


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    url: str
    author: str = ""
    kind: str = ""
    description: str = ""


@dataclass(frozen=True)
class Event:
    kind: str
    target: str
    number: int | None
    author: str
    member: bool
    bot: bool
    body: str
    labels: tuple[str, ...]
    sha: str
    run: str
    id: str = ""
    at: str = ""


class Forge:
    name = ""
    tools: AgentTools | None = None
    git_user = ""
    token_names: tuple[str, ...] = ()

    def missing(self) -> str:
        return msg("forge_off")

    def _refuse(self):
        raise ForgeUnavailable(self.missing() or msg("forge_unsupported", self.name))

    def _token(self) -> str:
        for name in self.token_names:
            if os.environ.get(name):
                return os.environ[name]
        raise ForgeUnavailable(msg("forge_no_token", ", ".join(self.token_names)))

    def repo(self) -> str:
        return ""

    def change_url(self, number: int | str) -> str:
        return ""

    def change_link(self, number: int | str) -> str:
        return self.change_url(number) or self.change_ref(number)

    def compare_url(self, base: str, head: str) -> str:
        return ""

    def file_url(self, ref: str, path: str) -> str:
        return ""

    def run_url(self) -> str:
        return ""

    def change_ref(self, number: int | str) -> str:
        return str(number)

    def change_in_subject(self, subject: str) -> int | None:
        return None

    def change_in_message(self, message: str) -> int | None:
        return self.change_in_subject(message.strip().partition("\n")[0])

    def clone_url(self) -> str:
        self._refuse()

    def agent_login(self) -> str:
        return ""

    def login(self) -> str:
        self._refuse()

    def git_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        if not self.git_user:
            self._refuse()
        credential = base64.b64encode(f"{self.git_user}:{self._token()}".encode()).decode("ascii")
        self.mask(credential)
        address = urlsplit(self.clone_url())
        env = dict(os.environ if base is None else base)
        try:
            count = int(env.get("GIT_CONFIG_COUNT") or 0)
        except ValueError:
            count = 0
        env.update({f"GIT_CONFIG_KEY_{count}": f"http.{address.scheme}://{address.netloc}/.extraheader",
                    f"GIT_CONFIG_VALUE_{count}": f"AUTHORIZATION: basic {credential}",
                    "GIT_CONFIG_COUNT": str(count + 1)})
        return env

    def ci_files(self) -> list[str]:
        return []

    def in_ci(self) -> bool:
        return False

    def run_id(self) -> str:
        return ""

    def run_attempt(self) -> str:
        return ""

    def workspace(self) -> str:
        return ""

    def annotation(self, level: str, text: str) -> str:
        return text

    def group(self, title: str) -> str | None:
        return None

    def end_group(self) -> str | None:
        return None

    def step_summary(self, text: str) -> None:
        pass

    def export_env(self, name: str, value: str) -> None:
        self._refuse()

    def add_path(self, folder: str) -> None:
        pass

    def set_output(self, name: str, value: str) -> None:
        self._refuse()

    def mask(self, value: str) -> None:
        pass

    def run_jobs(self, run: str = "") -> dict[str, str]:
        self._refuse()

    def run_finished(self, run: str) -> bool:
        self._refuse()

    def run_of(self, url: str) -> str:
        return ""

    def run_path(self, run: str) -> str:
        return ""

    def checks(self, sha: str) -> list[Check]:
        self._refuse()

    def job_log(self, job: str) -> str:
        self._refuse()

    def event(self) -> Event | None:
        return None

    def review_input_change(self, run: str) -> Change | None:
        self._refuse()

    def publisher(self, number: int, head: str, base: str) -> bool:
        return True

    def review_run(self, number: int, url: str, snapshot: dict) -> dict:
        self._refuse()

    def review_artifact(self, url: str, name: str) -> dict[str, bytes]:
        self._refuse()

    def review_jobs(self, url: str) -> list:
        self._refuse()

    def review_requested(self, number: int, head: str) -> bool:
        self._refuse()

    def start_pipeline(self, variables: dict[str, str], event: Event | None = None) -> str:
        self._refuse()

    def setup_problems(self, base: str, release: str) -> list[str]:
        return []

    def earlier_run(self) -> str:
        return ""

    def ensure_event_hook(self) -> str:
        return ""

    def owner_steps(self, base: str, release: str) -> str:
        return ""

    def change(self, number: int) -> Change | None:
        self._refuse()

    def historical_heads(self, pull: Change, before: int) -> list[str]:
        return []

    def change_files(self, number: int) -> list[str] | None:
        self._refuse()

    def open_change(self, head: str, base: str, title: str, body: str, draft: bool = True,
                    labels: Sequence[str] = ()) -> Change:
        self._refuse()

    def edit_change(self, number: int, body: str) -> None:
        self._refuse()

    def mark_ready(self, number: int) -> None:
        self._refuse()

    def close_change(self, number: int) -> None:
        self._refuse()

    def close_noted(self, number: int, body: str) -> None:
        from text_data import opening
        self.comment(number, body, opening(CLOSE_NOTE))
        self.close_change(number)

    def add_label(self, number: int, name: str) -> None:
        self._refuse()

    def remove_label(self, number: int, name: str) -> None:
        self._refuse()

    def request_review(self, number: int) -> None:
        label = review_label()
        change = self.change(number)
        if change is None:
            raise ForgeUnavailable(msg("forge_no_change", self.change_ref(number)))
        if label in change.labels:
            self.remove_label(number, label)
        self.add_label(number, label)

    def notes(self, number: int) -> list[Note]:
        self._refuse()

    def task_notes(self, number: int) -> list[Note]:
        self._refuse()

    def reviews(self, number: int) -> list[Review]:
        self._refuse()

    def review_threads(self, number: int) -> list[Thread] | None:
        self._refuse()

    def reply_thread(self, thread: str, body: str) -> str:
        self._refuse()

    def resolve_thread(self, thread: str) -> str:
        self._refuse()

    def change_body(self, number: int) -> str:
        self._refuse()

    def readable(self, text: str) -> str:
        return text

    def merged_title(self, title: str) -> str:
        return title

    def commit_results(self, sha: str) -> dict[str, Check]:
        self._refuse()

    def set_commit_state(self, sha: str, context: str, state: str, description: str, url: str = "") -> None:
        self._refuse()

    def submit_review(self, number: int, sha: str, decision: str, body: str,
                      comments: Sequence[InlineComment] = (), rejected=None) -> None:
        self._refuse()

    def comment(self, number: int, body: str, marker: str = "", last: bool = False) -> None:
        self._refuse()

    def branch_head(self, branch: str) -> str:
        self._refuse()

    def open_changes(self, branch: str) -> list[int]:
        self._refuse()

    def merged_changes(self, branch: str) -> list[int]:
        self._refuse()

    def create_branch(self, branch: str, sha: str) -> None:
        self._refuse()

    def delete_branch(self, branch: str) -> None:
        self._refuse()

    def create_tag(self, tag: str, sha: str) -> None:
        self._refuse()

    def labels(self) -> set[str]:
        self._refuse()

    def create_label(self, name: str, description: str) -> None:
        self._refuse()

    def secret_names(self, environment: str = "") -> set[str]:
        self._refuse()


def review_label(field: str = "label") -> str:
    return get(f"processes.review.{field}", "", file="methodology.json")


def declared_labels(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if (key == "label" or key.endswith("Label")) and isinstance(value, str) and value:
                found.add(value)
            else:
                found |= declared_labels(value)
    elif isinstance(node, list):
        for item in node:
            found |= declared_labels(item)
    return found


@functools.lru_cache(maxsize=None)
def active() -> Forge:
    return adapter("forge") or Forge()


def remember_change(branch: str, change: int, issue: str = "") -> None:
    prefix = get("project.gitConfigPrefix", "")
    if not prefix or not branch or not change:
        return
    for name, value in (("pr", str(change)), ("issue", issue)):
        key = f"branch.{branch}.{prefix}-{name}"
        if not value or subprocess.run(["git", "config", "--get", key], cwd=tree_root(), capture_output=True,
                                       text=True, check=False).stdout.strip() == value:
            continue
        if subprocess.run(["git", "config", key, value], cwd=tree_root(), capture_output=True, check=False).returncode:
            print(msg("forge_change_unremembered", key, value), file=sys.stderr)


def __getattr__(name: str):
    if name.startswith("_") or not hasattr(Forge, name):
        raise AttributeError(name)
    return getattr(active(), name)


def inline_fallback(body: str, comments: Sequence[InlineComment]) -> str:
    if not comments:
        return body
    listed = [f"- {item.path}:{item.line}{' ' + msg('forge_inline_base') if item.side == LEFT else ''} - "
              + item.body.replace("\n", "\n  ") for item in comments]
    return body.rstrip() + "\n\n" + "\n".join(listed)


def pairs(parser: argparse.ArgumentParser, items: list[str]) -> list[tuple[str, str]]:
    found = []
    for pair in items:
        name, separator, value = pair.partition("=")
        if not separator:
            parser.error(msg("forge_pair_form", pair))
        found.append((name, value))
    return found


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Поставщик кода для shell, CI и агента.")
    commands = root.add_subparsers(dest="command", required=True)
    body = argparse.FileType("r", encoding="utf-8")

    def numbered(name: str, help: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help)
        command.add_argument("number", type=int)
        return command

    commands.add_parser("comment-tool", help="инструмент агента для реплики в обсуждении; пусто - модуля нет")
    env = commands.add_parser("export-env", help="переменные окружения следующих шагов CI")
    env.add_argument("pairs", nargs="+", metavar="NAME=VALUE")
    path = commands.add_parser("add-path", help="каталоги PATH следующих шагов CI")
    path.add_argument("folders", nargs="+")
    note = commands.add_parser("annotation", help="строка журнала CI уровня error, warning или notice")
    note.add_argument("level")
    note.add_argument("text")
    numbered("change", help="запрос на слияние: JSON")
    numbered("notes", help="комментарии к запросу: список JSON")
    numbered("reviews", help="ревью запроса с телами вердиктов: список JSON")
    numbered("comment", help="комментарий к запросу").add_argument("--body-file", required=True, type=body)
    label = numbered("label", help="метки запроса")
    label.add_argument("--add", action="append", default=[], metavar="NAME")
    label.add_argument("--remove", action="append", default=[], metavar="NAME")
    numbered("request-review", help="круг ревью запроса: метка review.label")
    numbered("ready", help="снять запрос с черновика")
    numbered("close-change", help=f"закрыть запрос без мержа: сначала комментарий {CLOSE_NOTE}, без него запрос не закрывается"
             ).add_argument("--body-file", required=True, type=body)
    numbered("edit-change", help="новое описание запроса на слияние").add_argument("--body-file", required=True, type=body)
    opened = commands.add_parser("open-change", help="новый запрос на слияние: JSON")
    for option in ("--head", "--base", "--title"):
        opened.add_argument(option, required=True)
    opened.add_argument("--body-file", required=True, type=body)
    opened.add_argument("--draft", action="store_true")
    opened.add_argument("--label", action="append", default=[])
    checks = commands.add_parser("checks", help="результаты CI на коммите: состояние, имя и адрес через табуляцию")
    checks.add_argument("sha")
    commands.add_parser("job-log", help="хвост журнала джоба CI").add_argument("job", metavar="ID_OR_URL")
    tag = commands.add_parser("tag", help="тег на коммите")
    tag.add_argument("tag")
    tag.add_argument("sha")
    return root


def rejected(body: str, **options) -> bool:
    from text_gate import refusal
    if reason := refusal(body, **options):
        print(reason, file=sys.stderr)
    return bool(reason)


def run(adapter: Forge, args: argparse.Namespace, root: argparse.ArgumentParser) -> int:
    command = args.command
    if command == "export-env":
        for name, value in pairs(root, args.pairs):
            adapter.export_env(name, value)
    elif command == "add-path":
        for folder in args.folders:
            adapter.add_path(folder)
    elif command == "change":
        change = adapter.change(args.number)
        if change is None:
            print(msg("forge_no_change", adapter.change_ref(args.number)), file=sys.stderr)
            return 1
        emit(asdict(change))
    elif command == "notes":
        emit([asdict(item) for item in adapter.notes(args.number)])
    elif command == "reviews":
        emit([asdict(item) for item in adapter.reviews(args.number)])
    elif command == "comment":
        from authorship import sign
        body = sign(args.body_file.read())
        if rejected(body):
            return 1
        adapter.comment(args.number, body)
    elif command == "label":
        for name in args.add:
            adapter.add_label(args.number, name)
        for name in args.remove:
            adapter.remove_label(args.number, name)
    elif command == "request-review":
        adapter.request_review(args.number)
    elif command == "ready":
        adapter.mark_ready(args.number)
    elif command == "close-change":
        from authorship import sign
        from text_data import marked
        body = sign(args.body_file.read())
        if not marked(body, CLOSE_NOTE):
            print(msg("forge_close_unmarked", CLOSE_NOTE, msg("close_note_command")), file=sys.stderr)
            return 1
        if rejected(body):
            return 1
        change = adapter.change(args.number)
        if change is None or change.state != "open":
            print(msg("forge_close_state", adapter.change_ref(args.number), change.state if change else "-"),
                  file=sys.stderr)
            return 1
        adapter.close_noted(args.number, body)
    elif command == "edit-change":
        body = args.body_file.read()
        if rejected(body, change=True):
            return 1
        adapter.edit_change(args.number, body)
    elif command == "open-change":
        body = args.body_file.read()
        if rejected(body, change=True, title=args.title):
            return 1
        change = adapter.open_change(args.head, args.base, args.title, body, args.draft, args.label)
        remember_change(args.head, change.number)
        emit(asdict(change))
    elif command == "checks":
        for check in adapter.checks(args.sha):
            print(f"{check.state}\t{check.name}\t{check.url}")
    elif command == "job-log":
        print(adapter.job_log(args.job))
    elif command == "tag":
        adapter.create_tag(args.tag, args.sha)
    return 0


def main() -> int:
    root = parser()
    args = root.parse_args()
    adapter = active()
    if args.command in ("comment-tool", "annotation"):
        if args.command == "comment-tool":
            found = adapter.tools.prefix + adapter.tools.comments[0] if adapter.tools and adapter.tools.comments else ""
        else:
            found = adapter.annotation(args.level, args.text)
        if not found:
            return 1
        print(found)
        return 0
    try:
        return run(adapter, args, root)
    except ForgeError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    import forge

    sys.exit(forge.main())
