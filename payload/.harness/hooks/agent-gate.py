#!/usr/bin/env python3
from __future__ import annotations

from contextlib import suppress
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / ".harness" / "lib")]
from agent_hook import EDITS, SHELLS, answer, session_file, skill_call, tool as tool_name
FAULT = ".fault"
PATCH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
SETTINGS = re.compile(r"\.harness[/\\\\]+settings\.env\b")
COMMAND_START = r"(?:^|[;&|(`]|\$\()\s*(?:\S*/)?"
CLOSE_CLI = re.compile(COMMAND_START + r"(?:gh\s+pr\s+close|glab\s+mr\s+close)\b", re.M)
API_CLIENT = re.compile(COMMAND_START + r"(?:gh\s+api|glab\s+api|curl|wget|http|https)\b", re.M)
CHANGE_PATH = re.compile(r"\b(?:pulls|merge_requests)/\d+")
CLOSE_FIELD = re.compile(r"""\bstate(?:_event)?\\?["']?\s*[=:]\s*\\?["']?close""")


def emit(agent: str, event: str, text: str = "") -> None:
    if event == "tool":
        output = answer(agent, text)
    elif agent == "cursor":
        output = {"continue": True} if event == "prompt" else {"followup_message": text} if text else {}
    elif not text:
        return
    elif event == "prompt":
        output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    else:
        output = {"decision": "block", "reason": text}
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))


def targets(tool: dict) -> list[Path]:
    from session_log import PATH_KEYS
    found = [str(tool[key]) for key in PATH_KEYS if tool.get(key)]
    found += PATCH.findall(str(tool.get("command") or tool.get("patch") or tool.get("input") or ""))
    return [Path(item) if Path(item).is_absolute() else ROOT / item for item in found]


def report(error: Exception) -> str:
    text = f"{type(error).__name__}: {error}"
    print("agent-gate: " + text, file=sys.stderr)
    return text


def fault(event: str, edit: bool, state: Path, error: Exception) -> str:
    from platform_messages import msg
    reason = msg("gate_fault", report(error), state.with_suffix(".*"))
    if event == "tool":
        return reason if edit else ""
    if event != "stop":
        return reason
    marker = state.with_suffix(FAULT)
    try:
        if marker.read_text(encoding="utf-8") == reason:
            return ""
    except OSError:
        pass
    try:
        marker.write_text(reason, encoding="utf-8")
    except OSError:
        return ""
    return reason


def lead(agent: str, event: str, payload: dict, session: str, tool: dict, starting: bool) -> str:
    sys.path[:0] = [str(ROOT / ".harness" / "scripts")]
    import script_paths  # noqa: F401
    import pipeline_next as pipeline

    transcript = str(payload.get("transcript_path") or "")
    if event == "prompt":
        pipeline.on_prompt(ROOT, session, transcript, str(payload.get("prompt") or ""))
        return ""
    if starting:
        if pipeline.starts(tool.get("command")):
            pipeline.engage(ROOT, session, agent, transcript)
        return ""
    state = pipeline.live(ROOT, session)
    if not state:
        return ""
    if event == "tool":
        return pipeline.before_edit(ROOT, state, targets(tool))
    return pipeline.on_stop(ROOT, state, transcript)


def gate(agent: str, event: str, payload: dict) -> str:
    session = str(payload.get("session_id") or payload.get("conversation_id") or "")
    tool = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    edit = tool_name(payload) in EDITS
    starting = event == "tool" and not edit
    if not session or starting and "pipeline-run.py" not in json.dumps(tool):
        return ""
    state = session_file(ROOT, session)
    entering = event == "prompt" and skill_call(str(payload.get("prompt") or "")) is not None
    if not starting and not entering and not state.is_file():
        return ""
    try:
        text = lead(agent, event, payload, session, tool, starting)
    except Exception as error:
        return fault(event, edit, state, error)
    if event == "stop":
        with suppress(OSError):
            state.with_suffix(FAULT).unlink(missing_ok=True)
    return text


def secret_read(payload: dict) -> str:
    tool = payload.get("tool_input")
    if tool_name(payload) in EDITS or not SETTINGS.search(json.dumps(tool if isinstance(tool, dict) else {})):
        return ""
    from platform_messages import msg
    return msg("gate_settings_read")


def closing(payload: dict) -> bool:
    tool = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool_name(payload) in SHELLS:
        command = tool.get("command")
        text = " ".join(map(str, command)) if isinstance(command, list) else str(command or "")
        return bool(CLOSE_CLI.search(text) or API_CLIENT.search(text) and (
            "closePullRequest" in text or CLOSE_FIELD.search(text) and CHANGE_PATH.search(text)))
    if not any(str(value).lower().startswith("close") for value in tool.values()):
        return False
    try:
        import forge
        return bool(forge.tools and forge.tools.closes(str(payload.get("tool_name") or ""), tool))
    except Exception as error:
        report(error)
        return False


def close_bypass(payload: dict) -> str:
    if not closing(payload):
        return ""
    from platform_messages import msg
    return msg("gate_close_bypass", msg("close_note_command"))


def main() -> int:
    agent, event = (sys.argv[1:3] + ["", ""])[:2]
    try:
        loaded = json.loads(sys.stdin.read() or "{}")
    except ValueError as error:
        loaded = error
    payload = loaded if isinstance(loaded, dict) else {}
    if event == "tool" and (refusal := secret_read(payload) or close_bypass(payload)):
        emit(agent, event, refusal)
        return 0
    if os.environ.get("HARNESS_AGENT_RUN"):
        emit(agent, event)
        return 0
    try:
        if isinstance(loaded, Exception):
            raise loaded
        os.chdir(ROOT)
        text = gate(agent, event, payload)
    except Exception as error:
        report(error)
        text = ""
    emit(agent, event, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
