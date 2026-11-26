from __future__ import annotations

import functools
import json
import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path

from .documents import block_of, compose, entry_markers, json_indent, json_text, kit_lists, layout, markers, merge_json, overlay_json
from .migrate import converted, seeded, unwired
from .render import AGENT_SERVERS, server_clashes
from .resources import (
    PROJECT_BLOCK,
    Item,
    POSIX_MODES,
    Refusal,
    git,
    index_modes,
    msg,
    prune,
    sha256,
    tracks_modes,
)

SKILLS_DIR = ".agents/skills"
SKILL_PATH = re.compile(r"\.agents/skills/([^/]+)/")


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


class Installer:
    def __init__(self, root: Path, dry_run: bool):
        self.root = root
        self.dry_run = dry_run
        self.report: list[tuple[str, str]] = []
        self.notes: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self.unchanged = 0
        self.moded: set[str] = set()
        self.placed: set[str] = set()

    def shell(self, every: list[Item], lock: dict, released: frozenset[str]) -> dict:
        return self.run([item for item in every if item.place == "shell"], lock, released,
                        frozenset(item.path for item in every if item.place == "lock"))

    def run(self, planned: list[Item], lock: dict, released: frozenset[str], kept: frozenset[str] = frozenset()) -> dict:
        files = {}
        handlers = {"managed": self.managed, "json": self.managed, "seed": self.seed, "block": self.block}
        for item in planned:
            files[item.path] = handlers[item.role](item, lock.get("files", {}).get(item.path, {}))
            if item.role in ("managed", "json"):
                self.set_mode(item.path, item.executable)
        for path, entry in lock.get("files", {}).items():
            if path in files or path in kept or entry.get("role") == "seed":
                continue
            if path in released:
                self.report.append((path, msg("state_released")))
            else:
                self.retire(path, entry)
        return files

    def refuse_taken_skills(self, planned: list[Item], owned: set[str]) -> None:
        shipped: dict[str, set[str]] = {}
        for item in planned:
            if skill := SKILL_PATH.match(item.path):
                shipped.setdefault(skill.group(1), set()).add(item.path)
        for name, files in sorted(shipped.items()):
            folder = self.root / SKILLS_DIR / name
            if not folder.is_dir() or any(path.startswith(f"{SKILLS_DIR}/{name}/") for path in owned):
                continue
            if any(path.relative_to(self.root).as_posix() not in files for path in folder.rglob("*") if path.is_file()):
                raise Refusal(msg("apply_skill_taken", name, f"{SKILLS_DIR}/{name}/"))

    def write(self, path: str, data: bytes, executable: bool) -> None:
        if self.dry_run:
            return
        self.put(path, data)
        self.set_mode(path, executable)

    def put(self, path: str, data: bytes) -> bool:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.read_bytes() == data:
            return False
        fresh = target.with_name(f".{target.name}.{os.getpid()}.harness-new")
        fresh.write_bytes(data)
        if target.is_file():
            os.chmod(fresh, target.stat().st_mode)
        os.replace(fresh, target)
        return True

    @functools.cached_property
    def index(self) -> dict[str, str]:
        return index_modes(self.root)

    @functools.cached_property
    def tracks_modes(self) -> bool:
        return tracks_modes(self.root)

    def set_mode(self, path: str, executable: bool) -> None:
        if self.dry_run or path in self.moded:
            return
        self.moded.add(path)
        if POSIX_MODES:
            target = self.root / path
            mode = target.stat().st_mode
            wanted = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH if executable else mode & ~0o111
            if wanted != mode:
                target.chmod(wanted)
        if self.tracks_modes:
            return
        mode = self.index.get(path)
        if (mode == "100755") == executable:
            return
        if mode is None and subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=self.root).returncode == 0:
            if executable:
                self.warnings.append(msg("apply_mode_ignored", path))
            return
        git(self.root, "update-index", "--add", "--chmod=" + ("+x" if executable else "-x"), "--", path, check=False)

    def retire(self, path: str, entry: dict) -> None:
        target = self.root / path
        if not target.is_file():
            return
        rest = None
        if entry.get("role") == "block":
            text = target.read_bytes().decode("utf-8", "replace")
            span = block_of(text, *entry_markers(path, entry))
            if span is None:
                return
            start = text.rfind("\n", 0, span[0]) + 1
            start = span[0] if text[start:span[0]].strip() else start
            stop = span[1] + 1 if text[span[1]:span[1] + 1] == "\n" else span[1]
            rest = text[:start] + text[stop:]
            found = layout(path, text, entry.get("block", "core"))
            if entry.get("created") and not found.mine and not "".join(found.outside).strip():
                rest = None
        if not self.dry_run:
            if rest is None:
                self.remove(target)
            else:
                target.write_bytes(rest.encode("utf-8"))
        self.report.append((path, msg("state_retired")))

    def remove(self, target: Path) -> None:
        target.unlink()
        prune(self.root, target)

    def managed(self, item: Item, previous: dict) -> dict:
        target = self.root / item.path
        new = item.content
        entry = {"role": item.role, "module": item.module}
        if item.role == "managed":
            entry["sha256"] = sha256(new)
        if item.extend:
            entry["extend"] = item.extend
        if item.role == "json":
            entry["lists"] = kit_lists(json.loads(new))
        if not target.exists():
            self.write(item.path, new, item.executable)
            self.report.append((item.path, msg("state_created")))
            return entry
        current = target.read_bytes()
        if item.role == "json" and current != new:
            try:
                kit, mine = json.loads(new), json.loads(current)
            except json.JSONDecodeError as error:
                raise Refusal(msg("json_broken", item.path, error)) from None
            merged = overlay_json(kit, mine, previous.get("lists")) if previous else merge_json(mine, kit)
            new = json_text(merged, json_indent(current)).encode("utf-8")
        if current == new:
            self.unchanged += 1
            return entry
        self.write(item.path, new, item.executable)
        self.report.append((item.path, msg("state_updated")))
        return entry

    def seed(self, item: Item, previous: dict) -> dict:
        target = self.root / item.path
        template = item.content.decode("utf-8", "replace")
        content = seeded(item.path, template).encode("utf-8") if markers(item.path, PROJECT_BLOCK)[0] in template \
            else item.content
        entry = {"role": "seed", "module": item.module, "edition": sha256(content)}
        if target.exists() and previous.get("role") == "block":
            text = converted(item.path, target.read_text(encoding="utf-8"), template)
            self.write(item.path, text.encode("utf-8"), item.executable)
            self.report.append((item.path, msg("state_seed_converted")))
        elif target.exists() and not previous and unwired(item.path, target.read_text(encoding="utf-8", errors="replace")):
            text = converted(item.path, target.read_text(encoding="utf-8"), template)
            self.write(item.path, text.encode("utf-8"), item.executable)
            self.report.append((item.path, msg("state_seed_wired")))
        elif target.exists() and item.path in self.placed and not previous:
            self.write(item.path, content, item.executable)
            self.report.append((item.path, msg("state_seed_adopted")))
        elif target.exists() and previous.get("edition") not in (None, entry["edition"]):
            self.notes.append((item.path, msg("state_seed_newer")))
        elif target.exists():
            if previous:
                self.unchanged += 1
            else:
                self.notes.append((item.path, msg("state_seed_kept")))
        elif previous:
            self.notes.append((item.path, msg("state_seed_removed")))
            entry = {key: value for key, value in {**entry, "edition": previous.get("edition")}.items() if value}
        else:
            self.write(item.path, content, item.executable)
            self.report.append((item.path, msg("state_created")))
        return entry

    def block(self, item: Item, previous: dict) -> dict:
        target = self.root / item.path
        text = item.content.decode("utf-8")
        begin, end = markers(item.path, item.block)
        span = block_of(text, begin, end)
        if span is None:
            raise Refusal(msg("block_markers_missing", item.path, begin))
        new_block = text[span[0]:span[1]]
        entry = {"role": "block", "block": item.block, "module": item.module, "sha256": sha256(new_block.encode("utf-8"))}
        if item.extend:
            entry["extend"] = item.extend
        if previous.get("created"):
            entry["created"] = True
        if not target.exists():
            self.write(item.path, compose(item.path, text, text, item.block).encode("utf-8"), item.executable)
            self.report.append((item.path, msg("state_created")))
            return {**entry, "created": True}
        current = target.read_text(encoding="utf-8")
        found = block_of(current, begin, end)
        clashes = server_clashes(current, new_block) if found is None and AGENT_SERVERS.get(item.path) == "codex" else []
        if clashes:
            named = ", ".join(msg("state_server_same", name) if not other else msg("state_server_second", name, other)
                              for name, other in clashes)
            raise Refusal(msg("apply_servers_clash", item.path, named))
        joined = compose(item.path, current, text, item.block)
        if item.path.endswith(".toml"):
            try:
                tomllib.loads(joined)
            except tomllib.TOMLDecodeError as error:
                raise Refusal(msg("block_toml_clash", item.path, error)) from None
        if joined == current:
            self.unchanged += 1
            return entry
        self.write(item.path, joined.encode("utf-8"), item.executable)
        self.report.append((item.path, msg("state_block_added" if found is None else "state_updated")))
        return entry
