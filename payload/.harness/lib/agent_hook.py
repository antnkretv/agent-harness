from __future__ import annotations

import fnmatch
from functools import lru_cache
import json
import os
from pathlib import Path
import re

DENIALS_ENV = "AGENT_DENIALS"

WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EDITS = {"edit", "write", "multiedit", "notebookedit", "applypatch", "strreplace", "delete"}
SHELLS = {"shell", "bash", "runterminalcmd", "runterminalcommand"}
SHELL_ENTRY = re.compile(r"^Bash\((.+)\)$")


@lru_cache(maxsize=None)
def harness_root(root: Path) -> Path:
    import subprocess
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, encoding="utf-8", check=True).stdout.rstrip("\n")
    return Path(common).resolve() / "harness"


def sessions_dir(root: Path) -> Path:
    return harness_root(root) / "sessions"


RELEASE_SKILL = "release"


def skill_call(prompt: str) -> re.Match | None:
    from manifest import get_record
    doc = get_record("processes.develop", {}, file="methodology.json").get("doc", "")
    names = "|".join(re.escape(name) for name in (Path(doc).parent.name, RELEASE_SKILL) if name)
    return re.match(rf"\s*[/$](?P<skill>{names})(?=\s|$)", prompt) if doc else None


def session_file(root: Path, session: str) -> Path:
    import hashlib
    name = hashlib.sha256(json.dumps(session, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return sessions_dir(root) / (name[:32] + ".json")


def tool(payload: dict) -> str:
    return re.sub(r"[^a-z]", "", str(payload.get("tool_name") or "").lower())


def deny_entries(spec: str) -> list[str]:
    entries, depth, current = [], 0, ""
    for char in spec:
        depth += (char == "(") - (char == ")")
        if depth <= 0 and (char == "," or char.isspace()):
            entries.append(current)
            current, depth = "", 0
        else:
            current += char
    entries.append(current)
    return [entry.strip() for entry in entries if entry.strip()]


def refused(entries: list[str], name: str, inputs: dict) -> str:
    for entry in entries:
        shell = SHELL_ENTRY.match(entry)
        if shell and name in SHELLS:
            hit = fnmatch.fnmatchcase(str(inputs.get("command")).strip(), shell.group(1).strip().replace(":*", "*"))
        else:
            hit = (entry in WRITE_TOOLS and name in EDITS
                   or entry in ("WebSearch", "WebFetch") and name.startswith("web")
                   or re.sub(r"[^a-z]", "", entry.lower()) == name)
        if hit:
            return entry
    return ""


def note(payload: object, denied: bool) -> None:
    ledger = os.environ.get(DENIALS_ENV)
    if not ledger:
        return
    with open(ledger, "a", encoding="utf-8") as handle:
        if denied:
            row = {key: payload.get(key) for key in ("tool_name", "tool_use_id", "tool_input")} \
                if isinstance(payload, dict) else {}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def answer(agent: str, text: str) -> dict | None:
    if agent == "cursor":
        return {"permission": "deny", "user_message": text, "agent_message": text} if text else {"permission": "allow"}
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": text}}
