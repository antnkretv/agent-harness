from __future__ import annotations

import json
import os
import re
from pathlib import Path, PureWindowsPath
import shutil
import sys


def bash(*, path: str | None = None) -> str:
    git_exe = shutil.which("git", path=path) if os.name == "nt" else None
    if git_exe:
        directory = os.path.dirname(git_exe)
        for relative in ("bash.exe", "../bin/bash.exe", "../../bin/bash.exe"):
            candidate = os.path.normpath(os.path.join(directory, relative))
            if os.path.isfile(candidate):
                return candidate
    return shutil.which("bash", path=path) or "bash"


def executable(program: str, *, path: str | None = None) -> str:
    if program == "bash":
        return bash(path=path)
    if program == "python3":
        return sys.executable
    return shutil.which(program, path=path) or program


CURSOR_VERSION = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})(?:-(\d{2})-(\d{2})-(\d{2}))?-[a-f0-9]+")


def cursor_node(versions: Path) -> list[str]:
    found = [(CURSOR_VERSION.fullmatch(path.name), path) for path in versions.iterdir() if path.is_dir()]
    dated = [((*(int(part or 0) for part in match.groups()), path.name), path) for match, path in found if match]
    folder = max(dated, key=lambda item: item[0])[1] if dated else versions
    node, entry = folder / "node.exe", folder / "index.js"
    if not node.is_file() or not entry.is_file():
        from platform_messages import msg
        raise OSError(msg("executable_cursor_version_missing", folder))
    return [str(node), str(entry)]


def command(program: str, *, path: str | None = None) -> list[str]:
    resolved = executable(program, path=path)
    shim = Path(resolved)
    if os.name != "nt" or shim.suffix.lower() not in {".cmd", ".bat"}:
        return [resolved]
    if shim.stem.lower() == "cursor-agent" and (shim.parent / "versions").is_dir():
        return cursor_node(shim.parent / "versions")
    try:
        found = re.search(r'"%dp0%[\\/](node_modules[\\/][^"\r\n]+\.(?:[cm]?js|exe))"',
                          shim.read_text(encoding="utf-8"), re.I)
        if found is None:
            raise ValueError
        parts = PureWindowsPath(found[1]).parts
        package = shim.parent.joinpath(*parts[:3 if parts[1].startswith("@") else 2]).resolve()
        entry = shim.parent.joinpath(*parts).resolve()
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError
        bins = metadata.get("bin", {})
        if isinstance(bins, str):
            bins = {str(metadata.get("name", "")).rsplit("/", 1)[-1]: bins}
        selected = bins.get(shim.stem) if isinstance(bins, dict) else None
        if not isinstance(selected, str) or (package / selected).resolve() != entry or not entry.is_relative_to(package) or not entry.is_file():
            raise ValueError
        if entry.suffix.lower() == ".exe":
            return [str(entry)]
        node = shim.parent / "node.exe"
        node = str(node) if node.is_file() else executable("node", path=path)
        if not Path(node).is_file():
            raise ValueError
        return [node, str(entry)]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        from platform_messages import msg
        text = msg("executable_npm_shim_invalid", resolved)
        if isinstance(error, OSError):
            text += " " + str(error)
        raise OSError(text) from error
