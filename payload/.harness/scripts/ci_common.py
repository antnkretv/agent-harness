#!/usr/bin/env python3
from __future__ import annotations

import atexit
import functools
import glob
import hashlib
from collections import deque
from datetime import datetime
import importlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HARNESS_BLOCK = re.compile(r"(?:#|<!--) harness:begin (\S+)(?: -->)?\n.*?harness:end \1(?: -->)?", re.S)

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

import script_paths  # noqa: E402,F401
import platform_messages  # noqa: E402
from executables import bash, executable  # noqa: E402,F401
from manifest import get, get_list, get_map, get_record, get_records, module_paths, root, rules, secrets, setting  # noqa: E402,F401

ROOT = root()


def log_tail(path: str | os.PathLike[str], limit: int = 200) -> str:
    with open(path, encoding="utf-8", errors="replace") as stream:
        return "".join(deque(stream, maxlen=limit))


def moment(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def process_definitions(declared: dict) -> dict:
    resolved = {}
    visiting = set()

    def resolve(process: str, identity: str) -> dict:
        key = (process, identity)
        if key in visiting:
            raise ValueError(platform_messages.msg("pipeline_step_reference", "/".join(key)))
        if key in resolved:
            return resolved[key]
        entries = declared.get(process, {}).get("steps", [])
        matches = [entry for entry in entries if entry.get("id", entry.get("ref", "").rpartition("/")[2]) == identity]
        if len(matches) != 1:
            raise ValueError(platform_messages.msg("pipeline_step_reference", "/".join(key)))
        entry = matches[0]
        visiting.add(key)
        if "ref" in entry:
            target = entry["ref"]
            if process == "review" or not isinstance(target, str) or target.count("/") != 1 or set(entry) - {"ref", "requires"}:
                raise ValueError(platform_messages.msg("pipeline_step_reference", str(target)))
            parent, name = target.split("/")
            result = {**resolve(parent, name), **entry}
        else:
            result = dict(entry)
        visiting.remove(key)
        resolved[key] = result
        return result

    return {name: {**process, "steps": [resolve(name, step.get("id", step.get("ref", "").rpartition("/")[2]))
                                      for step in process.get("steps", [])]}
            for name, process in declared.items()}


def processes() -> dict[str, dict]:
    return process_definitions(get_map("processes", {}, file="methodology.json"))


def __getattr__(name: str):
    if name in ("forge", "tracker"):
        return importlib.import_module(name)
    raise AttributeError(name)


BASE_BRANCH = "origin/" + get("git.baseBranch", "develop")
CUT_PREFIX, BACK_PREFIX = "release/", "back-merge/"


GIT_LOW_SPEED = (("http.lowSpeedLimit", "1024"), ("http.lowSpeedTime", "60"))


def git_auth() -> dict | None:
    forge = importlib.import_module("forge")
    return forge.git_env() if forge.name and any(os.environ.get(name) for name in forge.token_names) else None


def head_checks(head: str) -> dict:
    forge = importlib.import_module("forge")
    found = forge.commit_results(head)
    for check in forge.checks(head):
        if check.name not in found or getattr(check, "kind", "") in ("job", "check"):
            found[check.name] = check
    return found


READY_STATUS = "pipeline/ready"


def protected_status(name: str) -> bool:
    return name == READY_STATUS or name.startswith(get("processes.review.statusPrefix", "review", file="methodology.json") + "/")


def failing(checks: dict, skip) -> list:
    return [check for name, check in sorted(checks.items())
            if not skip(name) and check.state not in ("success", "neutral", "skipped", "pending")]


def checks_state(checks: dict, skip) -> str:
    if failing(checks, skip):
        return "failure"
    return "pending" if any(check.state == "pending" for name, check in checks.items() if not skip(name)) else ""


def change_env(change) -> dict[str, str]:
    return {"PR_BRANCH": change.head, "PR_BODY": change.body, "PR_NUMBER": str(change.number)}


def check_context() -> str:
    return "check" if importlib.import_module("forge").name == "github" else "harness-check"


def agent_context() -> str:
    return "react" if importlib.import_module("forge").name == "github" else "harness-agent"


def runs_finished(checks: dict) -> bool:
    forge = importlib.import_module("forge")
    runs = {forge.run_of(check.url) for check in checks.values()} - {""}
    return all(forge.run_finished(run) for run in runs)


def opened_into(base: str, prefix: str) -> list:
    forge = importlib.import_module("forge")
    return [change for change in map(forge.change, forge.open_changes(base))
            if change is not None and change.head.startswith(prefix)]


def opened_from(branch: str):
    forge = importlib.import_module("forge")
    found = [forge.change(number) for number in forge.open_changes(branch)]
    return next((change for change in found if change is not None and change.head == branch), None)


def threads_resolved(number: int) -> bool:
    threads = importlib.import_module("forge").review_threads(number)
    return threads is not None and all(thread.resolved for thread in threads)


def release_branches() -> tuple[str, str]:
    return get("git.releaseBranch", "master"), BASE_BRANCH.removeprefix("origin/")


def release_flow(branch: str) -> bool:
    return branch in release_branches() or branch.startswith((CUT_PREFIX, BACK_PREFIX))


def modules() -> dict[str, dict]:
    return {record["id"]: record for record in rules() if record.get("id")}


LOCK = ".harness/state/lock.json"
INSTALLED = "harness/installed.json"


KIT_ROLES = ("managed", "json")


def lock_entries(text: str) -> dict[str, dict]:
    try:
        found = json.loads(text or "{}").get("files", {})
    except (ValueError, AttributeError):
        return {}
    return {path: entry for path, entry in found.items() if isinstance(entry, dict)} if isinstance(found, dict) else {}


def kit_of(text: str) -> dict:
    try:
        found = json.loads(text or "{}").get("kit", {})
    except (ValueError, AttributeError):
        return {}
    return found if isinstance(found, dict) else {}


def lock_kit() -> dict:
    try:
        with open(os.path.join(ROOT, LOCK), encoding="utf-8") as handle:
            return kit_of(handle.read())
    except OSError:
        return {}


def lock_files() -> dict[str, dict]:
    try:
        with open(os.path.join(ROOT, LOCK), encoding="utf-8") as handle:
            entries = lock_entries(handle.read())
    except OSError:
        return {}
    for path in installed_files():
        entries.setdefault(path, {"role": "managed", "sha256": installed_record()["files"][path], "installed": True})
    return entries


LOADER = ".harness/ah.py"
METHODOLOGY_PATH = ".harness/methodology.json"
STALE_BASE_SECONDS = 6 * 3600


def install_kit(tree: str) -> subprocess.CompletedProcess | None:
    loader = os.path.join(tree, LOADER)
    try:
        with open(os.path.join(tree, LOCK), encoding="utf-8") as handle:
            pinned = json.load(handle).get("kit", {}).get("sha256")
    except (OSError, ValueError, AttributeError):
        pinned = None
    if not (os.path.isfile(loader) and pinned) or git("-C", tree, "ls-files", "--", METHODOLOGY_PATH):
        return None
    return subprocess.run([sys.executable, loader, "install"], cwd=tree, capture_output=True, text=True,
                          encoding="utf-8", check=False)


def place_trusted(tree: str) -> None:
    paths = installed_files()
    tracked = set(git("-C", tree, "ls-files", "-z", "--", *paths).split("\0")) if paths else set()
    record = installed_record()
    placed = {}
    inside = os.path.realpath(tree) + os.sep
    for path in paths:
        source, target = os.path.join(ROOT, path), os.path.join(tree, path)
        if path in tracked or os.path.islink(source) \
                or not (os.path.realpath(os.path.dirname(target)) + os.sep).startswith(inside):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            os.unlink(target)
        shutil.copy2(source, target)
        placed[path] = record["files"][path]
    kept = git("-C", tree, "rev-parse", "--path-format=absolute", "--git-path", INSTALLED)
    os.makedirs(os.path.dirname(kept), exist_ok=True)
    with open(kept, "w", encoding="utf-8") as handle:
        json.dump({"kit": record.get("kit", ""), "files": placed}, handle, ensure_ascii=False, indent=2)


@functools.lru_cache(maxsize=None)
def base_tree(base: str) -> str | None:
    if git("cat-file", "-t", f"{base}:{METHODOLOGY_PATH}") or not git("cat-file", "-t", f"{base}:{LOADER}"):
        return None
    prefix = "harness-base-" + hashlib.sha256(ROOT.encode("utf-8")).hexdigest()[:12] + "-"
    for stale in glob.glob(os.path.join(tempfile.gettempdir(), prefix + "*")):
        try:
            old = time.time() - os.path.getmtime(stale) > STALE_BASE_SECONDS
        except OSError:
            continue
        if old:
            shutil.rmtree(stale, ignore_errors=True)
    git("worktree", "prune")
    folder = tempfile.mkdtemp(prefix=prefix)
    tree = os.path.join(folder, "base")
    run_strict("git", "worktree", "add", "--quiet", "--detach", tree, base, unread=msg("gate_tree_unread"))
    atexit.register(lambda: (git("worktree", "remove", "--force", tree), shutil.rmtree(folder, ignore_errors=True),
                             git("worktree", "prune")))
    done = install_kit(tree)
    if done is not None and done.returncode:
        print((done.stderr or done.stdout).strip(), file=sys.stderr)
        print(msg("gate_tree_unread"))
        sys.exit(2)
    return tree


def live_base_tree(base: str) -> str | None:
    tree = base_tree(base)
    if tree is not None:
        os.utime(os.path.dirname(tree))
    return tree


def base_paths(base: str) -> list[str]:
    tree = live_base_tree(base)
    if tree is None:
        return git("ls-tree", "-r", "--name-only", base).splitlines()
    listed = (git("-C", tree, "ls-files", "-z") + "\0"
              + git("-C", tree, "ls-files", "-z", "--others", "--ignored", "--exclude-standard"))
    return sorted({path for path in listed.split("\0")
                   if path and os.path.isfile(os.path.join(tree, path)) and not os.path.islink(os.path.join(tree, path))})


def base_read(base: str, path: str) -> bytes | None:
    found = subprocess.run(["git", "-C", ROOT, "show", f"{base}:{path}"], capture_output=True, check=False)
    if found.returncode == 0:
        return found.stdout
    tree = live_base_tree(base)
    if tree is None:
        return None
    full = os.path.join(tree, path)
    if not os.path.isfile(full):
        return None
    with open(full, "rb") as handle:
        return handle.read()


@functools.lru_cache(maxsize=None)
def installed_record() -> dict:
    record = git("rev-parse", "--git-path", INSTALLED)
    try:
        with open(record if os.path.isabs(record) else os.path.join(ROOT, record), encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) and isinstance(found.get("files"), dict) else {}


def placed_digest(tree: str) -> bytes:
    found = hashlib.sha256()
    for path in sorted(record_files(tree)):
        full = os.path.join(tree, path)
        found.update(path.encode("utf-8") + b"\0")
        if os.path.isfile(full) and not os.path.islink(full):
            with open(full, "rb") as handle:
                found.update(hashlib.sha256(handle.read()).digest())
    return found.digest()


def installed_files() -> tuple[str, ...]:
    return tuple(path for path in installed_record().get("files", {})
                 if isinstance(path, str) and os.path.isfile(os.path.join(ROOT, path)))


def kit_owned(files: Mapping[str, dict] | None = None) -> set[str]:
    return {path for path, entry in (lock_files() if files is None else files).items() if entry.get("role") in KIT_ROLES}


def block_lines(text: str) -> set[int]:
    return {number for found in HARNESS_BLOCK.finditer(text)
            for number in range(text.count("\n", 0, found.start()) + 1, text.count("\n", 0, found.end()) + 2)}


def release_change(branch: str, base: str) -> bool:
    main = get("git.releaseBranch", "")
    target = base.removeprefix("origin/")
    return bool(main and branch and (branch == main or (target == main and branch.startswith(CUT_PREFIX))))


def change_body() -> str:
    number = os.environ.get("PR_NUMBER", "").strip()
    if number.isdecimal():
        return importlib.import_module("forge").change_body(int(number))
    body = os.environ.get("PR_BODY", "")
    return importlib.import_module("forge").readable(body) if body else body


MACHINE_TEXT_PREFIXES = tuple(get_list("docs.machineTextPrefixes", (
    ".agents/",
    ".claude/",
    ".codex/",
    ".cursor/",
    ".specify/",
    "specs/",
    ".harness/hooks/",
    ".harness/prompts/",
    ".harness/review/",
)))

LIVE_DOC_PREFIXES = tuple(get_list("docs.liveDocPrefixes", ("docs/features/",)))

ADR_TREE = "docs/platform/adr"
MACHINE_TEXT_SEGMENTS = ("/.claude/", "/.codex/", "/.cursor/")

GENERATED_SEGMENTS = tuple(get_list("docs.generatedSegments", (
    "/node_modules/", "/bin/", "/obj/", "/dist/", "Migrations/",
)))
GENERATED_PREFIXES = tuple(get_list("docs.generatedPrefixes", []))
GENERATED_SUFFIXES = (".Designer.cs", ".g.cs", ".gen.ts", ".gen.tsx", ".generated.ts")
GENERATED_MARKERS = ("<auto-generated", "@generated", "auto-generated by", "AUTO-GENERATED", "DO NOT EDIT")
TEST_PATH = re.compile(
    r"(^|/)[A-Za-z0-9_.]*Tests/|\.(test|spec)\.[cm]?[jt]sx?$|^e2e/|/prometheus/tests/.*\.ya?ml$"
)

PAYLOAD_PREFIXES = tuple(get_list("docs.payloadPrefixes", ())) + (".harness/state/",)
SHELL_EXCLUDED = (".specify/",)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout.strip()


def run_strict(*args: str, unread: str) -> str:
    done = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        why = done.stderr.strip().splitlines()
        print(f"x {' '.join(args)}: {why[0] if why else msg('gate_exit_code', done.returncode)}")
        print(unread)
        sys.exit(2)
    return done.stdout


def payload(path: str) -> bool:
    return path.startswith(PAYLOAD_PREFIXES)


def app_prefix(path: str) -> str:
    folder = posixpath.normpath(path).strip("/")
    return "" if folder == "." else folder + "/"


def listed() -> list[str]:
    found = run_strict("git", "ls-files", "-z", unread=msg("gate_tree_unread")).split("\0")
    return sorted({path for path in (*found, *installed_files()) if path})


def tracked() -> list[str]:
    return [path for path in listed() if not payload(path)]


def shell_scripts() -> list[str]:
    found = []
    for path in tracked():
        if path.startswith(SHELL_EXCLUDED):
            continue
        if path.endswith(".sh") or path.startswith(".githooks/"):
            found.append(path)
    return found


def base_ref(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        return upstream
    if git("rev-parse", "--verify", "--quiet", BASE_BRANCH):
        return BASE_BRANCH
    return None


def changed_since(base: str) -> list[str]:
    unread = msg("all_no_diff", base)
    fork = run_strict("git", "merge-base", base, "HEAD", unread=unread).strip()
    listed = run_strict("git", "diff", "--name-only", "-z", fork, unread=unread).split("\0")
    untracked = run_strict("git", "ls-files", "--others", "--exclude-standard", "-z", unread=unread).split("\0")
    return with_kit(sorted({path for path in listed + untracked if path}), fork)


def changed_files(base: str, worktree: bool) -> list[str]:
    chunks = [git("diff", "--name-only", f"{base}...HEAD")]
    if worktree:
        chunks.append(git("diff", "--name-only", "--cached"))
        chunks.append(git("diff", "--name-only"))
    seen: list[str] = []
    for chunk in chunks:
        for path in chunk.splitlines():
            if path and path not in seen:
                seen.append(path)
    return with_kit(seen, base)


def with_kit(paths: list[str], base: str) -> list[str]:
    placed = installed_files()
    moved = {path[len(prefix):] for path in paths for prefix in PAYLOAD_PREFIXES if path.startswith(prefix)}
    extra = [path for path in placed if path in moved]
    if LOCK in paths:
        before = record_files(base_tree(base)) if base_tree(base) else {}
        now = installed_record().get("files", {})
        extra += [path for path in placed if before and now.get(path) != before.get(path)]
    return paths + [path for path in dict.fromkeys(extra) if path not in paths]


def record_files(tree: str) -> dict:
    record = subprocess.run(["git", "-C", tree, "rev-parse", "--path-format=absolute", "--git-path", INSTALLED],
                            capture_output=True, text=True, check=False).stdout.strip()
    try:
        with open(record, encoding="utf-8") as handle:
            found = json.load(handle).get("files", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return found if isinstance(found, dict) else {}


def machine_text(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in MACHINE_TEXT_PREFIXES):
        return True
    return any(segment in path for segment in MACHINE_TEXT_SEGMENTS)


def generated_path(path: str) -> bool:
    if path.endswith(GENERATED_SUFFIXES) or path.startswith(GENERATED_PREFIXES):
        return True
    return any(segment in f"/{path}" for segment in GENERATED_SEGMENTS)


CODE_SPAN = re.compile(r"(`[^`\n]*`)")
MD_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MD_STARS = re.compile(r"\*+")
MD_UNDERSCORES = re.compile(r"_+")


def unemphasized(text: str) -> str:
    plain = MD_STARS.sub("", text)

    def marker(match: re.Match) -> str:
        start, end = match.span()
        inside = 0 < start and end < len(plain) and plain[start - 1].isalnum() and plain[end].isalnum()
        return match.group(0) if inside else ""

    return MD_UNDERSCORES.sub(marker, plain)


def heading_anchor(title: str) -> str:
    parts = CODE_SPAN.split(MD_LINK_TEXT.sub(r"\1", title))
    text = "".join(part.strip("`") if part.startswith("`") else unemphasized(part) for part in parts)
    slug = re.sub(r"[^\w\- ]", "", text.strip().lower(), flags=re.UNICODE).replace(" ", "-")
    return re.sub("-{2,}", "-", slug) if importlib.import_module("forge").name == "gitlab" else slug


def generated(path: str, text: str = "") -> bool:
    if generated_path(path):
        return True
    return any(marker in text[:2048] for marker in GENERATED_MARKERS)


def workflow_files() -> list[str]:
    patterns = importlib.import_module("forge").ci_files()
    return sorted({path for pattern in patterns for path in glob.glob(os.path.join(ROOT, pattern))})


MESSAGES = os.path.join(SCRIPTS, "messages.env")
MESSAGE_LINE = platform_messages.LINE
TEMPLATES = {**platform_messages.TEMPLATES, **platform_messages.load(MESSAGES), **platform_messages.PROJECT_TEMPLATES}


def msg(key: str, *args: object) -> str:
    return platform_messages.format_message(TEMPLATES, key, *args)


GLOSSARY = "docs/platform/standards/product-terms.md"
GLOSSARY_CELL = re.compile(r"(?<!\\)\|")
GLOSSARY_PATTERN = re.compile(r"`([^`]+)`(?:\s+([^\s,`]+))?")


def glossary_patterns(text: str, *, everywhere: bool = True) -> list[tuple[int, str, str]]:
    columns = None
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith("|"):
            columns = None
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in GLOSSARY_CELL.split(line.strip())[1:-1]]
        if columns is None:
            names = (msg("glossary_term_column"), msg("glossary_check_column"))
            columns = tuple(cells.index(name) for name in names) if all(name in cells for name in names) else ()
        elif columns and max(columns) < len(cells):
            term, check = columns
            for pattern, scope in GLOSSARY_PATTERN.findall(cells[check]):
                if not everywhere or scope == msg("glossary_everywhere"):
                    found.append((number, pattern, cells[term]))
    return found


def json_strings(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from json_strings(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from json_strings(value, f"{prefix}[{index}]")
    elif isinstance(node, str):
        yield prefix, node


def annotation(level: str, key: str, *args: object) -> str:
    return importlib.import_module("forge").annotation(level, msg(key, *args))


AGENT_RUN = os.path.join(SCRIPTS, "agent", "agent-run.py")
SCRIPT_TREE = ".harness/scripts/"
SCRIPT_TREES = (SCRIPT_TREE, ".platform/scripts/")
REVIEW_SCHEMA = SCRIPT_TREE + "review/review.schema.json"


def named_scripts(paths: Iterable[str], name: str) -> list[str]:
    return [path for path in paths if path.startswith(SCRIPT_TREES) and posixpath.basename(path) == name]


def agent_run_command(provider: str, mode: str, model: str, effort: str, prompt_file: str, log: str, *,
                      workdir: str | None = None, add_dirs: Sequence[str] = (), allowed: str = "",
                      disallowed: str = "", minutes: float = 0, inputs: str = "",
                      detached: bool = False, purpose: str = "") -> list[str]:
    command = [sys.executable, AGENT_RUN, "--provider", provider, "--mode", mode, "--model", model,
               "--prompt-file", prompt_file, "--log", log]
    for flag, value in (("--effort", effort), ("--workdir", workdir), ("--allowed-tools", allowed),
                        ("--disallowed-tools", disallowed), ("--inputs", inputs), ("--review-purpose", purpose)):
        if value:
            command += [flag, value]
    if detached:
        command.append("--detached")
    for folder in add_dirs:
        command += ["--add-dir", folder]
    if minutes:
        command += ["--timeout-minutes", str(minutes)]
    return command


def has_ref(ref: str) -> bool:
    done = subprocess.run(
        ("git", "rev-parse", "--verify", "--quiet", ref),
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    return done.returncode == 0
