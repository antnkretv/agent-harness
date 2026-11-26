#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, app_prefix, get_records, msg, payload  # noqa: E402

MANIFEST = ".harness/project/manifest.json"
PROFILE_TABLE = "CONTRIBUTING.md"
PROFILE_COLUMN = r"Локальный verify-гейт"
SKIP_DIRS = {"node_modules", "dist", "bin", "obj", ".design-sync", ".vale-work"}

NPM_RUN = re.compile(r"npm run ([\w:.@-]+)")
BACKTICKED = re.compile(r"`([^`]+)`")


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def steps_of(app: dict[str, str]) -> tuple[str, dict[str, str]]:
    manifest = f"{app['path']}/package.json"
    if not os.path.exists(os.path.join(ROOT, manifest)):
        return "", {}
    entry = NPM_RUN.findall(app.get("check", ""))
    scripts = json.loads(read(manifest)).get("scripts", {})
    if len(entry) != 1:
        return "", scripts
    return entry[0], scripts


def profile_rows(path: str) -> list[str]:
    column, rows = None, []
    for line in read(PROFILE_TABLE).splitlines():
        if not line.startswith("|"):
            if column is not None:
                break
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if column is None:
            if PROFILE_COLUMN in cells:
                column = cells.index(PROFILE_COLUMN)
            continue
        if len(cells) > column:
            rows.append(cells)
    return [cells[column] for cells in rows if f"`{path}/`" in cells[0]]


def mentions(text: str, entry: str, scripts: dict[str, str]) -> list[list[str]]:
    listed = re.findall(rf"`npm run {re.escape(entry)}`[*_\s]*\(([^)]*)\)", text)
    named = [[step for step in BACKTICKED.findall(group) if step in scripts] for group in listed]
    return [group for group in named if group]


def sources(path: str) -> list[str]:
    found = []
    for current, dirs, files in os.walk(os.path.join(ROOT, path)):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".git")]
        for name in files:
            relative = os.path.relpath(os.path.join(current, name), ROOT).replace(os.sep, "/")
            if name.endswith(".md") and not payload(relative):
                found.append(relative)
    return sorted(found)


def app_hook_directories(configured_apps: list[dict[str, str]]) -> list[str]:
    found = []
    for app in configured_apps:
        prefix = app_prefix(app["path"])
        if not prefix:
            continue
        relative = prefix + ".githooks"
        directory = os.path.join(ROOT, relative)
        if os.path.isdir(directory):
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    found.append(relative)
    return found


def main() -> int:
    failures = []
    configured_apps = get_records("apps", [])

    for app in configured_apps:
        path, name = app["path"], app["id"]
        entry, scripts = steps_of(app)
        if not entry:
            print(msg("profiles_not_npm", name))
            continue
        print(f"  {name}: `npm run {entry}`")

        if entry not in scripts:
            failures.append(msg("profiles_no_script", path, entry, MANIFEST))

        rows = profile_rows(path)
        row = next((row for row in rows if f"`npm run {entry}`" in row), rows[0] if rows else "")
        if not row:
            failures.append(msg("profiles_no_row", PROFILE_TABLE, path))
        elif f"`npm run {entry}`" not in row:
            failures.append(msg("profiles_no_command", PROFILE_TABLE, path, entry))

        for where, text in [(msg("profiles_where_row", PROFILE_TABLE, path), row)] + [
            (doc, read(doc)) for doc in sources(path)
        ]:
            for _ in mentions(text, entry, scripts):
                failures.append(msg("profiles_inner_steps", where, entry, path))

    for directory in app_hook_directories(configured_apps):
        failures.append(msg("profiles_foreign_hooks", directory))

    if failures:
        print("\n" + msg("profiles_diverged"))
        for line in sorted(set(failures)):
            print(f"  {line}")
        print("\n" + msg("profiles_hint"))
        return 1

    print(msg("profiles_clean"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
