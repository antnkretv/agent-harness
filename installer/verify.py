from __future__ import annotations

import argparse
from collections import Counter
import functools
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .catalog import enabled, is_on, load_catalog, lookup, modules_of, validate, wanted
from .configure import manifest_stale, module_evidence, read_answers
from .detect import Tree
from .documents import block_text, markers, outside_text
from .questions import Session
from .render import app_prefix, plan
from .resources import (
    ANSWERS,
    AREA_MAP,
    HERE,
    INSTALLER,
    LOCK,
    LINE,
    METHODOLOGY,
    PROJECT,
    Refusal,
    SERVICE_AREAS,
    SETUP,
    SETUP_PHASES,
    git,
    index_modes,
    kit_tree,
    layer_manifest,
    load_json,
    manifest_moved,
    msg,
    read_lock,
    repository,
    say,
    setup_wrappers,
    tracks_modes,
)
from .state import Installer, version_tuple
from .links import installed_links, installed_test
from .migrate import ENTRY_LINES, unwired


SHELL_BUILTINS = frozenset({"cd", "test", "[", "exit", "true", ":", "export", "set", "source", ".", "eval", "exec", "command"})


@dataclass
class Finding:
    level: str
    text: str


def undeclared(root: Path, project: dict, catalog: dict) -> list[Finding]:
    found = []
    session = Session(catalog, Tree(root), {})
    for name in catalog["order"]:
        entry = modules_of(project).get(name)
        if is_on(entry) or isinstance(entry, dict) and str(entry.get("off") or "").strip():
            continue
        evidence = module_evidence(session, name)
        if evidence:
            found.append(Finding("error", msg("verify_undeclared", name, evidence[0])))
    return found


def manifest_synced(root: Path) -> list[Finding]:
    if not (root / ANSWERS).is_file() or not manifest_stale(root, read_answers(root)):
        return []
    return [Finding("error", msg("verify_manifest_stale", PROJECT, ANSWERS, INSTALLER))]


def setup_left(root: Path) -> list[Finding]:
    lists = ", ".join(f"{SETUP}.{phase}" for phase in SETUP_PHASES)
    return [Finding("error", msg("verify_setup_wrapper", path.relative_to(root).as_posix(), lists, PROJECT))
            for path in setup_wrappers(root)]


def integrity(root: Path, lock: dict) -> list[Finding]:
    found = []
    managed = "".join(path + "\0" for path, entry in lock.get("files", {}).items() if entry.get("role") != "seed")
    done = subprocess.run(["git", "check-ignore", "--verbose", "--stdin", "-z"], cwd=root, input=managed.encode("utf-8"),
                          capture_output=True)
    if done.returncode not in (0, 1):
        found.append(Finding("error", msg("verify_ignore_failed", done.stderr.decode("utf-8", "replace").strip())))
    fields = done.stdout.decode("utf-8").split("\0")
    records = [fields[start:start + 4] for start in range(0, len(fields) - 3, 4)]
    for source, number, pattern, path in sorted(records, key=lambda record: record[3]):
        if not pattern.startswith("!") and Path(source).name == ".gitignore" and not Path(source).is_absolute():
            found.append(Finding("error", msg("verify_ignored", path, pattern, source, number)))
    tracked = index_modes(root)
    hidden = [path for path, entry in lock.get("files", {}).items() if path not in tracked and (root / path).is_file()]
    if hidden:
        done = subprocess.run(["git", "check-ignore", "--verbose", "--stdin", "-z"], cwd=root,
                              input="".join(path + "\0" for path in hidden).encode("utf-8"), capture_output=True)
        fields = done.stdout.decode("utf-8").split("\0")
        for source, number, pattern, path in sorted((fields[start:start + 4] for start in range(0, len(fields) - 3, 4)),
                                                    key=lambda record: record[3]):
            if pattern == "/" + path:
                found.append(Finding("error", msg("verify_hidden", path, pattern, source, number)))
    return found


def outside_blocks(root: Path, lock: dict) -> list[Finding]:
    found = []
    for path, entry in sorted(lock.get("files", {}).items()):
        target = root / path
        if entry.get("role") == "block" and target.is_file() and outside_text(
                path, target.read_text(encoding="utf-8", errors="replace"), entry.get("block", "core")):
            found.append(Finding("error", msg("verify_outside_blocks", path, INSTALLER)))
    return found


def modes(root: Path, lock: dict, kit: dict) -> list[Finding]:
    if tracks_modes(root):
        return []
    index, files = index_modes(root), kit.get("files", {})
    return [Finding("error", msg("verify_mode", path)) for path in sorted(lock.get("files", {}))
            if files.get(path, {}).get("executable") and index.get(path) == "100644"]


def twice(data: dict) -> list[str]:
    counts = Counter(m.get("id") for m in data.get("modules", []) if isinstance(m, dict))
    return sorted((name for name, count in counts.items() if count > 1), key=str)


def rule_registry(methodology: dict, project: dict) -> dict:
    return {"modules": shipped_module("lib/manifest.py").rule_records(methodology, project)}


def registry(root: Path, methodology: dict, project: dict, data: dict) -> list[Finding]:
    known = {rule.get("id") for rule in methodology.get("modules", []) if isinstance(rule, dict)}
    own = project.get("rules") if isinstance(project.get("rules"), dict) else {}
    found = [Finding("error", msg("verify_disabled_unknown", entry.get("id"))) for entry in own.get("disabled", [])
             if not isinstance(entry, dict) or entry.get("id") not in known]
    found += [Finding("error", msg("verify_module_twice", name)) for name in twice(data)]
    modules = {m.get("id"): m for m in data.get("modules", []) if isinstance(m, dict)}
    for module in modules.values():
        command = module.get("command")
        if command is not None and not (isinstance(command, list) and command and all(isinstance(a, str) for a in command)
                                        and module.get("gate") in command):
            found.append(Finding("error", msg("verify_command_invalid", module.get("id"))))
        if module.get("enabled", True):
            gate = module.get("gate")
            if gate and not (root / gate).exists():
                found.append(Finding("error", msg("verify_gate_missing", module.get("id"), gate)))
        elif not module.get("disabledReason") or not module.get("replacement"):
            found.append(Finding("error", msg("verify_disabled_silent", module.get("id"))))
    shipped = load_json(HERE / "payload" / METHODOLOGY, msg("no_methodology", HERE))
    for rule in shipped.get("modules", []):
        if isinstance(rule, dict) and rule.get("id") not in modules:
            found.append(Finding("error", msg("verify_module_missing", rule.get("id"))))
    return found


def located(tool: str) -> str | None:
    local = os.path.expanduser(f"~/.local/bin/{tool}")
    return shutil.which(executable_resolver()(tool)) or (local if os.path.exists(local) else None)


def shipped(relative: str) -> str:
    kit = HERE / "payload/.harness" / relative
    return str(kit if kit.exists() else HERE.parent / relative)


def expanded(command: list[str]) -> list[str]:
    return [shipped(part[len("{platform}/"):]) if part.startswith("{platform}/") else part for part in command]


def found_version(probe: list[str]) -> str | None:
    probe = expanded(probe)
    path = located(probe[0])
    if path is None:
        return None
    done = subprocess.run([path, *probe[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace")
    match = re.search(r"\d+(?:\.\d+)+", done.stdout + done.stderr)
    return match.group(0) if match else None if done.returncode else "?"


QUIET = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
STATUS_SECONDS = 30


def status_of(command: list[str], root: Path) -> tuple[bool, str]:
    path = located(command[0])
    if path is None:
        return False, msg("doctor_absent", command[0])
    env = {**os.environ, **QUIET}
    if not (env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH")
            or git(root, "config", "--get", "core.sshCommand", check=False)):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    try:
        done = run_gate(root, [path, *command[1:]], env=env, timeout=STATUS_SECONDS)
    except subprocess.TimeoutExpired:
        return False, msg("doctor_timeout", STATUS_SECONDS)
    return done.returncode == 0, "" if done.returncode == 0 else (done.stderr or done.stdout).strip() or str(done.returncode)


@dataclass
class Requirement:
    name: str
    need: str
    expected: str
    found: str
    level: str
    purpose: str = ""
    detail: str = ""


def requirements(kit: dict, project: dict, ci: bool, root: Path | None = None, local: bool = True) -> list[Requirement]:
    rows = []
    paths = enabled(project)
    for item in kit.get("dependencies", []):
        if item.get("pinSetting"):
            item = {**item, "pinned": lookup(project, item["pinSetting"])}
        need = item["need"]
        expected = item.get("minimum") and ">= " + item["minimum"] or item.get("pinned") or item.get("verified") or "-"
        if not wanted(item, paths) or item.get("local") and (ci or not local):
            continue
        hard = need == "required" or (need == "ci" and ci)
        miss = "error" if hard else ("warning" if need == "ci" else "info")
        if "env" in item:
            names = [name for name in item["env"] if os.environ.get(name)]
            rows.append(Requirement(item["name"], need, " | ".join(item["env"]), names[0] if names else "-",
                                    "ok" if names else miss, item.get("purpose", "")))
            continue
        if "status" in item:
            ok, detail = status_of(item["status"], root or Path.cwd())
            rows.append(Requirement(item["name"], need, "-", "ok" if ok else "-", "ok" if ok else miss,
                                    item.get("purpose", ""), detail))
            continue
        if "probe" not in item:
            rows.append(Requirement(item["name"], need, expected, item.get("source", "-"), "info", item.get("purpose", "")))
            continue
        version = found_version(item["probe"])
        if version is None and item.get("alternative") and located(item["alternative"]):
            rows.append(Requirement(item["name"], need, expected, item["alternative"], "ok", item.get("purpose", "")))
            continue
        if version is None:
            level = miss
        elif item.get("minimum") and version != "?" and version_tuple(version) < version_tuple(item["minimum"]):
            level = "error" if hard else "warning"
        elif item.get("pinned") and version != item["pinned"]:
            level = "warning"
        else:
            level = "ok"
        rows.append(Requirement(item["name"], need, expected, version or "-", level, item.get("purpose", "")))
    return rows


def tools(kit: dict, project: dict) -> list[Finding]:
    found = []
    for row in requirements(kit, project, False, local=False):
        if row.level in ("error", "warning"):
            text = msg("verify_tool", row.name, row.expected) if row.found == "-" else \
                msg("verify_tool_version", row.name, row.expected, row.found)
            found.append(Finding(row.level, text))
    return found


def doctor(args: argparse.Namespace) -> int:
    root = repository(args.target)
    kit = load_json(HERE / "kit.json", msg("no_kit", HERE))
    project_path = root / PROJECT
    project = load_json(project_path, msg("no_project", project_path)) if project_path.exists() else {}
    rows = requirements(kit, project, args.ci, root)
    width = max(len(row.name) for row in rows)
    for row in rows:
        print(f"  {row.level:<8} {row.name:<{width}}  {row.need:<13} {row.expected:<12} {row.found:<10} {row.purpose}")
        for line in row.detail.splitlines() if row.level != "ok" else []:
            print(f"           {line}")
    errors = sum(row.level == "error" for row in rows)
    say("doctor_summary", errors, sum(row.level == "warning" for row in rows))
    return 1 if errors else 0


def area_map(root: Path, project: dict) -> list[Finding]:
    carte = load_json(root / AREA_MAP, msg("no_area_map", root))
    areas = carte.get("areas", {})
    found = [Finding("error", msg("verify_area_missing", name)) for name in SERVICE_AREAS if name not in areas]
    for app in project.get("apps", []):
        if not isinstance(app, dict) or not isinstance(app.get("path"), str):
            continue
        area = areas.get(app.get("id"))
        if area is None:
            found.append(Finding("error", msg("verify_area_missing", app.get("id"))))
        elif not any(app_prefix(app["path"]).startswith(prefix) for prefix in area.get("prefixes", [])):
            found.append(Finding("error", msg("verify_area_uncovered", app.get("id"), app["path"])))
    return found


def app_entries(root: Path, project: dict) -> list[Finding]:
    layer, executable = layer_manifest(), executable_resolver()
    found = []
    for app in project.get("apps", []) if isinstance(project.get("apps"), list) else []:
        if not isinstance(app, dict):
            continue
        for role, entry in layer["app_entries"](app).items():
            try:
                words = shlex.split(entry["command"])
            except ValueError:
                words = []
            program = next((word for word in words if not re.fullmatch(r"\w+=.*", word)), "")
            if program in SHELL_BUILTINS:
                continue
            if not (Path(executable(program)).is_file() or (root / entry["cwd"] / program).is_file()):
                found.append(Finding("error", msg("verify_entry_program", app.get("id"), role, program)))
        if unset := layer["unset_roles"](app):
            found.append(Finding("unset", msg("verify_entries_unset", app.get("id"), ", ".join(unset))))
    return found


def disabled_gates(data: dict) -> dict[str, str]:
    return {module["gate"]: module.get("id", "") for module in data.get("modules", [])
            if isinstance(module, dict) and module.get("gate") and not module.get("enabled", True)}


def gate_off(title: str, script: str, off: dict[str, str], found: list[Finding]) -> bool:
    if script in off:
        found.append(Finding("warning", msg("verify_gate_off", title, off[script])))
    return script in off


@functools.cache
def shipped_module(relative: str):
    spec = importlib.util.spec_from_file_location("harness_" + Path(relative).stem, shipped(relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def executable_resolver():
    return shipped_module("lib/executables.py").executable


def run_gate(root: Path, command: list[str], **extra) -> subprocess.CompletedProcess:
    if os.name == "nt":
        program = command[0]
        command = [executable_resolver()(program, path=extra.get("env", os.environ).get("PATH")), *command[1:]]
    return subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", **extra)


def gates(root: Path, off: dict[str, str], absent: frozenset[str]) -> list[Finding]:
    found = []
    python = sys.executable
    scripts = ".harness/scripts/check/"
    checks = [
        [python, scripts + "check-pipeline.py"],
        [python, scripts + "check-agent-permissions.py"],
        [python, scripts + "check-docs.py", "--area", "agents"],
        [python, scripts + "check-docs.py", "--area", "docs", "--anchors"],
        [python, scripts + "check-text-registry.py"],
        [python, scripts + "check-comments.py", "--all"],
        [python, scripts + "check-adr-refs.py"],
        [python, scripts + "check-area-profiles.py"],
        [python, scripts + "check-review-label.py"],
    ]
    for command in checks:
        title = " ".join([os.path.basename(command[1]), *command[2:]])
        if command[1] in absent or gate_off(title, command[1], off, found):
            continue
        if not (root / command[1]).exists():
            found.append(Finding("error", msg("verify_gate_absent", title)))
            continue
        done = run_gate(root, command)
        if done.returncode != 0:
            tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-15:])
            found.append(Finding("error", msg("verify_gate_failed", title, done.returncode) + "\n" + tail))
    return found


PROBE_WORDS = 1000


def probes(root: Path, project: dict, off: dict[str, str], absent: frozenset[str]) -> list[Finding]:
    found = []
    python = sys.executable
    templates = {key: value for path in (".harness/scripts/messages.env", ".harness/messages.env", ".harness/project/messages.env") if (root / path).is_file()
                 for key, value in LINE.findall((root / path).read_text(encoding="utf-8"))}
    with tempfile.TemporaryDirectory(dir=root) as folder:
        work = Path(folder)
        bad_commit = work / "COMMIT_EDITMSG"
        bad_commit.write_text("bad header without type\n", encoding="utf-8", newline="\n")
        bad_comment = work / "probe.sh"
        bad_comment.write_text("#!/usr/bin/env bash\n" + msg("probe_history_comment") + "\necho ok\n", encoding="utf-8", newline="\n")
        bad_adr = work / "probe.md"
        bad_adr.write_text(msg("probe_adr_text", "ADR-%04d" % 91) + "\n", encoding="utf-8", newline="\n")
        tool = "" if ".harness/hooks/forge-text-lint.py" in absent else run_gate(
            root, [python, ".harness/lib/forge.py", "comment-tool"], check=True).stdout.strip()
        text = " ".join([msg("probe_volume_word")] * PROBE_WORDS)
        overflow = json.dumps({"tool_name": tool, "tool_input": {"body": text}}, ensure_ascii=False)
        branch = "" if ".harness/scripts/check/check-pr-issue-link.py" in absent else run_gate(
            root, [python, ".harness/lib/tracker.py", "probe-branch"], check=True).stdout.strip()
        cases = [
            ("commit-format.sh", ["bash", ".harness/scripts/check/commit-format.sh", bad_commit.relative_to(root).as_posix()], {}, None, "commit_bad_header"),
            ("check-adr-refs.py", [python, ".harness/scripts/check/check-adr-refs.py", "--files", str(bad_adr)], {}, None, "adr_no_area"),
            ("check-pr-issue-link.py", [python, ".harness/scripts/check/check-pr-issue-link.py"],
             {"PR_BRANCH": branch, "PR_NUMBER": "", "PR_BODY": msg("probe_pr_body")}, None, "pr_issue_missing"),
        ]
        history = project.get("docs", {}).get("comments", {}).get("history", [])
        if any(re.search(pattern, msg("probe_history_comment"), re.IGNORECASE) for pattern in history):
            cases.append(("check-comments.py", [python, ".harness/scripts/check/check-comments.py", "--files", str(bad_comment)],
                          {}, None, "comments_failed"))
        if tool:
            cases.append(("forge-text-lint.py", [python, ".harness/hooks/forge-text-lint.py"], {}, overflow, "textlint_words_over"))
        prose = ".harness/scripts/check/vale-lint.py"
        if located("vale"):
            cases.append(("vale-lint.py", [python, prose, "--stdin", "--ext", ".publish.md"], {}, msg("probe_prose_text"), "vale_stdin_failed"))
        elif prose not in absent:
            found.append(Finding("warning", msg("verify_probe_skipped", "vale-lint.py", "vale")))
        env = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", ""))
        for title, command, extra, stdin, key in cases:
            if command[1] in absent or gate_off(title, command[1], off, found):
                continue
            done = run_gate(root, command, env={**env, **extra}, input=stdin)
            pattern = ".*?".join(re.escape(part) for part in re.split(r"%[sd]", templates[key]))
            try:
                verdict = json.loads(done.stdout)
            except ValueError:
                verdict = None
            if isinstance(verdict, dict):
                denied = (done.returncode == 0 and verdict.get("deny") is True
                          and re.search(pattern, str(verdict.get("reason", "")), re.S) is not None)
            else:
                denied = done.returncode == 1 and re.search(pattern, done.stdout + done.stderr, re.S) is not None
            if not denied:
                found.append(Finding("error", msg("verify_probe_failed", title, done.returncode)
                                     + "\n" + (done.stdout + done.stderr).strip()))
    return found


HARNESS_FILES = ".harness/scripts/check/check-harness-files.py"


def edited(root: Path, kit: dict, catalog: dict, project: dict, lock: dict) -> list[Finding]:
    every = plan(HERE, kit, catalog, project, root, lock)
    planned = {item.path: item for item in every if item.place == "shell"}
    installer = Installer(root, True)
    installer.shell(every, lock, frozenset(kit.get("released", [])))
    changed = {path for path, _ in installer.report}
    index = index_modes(root)
    found = [Finding("error", msg("verify_tracked_lock", item.path)) for item in every if item.place != "shell" and item.path in index]
    for path, item in sorted(planned.items()):
        if item.role == "block":
            pair, target = markers(path, item.block), root / path
            mine = block_text(target.read_bytes(), *pair) if target.is_file() else None
            if mine != block_text(item.content, *pair):
                found.append(Finding("error", msg("verify_edited_block", path)))
        elif path in changed and item.role == "json":
            found.append(Finding("error", msg("verify_edited_json", path)))
        elif path in changed and item.role == "managed":
            found.append(Finding("error", msg("verify_edited_managed", path, item.extend or msg("verify_extend_default"))))
    return found


def installed_kit(root: Path) -> tuple[dict, dict]:
    lock = read_lock(root)
    if not lock.get("kit"):
        raise Refusal(msg("verify_no_lock", LOCK))
    kit = load_json(HERE / "kit.json", msg("no_kit", HERE))
    pinned, running = lock["kit"].get("tree", ""), kit_tree(HERE)
    if lock["kit"].get("version") != kit.get("version") or pinned and running and pinned != running:
        raise Refusal(msg("verify_other_kit", lock["kit"].get("version"), pinned[:12] or "-", kit.get("version"), running[:12] or "-"))
    return lock, kit


def project_files(root: Path, kit: dict, catalog: dict, project: dict, lock: dict) -> list[Finding]:
    placed = {item.path for item in plan(HERE, kit, catalog, project, root, lock) if item.place == "lock"}
    tracked = set(filter(None, git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")))
    installed = installed_test(tracked, placed)
    found = []
    for path in sorted(path for path in tracked if path.endswith(".md") and (root / path).is_file()):
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        found += [Finding("error", msg("verify_installed_link", path, number, target))
                  for number, target in installed_links(path, text, installed)]
    for path in ENTRY_LINES:
        if path == "CLAUDE.md" and "agents/claude-code" not in enabled(project):
            continue
        text = (root / path).read_text(encoding="utf-8", errors="replace") if (root / path).is_file() else ""
        found += [Finding("error", msg("verify_entry_line", path, line)) for line in unwired(path, text)]
    return found


def verify(args: argparse.Namespace) -> int:
    root = repository(args.target)
    lock, kit = installed_kit(root)
    project = load_json(root / PROJECT, msg("no_project", root / PROJECT))
    catalog = load_catalog()
    findings = [Finding("error", line) for line in validate(project, root, catalog)]
    findings += manifest_synced(root)
    findings += setup_left(root)
    if manifest_moved(root, lock):
        findings.append(Finding("error", msg("verify_manifest_moved", PROJECT, LOCK, INSTALLER)))
    findings += undeclared(root, project, catalog)
    findings += integrity(root, lock)
    findings += modes(root, lock, kit)
    findings += outside_blocks(root, lock)
    methodology = load_json(root / METHODOLOGY, msg("no_methodology", root))
    rules = rule_registry(methodology, project)
    findings += registry(root, methodology, project, rules)
    findings += area_map(root, project)
    findings += app_entries(root, project)
    findings += tools(kit, project)
    hooks = git(root, "config", "--get", "core.hooksPath", check=False)
    if "githooks" in enabled(project) and hooks != ".githooks":
        findings.append(Finding("error", msg("verify_hooks_path", hooks or "-")))
    off = disabled_gates(rules)
    if HARNESS_FILES not in off:
        findings += edited(root, kit, catalog, project, lock)
    findings += project_files(root, kit, catalog, project, lock)
    absent = frozenset(lock.get("absent", []))
    if not args.no_gates:
        findings += gates(root, off, absent)
        if not args.no_probes:
            findings += probes(root, project, off, absent)
    errors = [f for f in findings if f.level == "error"]
    marks = {"error": "ERROR ", "warning": "WARN  ", "unset": "NONE  "}
    for finding in findings:
        print(marks[finding.level] + finding.text)
    say("verify_summary", lock["kit"].get("version"), len(errors),
        sum(finding.level == "warning" for finding in findings))
    return 1 if errors else 0


def status(args: argparse.Namespace) -> int:
    root = repository(args.target)
    lock, kit = installed_kit(root)
    project = load_json(root / PROJECT, msg("no_project", root / PROJECT))
    installer = Installer(root, True)
    installer.shell(plan(HERE, kit, load_catalog(), project, root, lock), lock, frozenset(kit.get("released", [])))
    for path, state in installer.report:
        print(f"  {state:<14} {path}")
    findings = integrity(root, lock)
    for finding in findings:
        print(("ERROR " if finding.level == "error" else "WARN  ") + finding.text)
    say("status_summary", lock["kit"].get("version"), len(installer.report), len(findings))
    return 1 if any(f.level == "error" for f in findings) else 0
