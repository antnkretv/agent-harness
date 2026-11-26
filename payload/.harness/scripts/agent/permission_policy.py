from __future__ import annotations
import json
import re
import shlex
from ci_common import msg
from agent_hook import SHELL_ENTRY, WRITE_TOOLS, deny_entries
from agent_models import SessionError

PREFIX_SUFFIX = re.compile(r"(?::\*| ?\*)$")
GLOB = re.compile(r"[*?\[]")
SHELL_META = re.compile(r"[;&|<>`$\n\r]")


def shell_command(entry: str) -> str:
    match = SHELL_ENTRY.match(entry)
    return PREFIX_SUFFIX.sub("", match.group(1).strip()).strip() if match else ""


def unenforced(provider: str, entries: list[str]) -> None:
    if entries:
        raise SessionError(msg("agent_run_deny_unenforced", provider, ", ".join(entries)))


def declared_denies(records: list[dict]) -> list[str]:
    entries, names = [], set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"] \
                or not isinstance(record.get("bash"), str) or not record["bash"].strip() \
                or not isinstance(record.get("prefix"), bool):
            raise SessionError(msg("perms_record_invalid", repr(record)))
        if record["id"] in names:
            raise SessionError(msg("perms_record_duplicate", record["id"]))
        names.add(record["id"])
        entries.append("Bash(" + record["bash"] + ("*" if record["prefix"] else "") + ")")
    return entries


def executable_denies(spec: str, review: bool, provider: str = "codex") -> str:
    entries = deny_entries(spec)
    if review:
        enforced = (*WRITE_TOOLS, "WebFetch", "WebSearch") if provider == "codex" else WRITE_TOOLS
        entries = [entry for entry in entries if entry not in enforced]
    return ",".join(entries)


def codex_deny(spec: str) -> list[str]:
    rules, skipped = [], []
    for entry in deny_entries(spec):
        command = shell_command(entry)
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
        if not words or GLOB.search(command) or SHELL_META.search(command):
            skipped.append(entry)
            continue
        tokens = ", ".join(json.dumps(word, ensure_ascii=False) for word in words)
        rules.append(f'prefix_rule(pattern = [{tokens}], decision = "forbidden")')
    unenforced("codex", skipped)
    return rules


CURSOR_READ = ("Read", "Grep", "Glob")
CURSOR_SESSION = ("TodoWrite", "Task", "Skill")


def cursor_shell(entry: str) -> str:
    command = shell_command(entry)
    if not command or ":" in command or SHELL_META.search(command):
        return ""
    binary, _, arguments = command.partition(" ")
    tail = "*" if PREFIX_SUFFIX.search(entry[5:-1]) else ""
    return "Shell(" + binary + (":" + arguments + tail if arguments else "") + ")"


def cursor_allow(spec: str) -> list[str]:
    allow, skipped = [], []
    for entry in deny_entries(spec):
        shell = cursor_shell(entry) if not GLOB.search(shell_command(entry)) else ""
        if shell:
            allow.append(shell)
        elif entry in CURSOR_READ:
            allow.append("Read(**)")
        elif entry in WRITE_TOOLS:
            allow.append("Write(**)")
        elif tool := re.fullmatch(r"mcp__([A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*)(?:__([\w-]+))?", entry):
            allow.append("Mcp(" + tool.group(1) + ":" + (tool.group(2) or "*") + ")")
        elif entry not in CURSOR_SESSION:
            skipped.append(entry)
    if skipped:
        raise SessionError(msg("agent_run_allow_unenforced", "cursor", ", ".join(skipped)))
    return list(dict.fromkeys(allow))


def cursor_deny(spec: str) -> list[str]:
    deny, skipped = [], []
    for entry in deny_entries(spec):
        shell = cursor_shell(entry)
        if shell:
            deny.append(shell)
        elif entry in WRITE_TOOLS:
            deny.append("Write(**)")
        elif entry == "WebFetch":
            deny.append("WebFetch(*)")
        elif entry == "WebSearch":
            continue
        else:
            skipped.append(entry)
    unenforced("cursor", skipped)
    return list(dict.fromkeys(deny))
