from __future__ import annotations

import argparse
import json
import os
import tarfile
import time
from pathlib import Path

from .catalog import load_catalog
from .documents import json_text
from .render import plan
from .resources import POSIX_MODES, PROJECT, Refusal, read_lock, git, kit_root, load_json, loader, msg, repository, say, sha256
from .state import Installer
from .verify import installed_kit

RECORD = "harness/installed.json"
EXCLUDE = "info/exclude"
SECTION = "# harness:begin ", "# harness:end "
LOCK_TRIES, LOCK_PAUSE, LOCK_STALE = 300, 0.05, 10


def archive_build(archive: Path) -> tuple[str, dict, str]:
    with tarfile.open(archive) as tar:
        manifests = [member for member in tar.getmembers() if member.name.count("/") == 1 and member.name.endswith("/kit.json")]
        if len(manifests) != 1:
            raise Refusal(msg("install_archive_shape", archive))
        kit = json.loads(tar.extractfile(manifests[0]).read().decode("utf-8"))
        files = [(member.name.split("/", 1)[1], tar.extractfile(member).read())
                 for member in tar.getmembers() if member.isfile() and "/" in member.name and "__pycache__" not in member.name.split("/")]
    return sha256(archive.read_bytes()), kit, loader().tree_digest(files)


def git_file(root: Path, name: str) -> Path:
    found = Path(git(root, "rev-parse", "--git-path", name))
    return found if found.is_absolute() else root / found


def record_path(root: Path) -> Path:
    return git_file(root, RECORD)


def exclude_sections(text: str) -> tuple[list[str], dict[str, list[str]]]:
    own, sections, current = [], {}, None
    for line in text.splitlines():
        if current is None and line.startswith(SECTION[0]):
            current = line[len(SECTION[0]):]
            sections[current] = []
        elif current is not None and line == SECTION[1] + current:
            current = None
        elif current is not None:
            sections[current].append(line)
        else:
            own.append(line)
    return own, sections


def worktrees(root: Path) -> set[str]:
    found = set()
    for entry in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        lines = entry.splitlines()
        if lines and lines[0].startswith("worktree ") and not any(line.split(" ")[0] in ("bare", "prunable") for line in lines):
            found.add(os.path.normcase(os.path.realpath(lines[0][len("worktree "):])))
    return found


def listed(name: str, common: str, live: set[str]) -> bool:
    if os.path.normcase(os.path.realpath(name)) == os.path.normcase(os.path.realpath(common)):
        return True
    try:
        pointer = Path(name, "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return os.path.normcase(os.path.realpath(os.path.dirname(pointer))) in live


def claimed(lock: Path) -> int:
    for _ in range(LOCK_TRIES):
        try:
            return os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            try:
                seen = lock.stat()
                if time.time() - seen.st_mtime > LOCK_STALE and lock.stat().st_ino == seen.st_ino:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(LOCK_PAUSE)
    raise Refusal(msg("install_exclude_busy", lock))


def exclude(root: Path, paths: list[str]) -> None:
    target = git_file(root, EXCLUDE)
    key = git(root, "rev-parse", "--absolute-git-dir")
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    live = worktrees(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    handle = claimed(lock)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            text = target.read_text(encoding="utf-8") if target.is_file() else ""
            own, sections = exclude_sections(text)
            sections = {name: lines for name, lines in sections.items()
                        if name != key and listed(name, common, live)}
            sections[key] = ["/" + path for path in sorted(paths)]
            lines = own + [line for name, found in sections.items() for line in (SECTION[0] + name, *found, SECTION[1] + name)]
            fresh = "\n".join(lines) + "\n"
            out.write(fresh)
    except BaseException:
        lock.unlink(missing_ok=True)
        raise
    if fresh == text:
        lock.unlink(missing_ok=True)
    else:
        os.replace(lock, target)


def recorded(record: Path) -> list[str]:
    return list(json.loads(record.read_text(encoding="utf-8")).get("files", {})) if record.is_file() else []


def place_files(root: Path, kit: dict, every: list) -> Installer:
    kept = {item.path for item in every} | set(kit.get("released", []))
    record = record_path(root)
    previous = recorded(record)
    installer, files = Installer(root, False), {}
    exclude(root, [item.path for item in every if item.place == "lock"])
    for item in every:
        if item.place != "lock":
            continue
        files[item.path] = sha256(item.content)
        if installer.put(item.path, item.content):
            installer.report.append((item.path, msg("state_updated")))
        else:
            installer.unchanged += 1
        if POSIX_MODES:
            mode = (root / item.path).stat().st_mode
            wanted = mode | 0o111 if item.executable else mode & ~0o111
            if wanted != mode:
                (root / item.path).chmod(wanted)
    for path in sorted(previous):
        if path not in kept and (root / path).is_file():
            installer.remove(root / path)
            installer.report.append((path, msg("state_retired")))
    text = json_text({"kit": read_lock(root).get("kit", {}).get("tree", ""), "files": dict(sorted(files.items()))})
    if not record.is_file() or record.read_text(encoding="utf-8") != text:
        record.parent.mkdir(parents=True, exist_ok=True)
        fresh = record.with_name(f".{record.name}.{os.getpid()}.harness-new")
        fresh.write_text(text, encoding="utf-8", newline="\n")
        os.replace(fresh, record)
    return installer


def install(args: argparse.Namespace) -> int:
    root = repository(args.target)
    lock, kit = installed_kit(root)
    project = load_json(root / PROJECT, msg("no_project", root / PROJECT))
    installer = place_files(root, kit, plan(kit_root(), kit, load_catalog(), project, root, lock))
    for path, state in installer.report:
        print(f"  {state:<14} {path}")
    if installer.report:
        updated = sum(1 for _, state in installer.report if state == msg("state_updated"))
        say("install_summary", lock["kit"].get("version"), installer.unchanged + updated, len(installer.report))
    return 0
