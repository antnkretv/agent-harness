#!/usr/bin/env python3
"""Все гейты проекта одним прогоном: реестр правил, области диффа и проверки приложений.

Провайдер CI зовет только этот вход, поэтому состав проверок у всех провайдеров один.
Порядок, условия и отказы - docs/platform/delivery/ci-cd.md, раздел "Гейт проекта".

    ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --base origin/develop
    ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --list             # план без запуска
    ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --since <ревизия>  # приложения - по диффу от прошлого зеленого прогона
    ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --gates-only       # приложения уже проверил CI
    ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --tree             # гейты дерева без сборки и тестов приложений
    ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --docs             # гейты документации по всему дереву: этап 2 установки
    ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --change           # только гейты данных PR при PR_NUMBER: шаг gates сессии

В CI поставщика кода журнал идет группами и аннотациями его разметки, итог ложится
на страницу прогона; разметку дает .harness/lib/forge.py, флага у нее нет.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from areas import load_map, resolve  # noqa: E402
from ci_common import BASE_BRANCH, ROOT, annotation, bash, changed_since, executable, forge, get_record, get_records, git, modules, msg, setting, shell_scripts  # noqa: E402
from manifest import own_entries, unset_roles  # noqa: E402


@dataclass
class Gate:
    module: str
    command: list[str]
    code: bool = False
    prepare: Callable[[], None] | None = None
    docs: bool = False
    cwd: str = "."
    title: str = ""
    unset: list[str] | None = None


@dataclass
class Outcome:
    title: str
    status: str
    seconds: float = 0.0
    note: str = ""
    off: str = ""


CHANGE_GATES = ("pipeline", "pr-issue-link", "pr-size")


def prose_needed(changed: list[str]) -> bool:
    return bool(subprocess.run([sys.executable, ".harness/scripts/check/vale-lint.py", "--scope"], cwd=ROOT, input="\n".join(changed),
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip())


GITLEAKS_IMAGE = ("zricethezav/gitleaks:v8.30.1@sha256:"
                  "c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f")
GITLEAKS_CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "harness",
                            GITLEAKS_IMAGE.split("@")[0].replace("/", "-").replace(":", "-"))


def git_dirs() -> tuple[str, str] | None:
    dirs = git("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir").splitlines()
    return (dirs[0], dirs[1]) if len(dirs) == 2 else None


def bind(source: str, target: str) -> list[str]:
    return ["--mount", f'type=bind,"source={source}",target={target}']


def gitleaks() -> list[str] | None:
    local = os.path.expanduser("~/.local/bin/gitleaks")
    found = shutil.which("gitleaks") or (local if os.access(local, os.X_OK) else None)
    if found:
        return [found]
    linux = sys.platform.startswith("linux")
    if linux and os.access(GITLEAKS_CACHE, os.X_OK):
        return [GITLEAKS_CACHE]
    if not shutil.which("docker"):
        return None
    if not linux:
        if not (dirs := git_dirs()):
            return None
        tree, common = dirs
        inner = "/git/" + os.path.relpath(tree, common).replace(os.sep, "/")
        return ["docker", "run", "--rm", *bind(ROOT, "/repo"), *bind(common, "/git"), "-w", "/repo",
                "-e", f"GIT_DIR={inner}", "-e", "GIT_WORK_TREE=/repo", "-e", "GIT_CONFIG_COUNT=1",
                "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*", GITLEAKS_IMAGE]
    try:
        os.makedirs(os.path.dirname(GITLEAKS_CACHE), exist_ok=True)
    except OSError:
        return None
    created = subprocess.run(["docker", "create", GITLEAKS_IMAGE], capture_output=True, text=True)
    if created.returncode != 0:
        return None
    container = created.stdout.strip()
    try:
        copied = subprocess.run(["docker", "cp", f"{container}:/usr/bin/gitleaks", GITLEAKS_CACHE], capture_output=True)
        return [GITLEAKS_CACHE] if copied.returncode == 0 else None
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True)


def secret_scan(*arguments: str, config: str = ".gitleaks.toml") -> list[str]:
    command = gitleaks()
    return [*command, *arguments, f"--config={config}", "--redact=100", "--no-banner"] if command else []


def write_ignored(path: str) -> None:
    names = [name for name in git("ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z").split("\0") if name]
    pattern = "|".join(re.escape(name).replace("/", r"[/\\]") + ("" if name.endswith("/") else "$") for name in names)
    allowed = f"\n[[allowlists]]\npaths = ['''^(?:{pattern})''']\n" if names else ""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write('[extend]\npath = ".gitleaks.toml"\n' + allowed)


def tree_scan() -> Gate:
    command, dirs = gitleaks(), git_dirs()
    if not command or not dirs:
        return Gate("secret-scan", [])
    path = os.path.join(dirs[0], "harness-gitleaks-tree.toml")
    seen = "/git/" + os.path.relpath(path, dirs[1]).replace(os.sep, "/") if command[0] == "docker" else path
    return Gate("secret-scan", secret_scan("dir", ".", config=seen), prepare=lambda: write_ignored(path))


FORMATTER_INPUTS = (".csharpierignore", ".editorconfig", "dotnet-tools.json")


def formatter_input(path: str) -> bool:
    return path.endswith(".cs") or os.path.basename(path) in FORMATTER_INPUTS


def plan(base: str, changed: list[str], docs: bool = False,
         code_changed: list[str] | None = None) -> tuple[list[Gate], list[dict]]:
    raised = resolve(changed, load_map())[0]
    code_changed = changed if code_changed is None else code_changed
    code_raised = raised if code_changed is changed else resolve(code_changed, load_map())[0]
    python = sys.executable
    scripts = ".harness/scripts/check/"
    gates = [
        Gate("secret-scan", secret_scan("git", ".", f"--log-opts={base}..HEAD"), docs=True),
        tree_scan(),
        Gate("commit-format", [python, scripts + "check-commits.py", "--base", base], docs=True),
        Gate("diagnostic-integrity", [python, scripts + "check-diagnostic-integrity.py", "--base", base]),
        Gate("text-registry", [python, scripts + "check-text-registry.py"]),
        Gate("harness-files", [python, scripts + "check-harness-files.py"], docs=True),
        Gate("workflow-shell", [python, scripts + "check-workflow-shell.py"]),
        Gate("workflow-names", [python, scripts + "check-workflow-names.py"]),
        Gate("doc-symbols", [python, scripts + "check-doc-symbols.py", "--all"], docs=True),
        Gate("docs-structure", [python, scripts + "check-docs.py", "--retired-specs", "--anchors"], docs=True),
        Gate("pipeline", [python, scripts + "check-pipeline.py"], docs=True),
        Gate("agent-permissions", [python, scripts + "check-agent-permissions.py"], docs=True),
        Gate("requirements-registry", [python, scripts + "check-requirements-registry.py", "--base", base], docs=True),
        Gate("test-usefulness", [python, scripts + "check-test-budget.py", "--base", base]),
        Gate("review-label", [python, scripts + "check-review-label.py"]),
        Gate("shell-scripts", [bash(), scripts + "check-shell.sh", *shell_scripts()]),
        Gate("area-profiles", [python, scripts + "check-area-profiles.py"]),
        Gate("comment-layer", [python, scripts + "check-comments.py", "--all"])
        if get_record("docs.comments", {}) else Gate("", [], title="comment-layer", unset=["docs.comments"]),
        Gate("adr-reference", [python, scripts + "check-adr-refs.py", *(["--docs"] if docs else [])], docs=True),
        Gate("tool-pins", [python, scripts + "check-tool-pins.py"]),
    ]
    if docs or "docs" in raised:
        gates.append(Gate("docs-structure", [python, scripts + "check-docs.py", "--area", "docs"], docs=True))
    if docs or "agents" in raised:
        gates.append(Gate("docs-structure", [python, scripts + "check-docs.py", "--area", "agents"], docs=True))
    if docs:
        gates.append(Gate("prose", [python, scripts + "vale-lint.py", "--docs"], docs=True))
    elif prose_needed(changed):
        gates.append(Gate("prose", [python, scripts + "vale-lint.py", "--base", base]))
    if os.environ.get("PR_NUMBER"):
        gates.append(Gate(CHANGE_GATES[0], [python, ".harness/scripts/pipeline/task-worktree.py", "ci", "--base", base]))
        gates.append(Gate(CHANGE_GATES[1], [python, scripts + "check-pr-issue-link.py"]))
        gates.append(Gate(CHANGE_GATES[2], [python, scripts + "check-pr-size.py", "--base", base]))
    gates += [Gate(module, record["command"] if isinstance(record["command"], list) else [])
              for module, record in modules().items() if record.get("command")]

    apps = [app for app in get_records("apps", [], file="project/manifest.json") if app.get("id") in code_raised and app.get("check")]
    stacks = {app.get("stack") for app in apps}
    if "node" in stacks:
        gates.append(Gate("node-adapter", [python, scripts + "check-node-adapter.py"], code=True))
    if setting("backend/dotnet", "solution") and ("dotnet" in stacks or any(map(formatter_input, code_changed))):
        gates.append(Gate("dotnet-format", [python, scripts + "check-dotnet-format.py"], code=True))
    if "dotnet" in stacks:
        configuration = setting("backend/dotnet", "configuration")
        gates.append(Gate("build-diagnostics", [python, scripts + "check-build-diagnostics.py", "--configuration", configuration], code=True))
    for app in apps:
        gates += [Gate("", [bash(), "-c", entry["command"]], code=True, cwd=entry["cwd"],
                       title=msg("all_role_title", app["id"], role, entry["command"]))
                  for role, entry in own_entries(app).items()]
        if unset := unset_roles(app):
            gates.append(Gate("", [], code=True, title=msg("all_roles_title", app["id"]), unset=unset))
    return gates, apps


def label(command: list[str]) -> str:
    named = [part for part in command if part.startswith((".github/", ".harness/", "--", "git", "dir"))]
    return " ".join(os.path.basename(part) if "/" in part else part for part in named[:3])


def open_group(title: str) -> None:
    print(forge.group(title) or f"\n== {title}", flush=True)


def close_group() -> None:
    end = forge.end_group()
    if end:
        print(end, flush=True)


def timed(title: str, command: list[str], cwd: str) -> Outcome:
    open_group(title)
    started = time.monotonic()
    try:
        code = subprocess.run([executable(command[0]), *command[1:]], cwd=cwd, check=False).returncode
        status, note = ("passed", "") if code == 0 else ("failed", msg("gate_exit_code", code))
    except OSError as error:
        status, note = "error", msg("all_not_started", command[0], error.strerror)
    close_group()
    return Outcome(title, status, time.monotonic() - started, note)


def gate_outcome(gate: Gate, registry: dict[str, dict]) -> Outcome:
    if gate.unset is not None:
        return Outcome(gate.title, "unset", note=msg("all_roles_unset", ", ".join(gate.unset)))
    if not gate.module:
        return timed(gate.title, gate.command, os.path.join(ROOT, gate.cwd))
    title = f"{gate.module}: {label(gate.command)}"
    record = registry.get(gate.module)
    if record is None:
        return Outcome(title, "error", note=msg("all_module_missing", gate.module))
    if not record.get("enabled", True):
        reason, replacement = record.get("disabledReason", ""), record.get("replacement", "")
        if not (reason and replacement):
            return Outcome(title, "error", note=msg("all_disabled_no_reason", gate.module))
        return Outcome(title, "skipped", note=msg("all_disabled", reason, replacement), off=record.get("disabledBy", ""))
    if not gate.command:
        return Outcome(title, "error", note=msg("all_no_runner", gate.module))
    if gate.prepare:
        gate.prepare()
    return timed(title, gate.command, ROOT)


def execute(gates: list[Gate], apps: list[dict]) -> list[Outcome]:
    registry = modules()
    outcomes = []
    if os.path.isfile(os.path.join(ROOT, "package.json")) and not os.path.isdir(os.path.join(ROOT, "node_modules")):
        outcomes.append(timed("npm ci", ["npm", "ci", "--no-audit", "--no-fund"], ROOT))
    outcomes += [gate_outcome(gate, registry) for gate in gates if not gate.code]
    for app in apps:
        outcomes.append(timed(msg("all_app_title", app["id"], app["check"]), [bash(), "-c", app["check"]],
                              os.path.join(ROOT, app["path"])))
    outcomes += [gate_outcome(gate, registry) for gate in gates if gate.code]
    return outcomes


def report(outcomes: list[Outcome]) -> int:
    marks = {"passed": "OK  ", "skipped": "SKIP", "failed": "FAIL", "error": "ERR ", "unset": "NONE"}
    lines = [f"{marks[o.status]} {o.title} ({o.seconds:.0f} s){' - ' + o.note if o.note else ''}" for o in outcomes]
    print("\n" + "\n".join(lines))
    off = sorted({o.off for o in outcomes if o.off})
    if off:
        print(msg("all_disabled_enable", ", ".join(off)))
    failed = [o for o in outcomes if o.status in ("failed", "error")]
    if forge.in_ci():
        for outcome in failed:
            print(annotation("error", "value_pair", outcome.title, outcome.note))
        forge.step_summary("\n".join(["```", *lines, "```", ""]))
    ran = [o for o in outcomes if o.status != "unset"]
    print(msg("all_failed", len(failed), len(ran)) if failed else msg("all_passed", len(ran)))
    return 1 if failed else 0


def descends(rev: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", rev, "HEAD"], cwd=ROOT,
                          capture_output=True).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=BASE_BRANCH, help="база сравнения диффа")
    parser.add_argument("--list", action="store_true", help="напечатать план и выйти")
    parser.add_argument("--since", help="голова прошлого зеленого прогона: команды приложений и кодовые гейты "
                                        "отбираются по диффу от нее, гейты дерева - по диффу от базы")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--gates-only", action="store_true",
                       help="не повторять команды приложений, выполненные CI; гейты адаптеров остаются")
    scope.add_argument("--tree", action="store_true",
                       help="гейты дерева: без команд приложений, сборки и архитектурных тестов")
    scope.add_argument("--docs", action="store_true",
                       help="только гейты документации, по всему дереву: этап 2 установки harness")
    scope.add_argument("--change", action="store_true",
                       help="только гейты данных запроса на слияние: связь с задачей, размер и сверка коммитов с веткой")
    args = parser.parse_args()

    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", args.base], cwd=ROOT,
                      capture_output=True).returncode != 0:
        print(msg("all_no_base", args.base))
        return 2
    os.environ["HARNESS_BASE"] = args.base
    changed = changed_since(args.base)
    since = args.since if args.since and descends(args.since) else None
    if args.since and not since:
        print(msg("all_since_ignored", args.since))
    gates, apps = plan(args.base, changed, args.docs, changed_since(since) if since else None)
    if args.tree:
        gates = [gate for gate in gates if not gate.code]
    if args.docs:
        gates = [gate for gate in gates if gate.docs]
    if args.change:
        gates = [gate for gate in gates if gate.module in CHANGE_GATES]
    if args.gates_only or args.tree or args.docs or args.change:
        apps = []
    if args.list:
        print(json.dumps({"changed": len(changed), "since": since, "gates": [g.module or g.title for g in gates if g.unset is None],
                          "apps": [app["id"] for app in apps],
                          "unset": {g.title: g.unset for g in gates if g.unset is not None}}, ensure_ascii=False, indent=2))
        return 0
    return report(execute(gates, apps))


if __name__ == "__main__":
    sys.exit(main())
