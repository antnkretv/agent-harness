from __future__ import annotations

from contextlib import contextmanager

import hashlib
import json
import os
from pathlib import Path
import subprocess

from ci_common import ROOT, base_read, msg, placed_digest, process_definitions, processes
from manifest import MANIFEST, get_record, layer_manifest
from file_lock import Busy, locked as file_locked


class Blocked(RuntimeError):
    pass


class Pending(Blocked):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_json(path: Path, data: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


MERGE_FACTS = frozenset({"merged", "master-push", "base-merge"})


def produced_by(steps: list[dict], processes: dict, fact: str) -> tuple[dict, dict] | None:
    return next(((stage, step) for stage in steps for step in processes.get(stage["process"], {}).get("steps", [])
                 if fact in step.get("produces", [])), None)


def stage_ids(pipeline: str, fact: str) -> tuple[str, str]:
    found = stages(pipeline)
    produced = next((stage["id"] for stage in found for step in stage["definition"]["steps"]
                     if fact in step.get("produces", [])), "")
    review = next((stage["id"] for stage in found if stage["process"] == "review"), "")
    if not produced or not review:
        raise Blocked(msg("runtime_unsupported_audit_composition") + ": " + pipeline)
    return produced, review


def stages(pipeline: str) -> list[dict]:
    composition = get_record(pipeline, {}, file="pipelines.json")
    if not composition:
        raise Blocked(msg("runtime_unknown_or_empty_pipeline") + pipeline)
    declared = processes()
    found = []
    for stage in ordered(composition["steps"]):
        process = declared[stage["process"]]
        steps = process["steps"] if stage["process"] == "review" else ordered(process["steps"])
        found.append({**stage, "definition": {**process, "steps": steps}})
    return found


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def base_manifest(base: str, name: str) -> dict:
    if base and name == MANIFEST:
        name = layer_manifest(lambda path: base_read(base, f".harness/{path}") is not None)
    found = base_read(base, f".harness/{name}") if base else None
    if found is None:
        raise Blocked(msg('runtime_unsupported_audit_composition'))
    return json.loads(found)


def base_definitions(base: str) -> tuple[dict, dict]:
    return (base_manifest(base, "pipelines.json")["change"],
            process_definitions(base_manifest(base, "methodology.json")["processes"]))


def ordered(entries: list[dict]) -> list[dict]:
    by_id = {entry["id"]: entry for entry in entries}
    if len(by_id) != len(entries):
        raise Blocked(msg('runtime_duplicate_step_id'))
    result, pending = [], dict(by_id)
    while pending:
        ready = [entry for entry in pending.values()
                 if all(key in {item["id"] for item in result} for key in entry.get("requires", []))]
        if not ready:
            raise Blocked(msg('runtime_missing_dependency_or_unbounded_dependency_cycle'))
        for entry in ready:
            result.append(entry)
            del pending[entry["id"]]
    return result


@contextmanager
def locked(path: Path):
    try:
        with file_locked(path):
            yield
    except Busy:
        raise Blocked(msg('runtime_another_runner_owns_this_task_no_actions_were_replayed')) from None


class SourceTree:
    def __init__(self, path: str):
        self.path = path

    def git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True, check=False)
        if check and result.returncode:
            raise Blocked(result.stderr.strip() or "git " + " ".join(args))
        return result.stdout.strip()

    def source(self) -> str:
        files = subprocess.run(["git", "-C", self.path, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                               capture_output=True, check=True).stdout.split(b"\0")
        hashes = []
        for raw in sorted(set(files)):
            if not raw:
                continue
            path = Path(self.path, os.fsdecode(raw))
            value = os.readlink(path) if path.is_symlink() else file_hash(path) if path.is_file() else "missing"
            hashes.append((os.fsdecode(raw), value))
        hashes.append(("placed", placed_digest(self.path).hex()))
        return digest(hashes)


def journals(common: Path) -> Path:
    return common / "harness" / "pipelines"


class Journal:
    def __init__(self, source: SourceTree, context: str):
        directory = Path(source.git("rev-parse", "--git-common-dir"))
        if not directory.is_absolute():
            directory = Path(source.path, directory)
        self.directory = journals(directory) / context
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.context = context

    def load(self) -> dict:
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("context") != self.context:
                raise Blocked(msg("runtime_state_belongs_to_another_task"))
            return state
        return {"v": 1, "context": self.context, "records": {}, "transitions": {}}

    def save(self, state: dict) -> None:
        write_json(self.path, state)
