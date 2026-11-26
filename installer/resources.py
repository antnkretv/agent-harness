from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
LINE = re.compile(r"^MSG_([a-z][a-z0-9_]*)='([^']*)'", re.MULTILINE)
ENTRY = re.compile(r"^MSG_([a-z][a-z0-9_]*)='[^']*'\n?", re.MULTILINE)

try:
    TEMPLATES = dict(LINE.findall((HERE / "messages.env").read_text(encoding="utf-8")))
except OSError:
    TEMPLATES = {}


def fill(templates: dict[str, str], key: str, *args: object) -> str:
    template = templates.get(key)
    if template is None:
        return " ".join(["@" + key, *map(str, args)])
    return template % args


def msg(key: str, *args: object) -> str:
    return fill(TEMPLATES, key, *args)


def say(key: str, *args: object) -> None:
    print(msg(key, *args), flush=True)


class Refusal(Exception):
    pass


LOCK = ".harness/state/lock.json"
LOADER = ".harness/ah.py"
INSTALLED_CATALOG = ".harness/state/modules.json"


INSTALLER = '"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py"'
GLOSSARY_RULE_TOOL = ".harness/scripts/check/vale-lint.py"
CATALOG = "modules.json"
KIT_DOCUMENTS = ("README.md", "PARAMETERS.md")
PROJECT = ".harness/project/manifest.json"
METHODOLOGY = ".harness/methodology.json"
AREA_MAP = ".harness/project/area-map.json"
SETTINGS = ".harness/settings.env"
SETTINGS_DEFAULTS = ".harness/settings.defaults.env"
SETUP = "setup"
SETUP_PHASES = ("snapshot", "session")
SETUP_WRAPPERS = ".harness/project/setup"
OWN_SECTIONS = ("rules", SETUP)
SKILLS = ".agents/skills"
SIDES = ("backend", "frontend")
SOURCES = ("literal", "env", "secret")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
PLACEHOLDER = re.compile(r"\{\{([a-z][A-Za-z0-9_]*\.[A-Za-z0-9_.-]+)\}\}")
CONDITION = re.compile(r"\{\{#(if|unless) ([a-z][A-Za-z0-9_.:/=-]*)\}\}(.*?)\{\{/\1\}\}", re.S)
LEFTOVER = re.compile(r"\{\{(?:#(?:if|unless)\b[^}]*|/(?:if|unless)|else)\}\}")
MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s#]+)(#[^)\s]*)?\)")
BLOCK_MARKERS = {
    ".md": ("<!-- harness:begin {id} -->", "<!-- harness:end {id} -->"),
    ".props": ("<!-- harness:begin {id} -->", "<!-- harness:end {id} -->"),
    ".targets": ("<!-- harness:begin {id} -->", "<!-- harness:end {id} -->"),
}
HASH_MARKERS = ("# harness:begin {id}", "# harness:end {id}")
PROJECT_BLOCK = "project"
STACKS_WITH_ADAPTER = ("node", "dotnet")
KNOWN_STACKS = STACKS_WITH_ADAPTER + ("python", "go", "jvm", "other")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROJECT_NAME = r"[A-Za-z0-9][A-Za-z0-9 ._-]*"
GIT_PREFIX = r"[a-z0-9][a-z0-9-]*"
BRANCH = r"[\w./-]+"
SERVICE_AREAS = ("docs", "agents")
POSIX_MODES = os.name == "posix"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", text.lower())


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str, check: bool = True, input: str | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=root, input=input, capture_output=True, text=True, encoding="utf-8")
    if check and done.returncode != 0:
        raise Refusal(msg("git_failed", " ".join(args), done.stderr.strip()))
    return done.stdout.strip()


def tracks_modes(root: Path) -> bool:
    return POSIX_MODES and git(root, "config", "--bool", "core.fileMode", check=False) != "false"


def index_modes(root: Path) -> dict[str, str]:
    modes = {}
    for line in git(root, "ls-files", "-s", "-z", check=False).split("\0"):
        if "\t" in line:
            meta, path = line.split("\t", 1)
            modes[path] = meta.split(" ", 1)[0]
    return modes


def repository(target: str) -> Path:
    root = Path(target).resolve()
    if not root.is_dir():
        raise Refusal(msg("no_target", root))
    top = git(root, "rev-parse", "--show-toplevel", check=False)
    if not top:
        raise Refusal(msg("not_git", root))
    return Path(top)


@dataclass
class Item:
    path: str
    role: str
    module: str
    content: bytes
    executable: bool
    block: str = ""
    extend: str = ""
    place: str = ""

    def __post_init__(self) -> None:
        self.place = place_of(self.role, self.place)


def place_of(role: str, place: str = "") -> str:
    return place or ("lock" if role == "managed" else "shell")


def prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]


def kit_root() -> Path:
    if (HERE / "payload").is_dir() and (HERE / "kit.json").is_file():
        return HERE
    raise Refusal(msg("no_kit", HERE))


def load_json(path: Path, missing: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise Refusal(missing) from None
    except json.JSONDecodeError as error:
        raise Refusal(msg("json_broken", path, error)) from None
    if not isinstance(data, dict):
        raise Refusal(msg("json_broken", path, "object"))
    return data


def unpack(archive: Path, into: Path) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(into, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def build_of(kit: dict) -> tuple:
    return kit.get("version"), kit.get("digest"), kit.get("config") or kit.get("packed", {}).get("config")


def manifest_moved(root: Path, lock: dict) -> bool:
    recorded = lock.get("manifest")
    return bool(recorded) and (root / PROJECT).is_file() and git(root, "hash-object", "--", PROJECT) != recorded


LEGACY_ROOT, LEGACY_STATE = ".platform", "harness"


LEGACY_MANIFEST = ".harness/project.json"
LEGACY_PROJECT = ((LEGACY_MANIFEST, PROJECT),
                  (".harness/state/answers.json", ".harness/project/answers.json"),
                  (".harness/area-map.json", ".harness/project/area-map.json"))
LEGACY_SEEDS = ((".github/workflows/deploy.yml", ".github/workflows/project-deploy.yml"),
                (".gitlab/ci/deploy.yml", ".gitlab/ci/project-deploy.yml"))
LEGACY_STEPS = "sessionSteps"


@contextmanager
def relocated(root: Path, dry_run: bool = False, temporary: bool = False):
    with moved_layer(root, dry_run, temporary), extended(root, dry_run, temporary), setup_moved(root, dry_run, temporary):
        yield


def prune(root: Path, path: Path) -> None:
    for folder in path.parents:
        if folder == root or any(folder.iterdir()):
            break
        folder.rmdir()


def rewrite_lock(root: Path, moved) -> bytes:
    recorded = (root / LOCK).read_bytes()
    lock = json.loads(recorded)
    lock["files"] = {moved(path): entry for path, entry in lock.get("files", {}).items()}
    lock["absent"] = sorted(moved(path) for path in lock.get("absent", []))
    (root / LOCK).write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return recorded


@contextmanager
def extended(root: Path, dry_run: bool = False, temporary: bool = False):
    legacy = root / LEGACY_MANIFEST
    if not legacy.is_file():
        yield
        return
    if dry_run:
        raise Refusal(msg("relocate_needed", LEGACY_MANIFEST))
    listed = load_json(root / LOCK, msg("lock_broken", root / LOCK)).get("files", {}) if (root / LOCK).is_file() else {}
    moves = [(old, new) for old, new in LEGACY_PROJECT if (root / old).is_file()]
    moves += [(old, new) for old, new in LEGACY_SEEDS if (root / old).is_file() and old in listed]
    for old, new in moves:
        if (root / new).exists():
            raise Refusal(msg("relocate_both", old, new))
    done: list[tuple[str, str]] = []
    recorded = None
    originals: dict[Path, bytes | None] = {}

    def restore(converted: bool) -> None:
        for path, content in originals.items() if converted else ():
            if content is None:
                path.unlink(missing_ok=True)
                prune(root, path)
            else:
                path.write_bytes(content)
        if recorded is not None:
            (root / LOCK).write_bytes(recorded)
        for old, new in reversed(done):
            (root / new).rename(root / old)
            prune(root, root / new)

    try:
        for old, new in moves:
            (root / new).parent.mkdir(parents=True, exist_ok=True)
            (root / old).rename(root / new)
            done.append((old, new))
            if (old, new) in LEGACY_SEEDS:
                text = (root / new).read_bytes()
                originals[root / new] = text
                (root / new).write_bytes(text.replace(old.encode(), new.encode()))
        if (root / LOCK).is_file():
            renamed = dict(moves)
            recorded = rewrite_lock(root, lambda path: renamed.get(path, path))
        steps = steps_moved(root, originals)
        yield
    except BaseException:
        restore(True)
        raise
    if temporary:
        restore(False)
        return
    print(msg("extended", ", ".join(f"{old} -> {new}" for old, new in moves)), file=sys.stderr)
    if steps:
        print(msg("steps_moved", f"{SETUP}.session", ", ".join(steps)), file=sys.stderr)


def steps_moved(root: Path, originals: dict[Path, bytes | None]) -> list[str]:
    moved: list[str] = []

    def edit(project: dict) -> dict | None:
        settings = project.get("modules", {}).get("agents", {}).get("settings")
        if not isinstance(settings, dict) or LEGACY_STEPS not in settings:
            return None
        moved.extend("bash " + shlex.quote(step) for step in settings.pop(LEGACY_STEPS) or [])
        setup = project.setdefault(SETUP, {})
        if not isinstance(setup, dict) or not isinstance(setup.get("session", []), list):
            raise Refusal(msg("param_setup_shape", SETUP, ", ".join(SETUP_PHASES)))
        if moved:
            setup.setdefault("session", []).extend(moved)
        if not setup:
            project.pop(SETUP)
        return {f"modules.agents.settings.{LEGACY_STEPS}": None, **({SETUP: setup} if moved else {})}

    manifest_rewritten(root, originals, edit)
    return moved


def manifest_rewritten(root: Path, originals: dict[Path, bytes | None], edit) -> bool:
    from .documents import json_indent, json_text

    manifest = root / PROJECT
    if not manifest.is_file():
        return False
    raw = manifest.read_bytes()
    project = json.loads(raw)
    answered = edit(project)
    if answered is None:
        return False
    originals.setdefault(manifest, raw)
    text = json_text(project, json_indent(raw))
    manifest.write_text(text, encoding="utf-8", newline="\n")
    answers = root / ANSWERS
    if answers.is_file():
        from .configure import read_answers, write_answers

        originals.setdefault(answers, answers.read_bytes())
        state = read_answers(root)
        entries, keys = state.setdefault("answers", {}), set(state.get("manifestKeys", []))
        for key, value in answered.items():
            if value is None:
                entries.pop(key, None)
                keys.discard(key)
            else:
                entries[key] = {"value": value, "by": entries.get(key, {}).get("by", "agent")}
                keys.add(key)
        state["manifestKeys"] = sorted(keys)
        if state.get("manifest") == sha256(raw):
            state["manifest"] = sha256(text.encode("utf-8"))
        write_answers(root, state)
    return True


ROOT_OF_WRAPPER = '"$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)/'
WRAPPER_PLACE = re.compile(r"BASH_SOURCE|dirname|\$\{?0\b")
SHELL_SYNTAX = frozenset("|&;<>()$`*?[]~#{}!\\")


def wrapper_command(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if len(lines) != 1 or not lines[0].startswith("exec ") or lines[0].removeprefix("exec ").lstrip().startswith("-"):
        return None
    command = lines[0].removeprefix("exec ").strip().removesuffix(' "$@"')
    if command.startswith(ROOT_OF_WRAPPER) and "/" not in command[len(ROOT_OF_WRAPPER):].split('"', 1)[0]:
        command = command.replace(ROOT_OF_WRAPPER, '"./', 1)
    command = command.replace(ROOT_OF_WRAPPER, '"')
    if not command or WRAPPER_PLACE.search(command):
        return None
    if SHELL_SYNTAX & set(command):
        return command
    try:
        return shlex.join(shlex.split(command))
    except ValueError:
        return None


def setup_wrappers(root: Path) -> list[Path]:
    return sorted(path for path in (root / SETUP_WRAPPERS).rglob("*") if path.is_file())


@contextmanager
def setup_moved(root: Path, dry_run: bool, temporary: bool):
    phases: dict[str, list[tuple[Path, str | None]]] = {}
    for phase in [] if temporary else SETUP_PHASES:
        for path in sorted((root / SETUP_WRAPPERS / phase).glob("*.sh")):
            if path.is_file():
                phases.setdefault(phase, []).append((path, wrapper_command(path.read_text(encoding="utf-8", errors="replace"))))
    moved = {path: (phase, command) for phase, found in phases.items()
             if all(command is not None for _, command in found) for path, command in found}
    kept = [path.relative_to(root).as_posix() for found in phases.values() for path, _ in found if path not in moved]
    if kept:
        print(msg("setup_kept", ", ".join(kept)), file=sys.stderr)
    shown = ", ".join(f"{path.relative_to(root).as_posix()} -> {SETUP}.{phase}: {command}" for path, (phase, command) in moved.items())
    if not moved or dry_run:
        if shown:
            print(msg("setup_would_move", shown), file=sys.stderr)
        yield
        return
    originals: dict[Path, bytes | None] = {}
    modes: dict[Path, int] = {}

    def edit(project: dict) -> dict:
        setup = project.setdefault(SETUP, {})
        if not isinstance(setup, dict) or not all(isinstance(setup.get(phase, []), list) for phase in SETUP_PHASES):
            raise Refusal(msg("param_setup_shape", SETUP, ", ".join(SETUP_PHASES)))
        listed = {phase: set(setup.get(phase, [])) for phase in SETUP_PHASES}
        for phase, command in moved.values():
            if command not in listed[phase]:
                setup.setdefault(phase, []).append(command)
        return {SETUP: setup}

    try:
        if not manifest_rewritten(root, originals, edit):
            yield
            return
        for path in moved:
            originals[path], modes[path] = path.read_bytes(), path.stat().st_mode
            path.unlink()
            prune(root, path)
        yield
    except BaseException:
        for path, content in originals.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if path in modes:
                os.chmod(path, modes[path])
        raise
    print(msg("setup_moved", shown), file=sys.stderr)


@contextmanager
def moved_layer(root: Path, dry_run: bool = False, temporary: bool = False):
    legacy, target = root / LEGACY_ROOT, root / LOCK.split("/", 1)[0]
    if not (legacy / LEGACY_STATE / "lock.json").is_file():
        yield
        return
    if dry_run:
        raise Refusal(msg("relocate_needed", LEGACY_ROOT))
    state = (root / LOCK).parent
    for taken in (target, legacy / state.name):
        if taken.exists():
            raise Refusal(msg("relocate_both", LEGACY_ROOT, taken.relative_to(root).as_posix()))
    legacy.rename(target)
    (target / LEGACY_STATE).rename(state)
    recorded = (root / LOCK).read_bytes()
    try:
        old_state, new_state = f"{LEGACY_ROOT}/{LEGACY_STATE}/", LOCK.rsplit("/", 1)[0] + "/"

        def moved(path: str) -> str:
            if path.startswith(old_state):
                return new_state + path[len(old_state):]
            return target.name + path[len(LEGACY_ROOT):] if path.startswith(LEGACY_ROOT + "/") else path

        rewrite_lock(root, moved)
        yield
    except BaseException:
        (root / LOCK).write_bytes(recorded)
        state.rename(target / LEGACY_STATE)
        target.rename(legacy)
        raise
    if temporary:
        (root / LOCK).write_bytes(recorded)
        state.rename(target / LEGACY_STATE)
        target.rename(legacy)
        return
    print(msg("relocated", LEGACY_ROOT, target.name), file=sys.stderr)


def read_lock(root: Path) -> dict:
    path = root / LOCK
    if not path.exists():
        if (root / LEGACY_ROOT / LEGACY_STATE / "lock.json").is_file():
            raise Refusal(msg("relocate_needed", LEGACY_ROOT))
        return {"files": {}}
    if (root / LEGACY_MANIFEST).is_file():
        raise Refusal(msg("relocate_needed", LEGACY_MANIFEST))
    return load_json(path, msg("lock_broken", path))


ANSWERS = ".harness/project/answers.json"
CONFIG = "config.json"
AGENT = "AGENT.md"
GUIDES = (AGENT, *(f"installer/stages/STAGE-{stage}.md" for stage in (1, 2, 3)))
SEED_FOLDERS = ("docs/platform/delivery/", "docs/platform/operations/")
GUIDE_TEMPLATES = "templates"
SHAPES = {"fullstack": SIDES, "backend": ("backend",), "frontend": ("frontend",)}
OFF, NONE, ON = "off", "none", "on"
OPTIONS_MAX = 4
EVIDENCE_MAX = 4
IDENTITY = ("project.name", "project.gitConfigPrefix", "git.baseBranch", "git.releaseBranch", "git.branchConvention")
BRANCH_CONVENTIONS = ("conventional", "gitflow")
APP_FIELDS = ("path", "side", "stack", "check", "entries", "configs", "architectureWaiver", "doc", "skill")


@functools.cache
def layer_manifest() -> dict:
    return runpy.run_path(str(HERE / "payload" / ".harness" / "lib" / "manifest.py"))


def app_roles() -> tuple[str, ...]:
    return layer_manifest()["APP_ROLES"]


DOCS = {"index": "docs/README.md", "instructionRoots": [""],
        "machineTextPrefixes": [".agents/", ".claude/", ".codex/", ".cursor/", ".specify/", "specs/",
                                ".harness/hooks/", ".harness/prompts/", ".harness/review/"],
        "liveDocPrefixes": ["docs/features/"], "typedPrefixes": ["docs/"],
        "generatedSegments": ["/node_modules/", "/bin/", "/obj/", "/dist/", "Migrations/"],
        "historicalPrefixes": [], "generatedPrefixes": [], "vendoredSkills": [], "comments": {}, "checks": []}
DOCS_ASKED = {
    "generatedSegments": lambda: (msg("q_docs_generated_segments"), msg("rule_docs_generated_segments")),
    "generatedPrefixes": lambda: (msg("q_docs_generated_prefixes"), msg("rule_docs_generated_prefixes")),
    "machineTextPrefixes": lambda: (msg("q_docs_machine_text_prefixes"), msg("rule_docs_machine_text_prefixes")),
    "liveDocPrefixes": lambda: (msg("q_docs_live_doc_prefixes"), msg("rule_docs_live_doc_prefixes")),
    "historicalPrefixes": lambda: (msg("q_docs_historical_prefixes"), msg("rule_docs_historical_prefixes")),
    "vendoredSkills": lambda: (msg("q_docs_vendored_skills"), msg("rule_docs_vendored_skills")),
}


def runtime_files(root: Path) -> tuple[str, ...]:
    package = root / "installer"
    return ("ah.py", "messages.env", CATALOG, *(
        path.relative_to(root).as_posix() for path in sorted(package.rglob("*.py"))
        if "__pycache__" not in path.parts))
