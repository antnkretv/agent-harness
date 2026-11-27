#!/usr/bin/env python3
"""Валит сборку .NET, после которой в журнале компилятора осталась хоть одна
диагностика: ошибка, предупреждение или сообщение уровня info. Правило и его
причина - .harness/docs/ci-cd.md.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-build-diagnostics.py --configuration <Debug|Release> [--project <csproj>]

Журналы SARIF пишет компилятор по ErrorLog из .harness/dotnet/Harness.Build.targets.
Сообщения info консоль dotnet build не печатает вовсе, а Visual Studio
показывает их в Error List, поэтому считать их можно только по журналу.

Журнал проекта переписывает каждая его компиляция, поэтому он отвечает
последней сборке проекта в этой конфигурации - и той, что шла из Visual Studio.
--project сужает отбор до ProjectReference-замыкания проекта, который собрала
проверка: журнал проекта вне графа остался от сборки другой ветки или от
удаленного проекта, и его находка к отправляемому дереву не относится.
Журналов обязано быть больше нуля: иначе сборка их не пишет, и гейт молча
проходил бы на пустом месте.
"""

from __future__ import annotations

import argparse
import functools
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, annotation, msg, setting, tracked  # noqa: E402
from generated_sources import generated as owned_generated  # noqa: E402
from tool_pins import solution_projects  # noqa: E402

LOG_NAME = "diagnostics.sarif"
SOURCE_FILES = frozenset(tracked())
SOLUTION = setting("backend/dotnet", "solution", "")
SOLUTION_ROOT = os.path.dirname(SOLUTION) or "."
SUPPRESSOR = re.compile(r'SuppressionDescriptor\s+\w+\s*=\s*new\(\s*"([A-Z]+\d+)",\s*"([A-Z]+\d+)"')


@functools.cache
def semantic_suppressions() -> dict[str, str]:
    found = {str(entry.get("suppressor")): str(entry.get("rule")) for entry in setting("backend/dotnet", "suppressions")}
    for path in SOURCE_FILES:
        if path.endswith(".cs") and (path.startswith(SOLUTION_ROOT + "/") or SOLUTION_ROOT == "."):
            with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as handle:
                found.update(SUPPRESSOR.findall(handle.read()))
    return found


def generated(path: str) -> bool:
    relative = (os.path.relpath(path, ROOT) if os.path.isabs(path) else path).replace(os.sep, "/")
    if relative in SOURCE_FILES:
        return owned_generated(relative)
    return not relative.startswith("../") and "/obj/" in relative


def semantic_suppression(result: dict) -> bool:
    suppressions = result.get("suppressions", [])
    if not suppressions:
        return False
    for suppression in suppressions:
        contract = re.fullmatch(
            r"DiagnosticSuppressor \{ Suppression Id: ([A-Z]+\d+), Suppression Justification: [^\r\n{}]* \}",
            suppression.get("properties", {}).get("suppressionType", ""))
        if contract is None or semantic_suppressions().get(contract[1]) != result.get("ruleId"):
            return False
    return True


def project_graph(project: str) -> set[str]:
    seen: set[str] = set()
    pending = [os.path.abspath(project)]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for reference in ET.parse(current).getroot().findall(".//{*}ProjectReference"):
            include = reference.get("Include", "").replace("\\", "/")
            pending.append(os.path.normpath(os.path.join(os.path.dirname(current), include)))
    return {os.path.dirname(path) for path in seen}


def logs(configuration: str, project: str | None) -> list[str]:
    if project is None:
        solution_root = os.path.abspath(os.path.join(ROOT, SOLUTION_ROOT))
        directories = {
            os.path.dirname(os.path.join(ROOT, path))
            for path in SOURCE_FILES
            if path.endswith(".csproj") and os.path.isfile(os.path.join(ROOT, path))
            and os.path.commonpath((solution_root, os.path.abspath(os.path.join(ROOT, path)))) == solution_root
        }
        if SOLUTION:
            directories |= {str(path.parent) for path in solution_projects(Path(ROOT, SOLUTION))}
    else:
        directories = project_graph(project)
    return [
        path
        for directory in directories
        for path in glob.glob(os.path.join(directory, "obj", configuration, "**", LOG_NAME), recursive=True)
    ]


def location(result: dict) -> tuple[str, int, int]:
    physical = (result.get("locations") or [{}])[0].get("physicalLocation", {})
    uri = physical.get("artifactLocation", {}).get("uri", "")
    region = physical.get("region", {})
    path = url2pathname(urlparse(uri).path)
    try:
        path = os.path.relpath(path, ROOT)
    except ValueError:
        pass
    return path.replace(os.sep, "/"), region.get("startLine", 0), region.get("startColumn", 0)


def rule_texts(run: dict) -> dict[str, str]:
    rules = run.get("tool", {}).get("driver", {}).get("rules", [])
    return {rule["id"]: (rule.get("fullDescription") or {}).get("text") or rule.get("helpUri", "") for rule in rules}


def diagnostics(path: str) -> list[tuple[str, int, int, str, str, str, str]]:
    with open(path, encoding="utf-8") as handle:
        log = json.load(handle)
    found = []
    for run in log.get("runs", []):
        about = rule_texts(run)
        for result in run.get("results", []):
            source = location(result)
            if result.get("suppressions") and (generated(source[0]) or semantic_suppression(result)):
                continue
            text = result.get("message", {}).get("text", "")
            rule = result.get("ruleId", "?")
            found.append((*source, result.get("level", "warning"), rule, text, about.get(rule, "")))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--configuration", required=True, help="конфигурация сборки: Debug или Release")
    parser.add_argument("--project", help="корневой проект сборки; без него - все журналы решения")
    args = parser.parse_args()

    paths = logs(args.configuration, args.project)
    if not paths:
        print(annotation("error", "diag_no_logs", LOG_NAME, args.configuration))
        return 1

    found = sorted({item for path in paths for item in diagnostics(path)})
    for file, line, column, level, rule, text, about in found:
        print(f"{file}({line},{column}): {level} {rule}: {text}")
        if about:
            print(f"    {about}")

    if found:
        print(annotation("error", "diag_found", len(found)))
        return 1

    print(msg("diag_clean", len(paths)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
