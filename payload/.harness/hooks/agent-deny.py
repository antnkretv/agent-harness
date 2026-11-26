#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / ".harness" / "lib")]
import agent_hook
from platform_messages import msg


def declared() -> list[str]:
    sys.path[:0] = [str(ROOT / ".harness" / "scripts")]
    import script_paths  # noqa: F401
    from manifest import get_records
    from permission_policy import declared_denies
    return declared_denies(get_records("policies.permissions.deny", [], file="methodology.json"))


def main() -> int:
    agent, spec = (sys.argv[1:3] + ["", ""])[:2]
    payload: object = {}
    try:
        entries = agent_hook.deny_entries(spec) if spec else declared()
        if not entries:
            raise ValueError(msg("perms_hook_no_denies"))
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_name"), str):
            raise ValueError(msg("perms_tool_name_missing"))
        name, inputs = agent_hook.tool(payload), payload.get("tool_input") or {}
        if not isinstance(inputs, dict):
            raise ValueError(msg("perms_tool_input_invalid"))
        if name in agent_hook.SHELLS and not isinstance(inputs.get("command"), str):
            raise ValueError(msg("perms_shell_missing"))
        entry = agent_hook.refused(entries, name, inputs)
        reason = msg("perms_required_deny", entry) if entry else ""
    except Exception as error:
        reason = str(error) or type(error).__name__
    try:
        if spec:
            agent_hook.note(payload, bool(reason))
    except OSError as error:
        print(msg("perms_ledger_unwritten", error), file=sys.stderr)
    output = agent_hook.answer(agent, reason)
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
