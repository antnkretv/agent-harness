from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .catalog import enabled, load_catalog, validate
from .documents import json_text
from .render import absent_paths, plan, remediation
from .resources import (
    GLOSSARY_RULE_TOOL,
    INSTALLER,
    LOADER,
    LOCK,
    relocated,
    PROJECT,
    Refusal,
    SETTINGS,
    SETTINGS_DEFAULTS,
    git,
    index_modes,
    kit_tree,
    prefixes,
    kit_root,
    load_json,
    msg,
    read_lock,
    repository,
    say,
)
from .lock_install import archive_build, place_files, record_path, recorded
from .state import Installer
from .verify import run_gate

KIT_FIELDS = ("name", "version", "repository", "commit", "tree")


def write_lock(root: Path, kit: dict, catalog: dict, paths: frozenset[str], files: dict, tree: str) -> None:
    lock = {
        "_": [msg("lock_note_1"), msg("lock_note_2")],
        "kit": {key: value for key, value in {**kit, "tree": tree}.items() if key in KIT_FIELDS and value},
        "manifest": git(root, "hash-object", "--", PROJECT),
        "modules": sorted(paths),
        "absent": sorted(absent_paths(kit, paths, catalog)),
        "files": dict(sorted(files.items())),
    }
    (root / LOCK).parent.mkdir(parents=True, exist_ok=True)
    (root / LOCK).write_text(json_text(lock), encoding="utf-8", newline="\n")


def case_collisions(root: Path, paths: list[str]) -> list[str]:
    planned = {prefix.casefold(): prefix for path in paths for prefix in prefixes(path)}
    found = set()
    for tracked in index_modes(root):
        for prefix in prefixes(tracked):
            mine = planned.get(prefix.casefold())
            if mine is not None and mine != prefix:
                found.add(msg("apply_case_pair", prefix, mine))
                break
    return sorted(found)


def apply(args: argparse.Namespace) -> int:
    root = repository(args.target)
    with relocated(root, args.dry_run):
        return _apply(args, root)


def _apply(args: argparse.Namespace, root: Path) -> int:
    kit_dir = kit_root()
    kit = load_json(kit_dir / "kit.json", msg("no_kit", kit_dir))
    catalog = load_catalog()
    project = load_json(root / PROJECT, msg("no_project", root / PROJECT))
    problems = validate(project, root, catalog)
    if problems:
        for line in problems:
            print("  - " + line)
        raise Refusal(msg("params_invalid", PROJECT))
    beyond = sorted(enabled(project) - set(kit.get("packed", {}).get("paths", enabled(project))))
    if beyond:
        raise Refusal(msg("packed_beyond", ", ".join(beyond)))
    lock = read_lock(root)
    installed = lock.get("kit", {})
    tree = kit_tree()
    if args.archive:
        _, shipped, packed = archive_build(Path(args.archive).resolve())
        if packed != tree or shipped.get("version") != kit.get("version"):
            raise Refusal(msg("apply_archive_other", args.archive, kit_dir))
        tree = packed
    elif not tree:
        say("apply_unpinned", LOCK)
    if installed.get("version") == kit.get("version") and installed.get("tree") and tree and installed["tree"] != tree:
        say("apply_same_version", kit["version"], installed["tree"][:12], tree[:12])
    planned = list(plan(kit_dir, kit, catalog, project, root, lock))
    collisions = case_collisions(root, [item.path for item in planned])
    if collisions:
        for line in collisions:
            print("  - " + line)
        raise Refusal(msg("apply_case_collision"))
    released = frozenset(kit.get("released", []))
    placed = set(recorded(record_path(root)))
    installer = Installer(root, True)
    installer.placed = placed
    installer.refuse_taken_skills(planned, set(lock.get("files", {})) | placed)
    files = installer.shell(planned, lock, released)
    if not args.dry_run:
        installer = Installer(root, False)
        installer.placed = placed
        files = installer.shell(planned, lock, released)
        outside = {item.path for item in planned if item.place != "shell"}
        untracked = sorted(path for path in index_modes(root) if path in outside)
        if untracked:
            git(root, "rm", "-q", "--cached", "--", *untracked)
        moved = place_files(root, kit, planned)
        installer.report += moved.report
        installer.unchanged += moved.unchanged
        write_lock(root, kit, catalog, enabled(project), files, tree)
        if not (root / SETTINGS).exists() and (root / SETTINGS_DEFAULTS).is_file():
            shutil.copyfile(root / SETTINGS_DEFAULTS, root / SETTINGS)
            installer.report.append((SETTINGS, msg("state_created")))
        if "prose/vale" in enabled(project):
            built = run_gate(root, [sys.executable, GLOSSARY_RULE_TOOL, "--glossary"])
            if built.returncode:
                installer.warnings.append(msg("glossary_rule_failed", GLOSSARY_RULE_TOOL, (built.stderr or built.stdout).strip()))
        if "githooks" in enabled(project):
            git(root, "config", "core.hooksPath", ".githooks", check=False)
        elif git(root, "config", "--get", "core.hooksPath", check=False) == ".githooks":
            git(root, "config", "--unset", "core.hooksPath", check=False)
        if args.archive:
            loaded = run_gate(root, [sys.executable, LOADER, "install", "--archive", str(Path(args.archive).resolve())])
            print(loaded.stdout, end="")
            if loaded.returncode:
                raise Refusal(msg("apply_loader_failed", LOADER, (loaded.stderr or loaded.stdout).strip()))
    for path, state in installer.report + installer.notes:
        print(f"  {state:<14} {path}")
    say("apply_summary", len(installer.report) + len(installer.notes) + installer.unchanged, len(installer.report))
    remedies = installer.warnings + (remediation(root, project))
    if remedies:
        say("apply_remediation")
        for line in remedies:
            print("  - " + line)
    if not args.dry_run:
        say("apply_next", INSTALLER)
    return 0
