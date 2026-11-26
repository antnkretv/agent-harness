from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import os
import tarfile
from pathlib import Path

from .catalog import load_catalog
from .documents import json_text
from .render import plan
from .resources import LOADER, POSIX_MODES, PROJECT, Refusal, build_of, read_lock, git, kit_root, load_json, msg, repository, say, sha256
from .state import Installer
from .verify import installed_kit

RECORD = "harness/installed.json"


def archive_build(archive: Path) -> tuple[str, dict, str]:
    with tarfile.open(archive) as tar:
        manifests = [member for member in tar.getmembers() if member.name.count("/") == 1 and member.name.endswith("/kit.json")]
        if len(manifests) != 1:
            raise Refusal(msg("install_archive_shape", archive))
        kit = json.loads(tar.extractfile(manifests[0]).read().decode("utf-8"))
        files = [(member.name.split("/", 1)[1], tar.extractfile(member).read())
                 for member in tar.getmembers() if member.isfile() and "/" in member.name]
    return sha256(archive.read_bytes()), kit, loader().tree_digest(files)


@functools.cache
def loader():
    spec = importlib.util.spec_from_file_location("ah_loader", kit_root() / "payload" / LOADER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_path(root: Path) -> Path:
    found = Path(git(root, "rev-parse", "--git-path", RECORD))
    return found if found.is_absolute() else root / found


def recorded(record: Path) -> list[str]:
    return list(json.loads(record.read_text(encoding="utf-8")).get("files", {})) if record.is_file() else []


def place_files(root: Path, kit: dict, every: list) -> Installer:
    kept = {item.path for item in every} | set(kit.get("released", []))
    record = record_path(root)
    previous = recorded(record)
    installer, files = Installer(root, False), {}
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
    text = json_text({"kit": read_lock(root).get("kit", {}).get("sha256", ""), "files": dict(sorted(files.items()))})
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
