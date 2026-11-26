"""Текст для людей живет в реестре своего корня.

Поверхность - способ, которым текст доходит до читателя. Каждая поверхность -
функция из таблицы SURFACES: она обходит свои файлы и возвращает нарушения.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-text-registry.py
"""

from __future__ import annotations
import argparse
import ast
import os
import posixpath
import re
from collections.abc import Set
from ci_common import ROOT, generated_path, lock_files, msg, tracked as listed
from generated_sources import VENDORED
from code_text import frontend_surface, js_surface, powershell_surface, python_surface
from shell_text import MSG_LINE, WORKFLOW_ROOT, balanced, lex, shell_surface, workflow_surface
from text_primitives import KEY, Violation, is_shell, line_of
from text_templates import surface as template_surface

CI_ROOTS = (WORKFLOW_ROOT, ".gitlab-ci.yml", ".gitlab/ci/")


def ci_yaml(path: str) -> bool:
    return path.startswith(CI_ROOTS) and path.endswith((".yml", ".yaml"))
TESTS = re.compile(r"(^|/)(tests?|Tests|__tests__)/|\.test\.|(^|/)test_[^/]*\.py$|_test\.py$")

SHARED_REGISTRY = ".harness/provisioning/messages.env"
PROJECT_REGISTRY = ".harness/project/messages.env"
LAYER_ROOTS = (".harness/", ".github/", ".githooks/", ".gitlab/", ".gitlab-ci.yml")

BORROWED_REGISTRY = {
    ".githooks/": ".harness/scripts/messages.env",
    ".harness/project/checks/": ".harness/scripts/messages.env",
    ".github/workflows/": ".harness/scripts/messages.env",
    ".github/actions/": ".harness/scripts/messages.env",
    ".claude/": ".harness/messages.env",
    ".codex/": ".harness/messages.env",
}

SPEC = re.compile(r"%(.)", re.DOTALL)


def tracked() -> list[str]:
    return [p for p in listed()
            if not p.startswith(VENDORED) and not TESTS.search(p) and not generated_path(p)]


def read(path: str) -> str:
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return ""


JQ_FORMATS = r"(?:text|json|html|uri|csv|tsv|sh|base32d?|base64d?)\b"

FUNCTION_DECLARATION = re.compile(
    r"^[ \t]*(?:declare|typeset)[ \t]+-[A-Za-z]*[fF][A-Za-z]*[ \t]+"
    r"(?:[A-Za-z_][A-Za-z0-9_-]*[ \t]*)+(?:#.*)?$", re.MULTILINE)

SHELL_USE = (
    re.compile(rf"(?:^|[\s(])@(?!{JQ_FORMATS})({KEY})(?=[\s;)\"'|&]|$)", re.MULTILINE),
    re.compile(rf"(?:\b|_)msg[ \t]+({KEY})\b"),
    re.compile(rf"\$\{{?MSG_({KEY})"),
    re.compile(rf"process\.env\.MSG_({KEY})"),
)

PYTHON_USE = (
    re.compile(rf"""\b(?:msg|say|warn|fail|die)\(\s*["']({KEY})["']"""),
    re.compile(rf"""\bannotation\(\s*["']\w+["']\s*,\s*["']({KEY})["']"""),
)

POWERSHELL_USE = (re.compile(rf"""Get-[A-Za-z][A-Za-z0-9]*Message\s+['"]?({KEY})"""),)

WORKFLOW_MSG = re.compile(rf"^[ \t]+MSG_({KEY}):[ \t]*'([^']*)'", re.MULTILINE)


def uses(path: str, text: str) -> list[tuple[str, int]]:
    if path.endswith(".py"):
        patterns = PYTHON_USE
    elif path.endswith(".ps1"):
        patterns = POWERSHELL_USE
    elif path.startswith(WORKFLOW_ROOT):
        patterns = SHELL_USE + PYTHON_USE
    elif ci_yaml(path) or is_shell(path, text) or os.path.basename(path) == "Dockerfile":
        patterns = SHELL_USE
    else:
        return []
    code = "\n".join("" if line.lstrip().startswith("#") else line for line in text.split("\n"))
    code = MSG_LINE.sub(lambda m: "\n" * m.group(0).count("\n"), code)
    code = WORKFLOW_MSG.sub("", code)
    code = FUNCTION_DECLARATION.sub(lambda found: " " * len(found.group()), code)
    return [(m.group(1), line_of(code, m.start(1))) for pattern in patterns for m in pattern.finditer(code)]


def python_dependencies(path: str, text: str, paths: Set[str]) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    folder = posixpath.dirname(path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
            roots = (folder, "")
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else [alias.name for alias in node.names]
            relative = folder
            for _ in range(max(0, node.level - 1)):
                relative = posixpath.dirname(relative)
            roots = (relative,) if node.level else (folder, "")
        else:
            continue
        for name in names:
            module = name.replace(".", "/")
            for root in roots:
                base = posixpath.normpath(posixpath.join(root, module))
                candidates = {base + ".py", posixpath.join(base, "__init__.py")} & paths
                if candidates:
                    found.update(candidates)
                    break
    return found


def script_dependencies(path: str, text: str, paths: Set[str]) -> set[str]:
    if path.endswith(".py"):
        return python_dependencies(path, text, paths)
    if not is_shell(path, text) and not path.endswith(".ps1"):
        return set()
    commands, _ = lex(text)
    found = set()
    folder = posixpath.dirname(path)
    for command in commands:
        if len(command.words) < 2 or command.words[0] not in (".", "source"):
            continue
        raw = command.words[1]
        start = raw.find("$(dirname")
        if start >= 0:
            stop = balanced(raw, start + 2, "(", ")")
            raw = raw[:start] + folder + raw[stop:]
        raw = raw.replace('"', "").replace("'", "")
        if "$" not in raw:
            candidates = {posixpath.normpath(raw), posixpath.normpath(posixpath.join(folder, raw))}
        else:
            tail = re.sub(r"\$(?:\{[^}]+\}|[A-Za-z_][A-Za-z0-9_]*)", "", raw).lstrip("/")
            candidates = set()
            parent = folder
            while True:
                candidates.add(posixpath.normpath(posixpath.join(parent, tail)))
                if not parent:
                    break
                parent = posixpath.dirname(parent)
        matches = candidates & paths
        if len(matches) == 1:
            found |= matches
    if path.endswith(".ps1"):
        for match in re.finditer(r"""(?m)^\s*\.\s*\(Join-Path\s+\$PSScriptRoot\s+["']([^"']+)["']\)""", text):
            target = posixpath.normpath(posixpath.join(folder, match.group(1).replace("\\", "/")))
            if target in paths:
                found.add(target)
    return found


def imported_registries(path: str, files: dict[str, str], dependencies: dict[str, set[str]]) -> set[str]:
    pending, seen, found = [path], set(), set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current.endswith("messages.env"):
            found.add(current)
            continue
        body = files.get(current, "")
        if MSG_LINE.search(body):
            found.add(current)
        if "messages.env" in body and re.search(r"\b(?:Get-Content|open|read_text)\b", body):
            folder = posixpath.dirname(current)
            while True:
                registry = posixpath.join(folder, "messages.env")
                if registry in files:
                    found.add(registry)
                    break
                if not folder:
                    break
                folder = posixpath.dirname(folder)
        if current not in dependencies:
            dependencies[current] = script_dependencies(current, body, files.keys())
        pending.extend(dependencies[current] - seen)
    return found


def registry_surface(files: dict[str, str]) -> list[Violation]:
    found = []
    registries: dict[str, dict[str, int]] = {}
    declared: dict[str, str] = {}
    for path, text in files.items():
        ci = ci_yaml(path)
        if path.endswith("messages.env"):
            rest = MSG_LINE.sub("", text)
            for number, line in enumerate(rest.split("\n"), 1):
                if line.strip() and not line.startswith("#"):
                    found.append(Violation(path, number, msg("textreg_line_form"), line))
        elif not (ci or is_shell(path, text) or path.endswith(".py") or os.path.basename(path) == "Dockerfile"):
            continue
        entries = {}
        for m in (WORKFLOW_MSG if ci else MSG_LINE).finditer(text):
            key, template = m.group(1), m.group(2)
            number = line_of(text, m.start())
            if key in declared:
                found.append(Violation(path, number, msg("textreg_key_twice_in", declared[key]), key))
            declared[key] = path
            entries[key] = number
            if "\\" in template or any(s.group(1) not in "s%" for s in SPEC.finditer(template)):
                found.append(Violation(path, number, msg("textreg_template_specs"), key))
        if entries:
            registries[path] = entries

    used: set[str] = set()
    dependencies: dict[str, set[str]] = {}
    kit_files = set(lock_files())
    for path, text in files.items():
        if not (is_shell(path, text) or path.endswith((".py", ".ps1", ".yml", ".yaml"))
                or os.path.basename(path) == "Dockerfile"):
            continue
        visible = set(registries.get(path, {})) | set(registries.get(SHARED_REGISTRY, {}))
        if path.startswith(LAYER_ROOTS) and path not in kit_files:
            visible |= set(registries.get(PROJECT_REGISTRY, {}))
        for registry in imported_registries(path, files, dependencies):
            visible |= set(registries.get(registry, {}))
        for prefix, registry in BORROWED_REGISTRY.items():
            if path.startswith(prefix):
                visible |= set(registries.get(registry, {}))
        folder = os.path.dirname(path)
        while True:
            visible |= set(registries.get(posixpath.join(folder, "messages.env"), {}))
            if not folder:
                break
            folder = os.path.dirname(folder)
        for key, number in uses(path, text):
            used.add(key)
            if key not in visible and key in declared:
                found.append(Violation(path, number, msg("textreg_key_foreign", declared[key]), key))
            elif key not in visible:
                found.append(Violation(path, number, msg("textreg_key_undeclared"), key))
    for path, entries in registries.items():
        for key, number in entries.items():
            if key not in used:
                found.append(Violation(path, number, msg("textreg_key_unused"), key))
    return found


SURFACES = {
    "shell": shell_surface,
    "workflow": workflow_surface,
    "js": js_surface,
    "python": python_surface,
    "powershell": powershell_surface,
    "registry": registry_surface,
    "frontend": frontend_surface,
    "templates": template_surface,
}


def check(files: dict[str, str], surfaces) -> int:
    violations = {v for surface in surfaces for v in surface(files)}
    failed = sorted(violations, key=lambda v: (v.path, v.line, v.rule))
    for v in failed:
        print(f"{v.path}:{v.line}: {v.rule}: {v.snippet.strip()[:120]}")
    if failed:
        print("\n" + msg("textreg_failed", len(failed)))
        return 1
    print(msg("gate_clean", len(files)))
    return 0


def main(extra: tuple = ()) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    return check({path: read(path) for path in tracked()}, (*SURFACES.values(), *extra))
