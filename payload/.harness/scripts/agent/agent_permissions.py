from __future__ import annotations
import permission_policy as native
import process_config
import argparse
import json
import sys
from pathlib import Path
from ci_common import ROOT, get, get_records, module_paths, msg
from manifest import extensions

CLIENT_PATHS = {"claude-code": ".claude/settings.json", "codex": ".codex/rules/harness.rules",
                "cursor": ".cursor/cli.json"}

CURSOR_HOOK = {"command": "./.harness/hooks/python.cmd .harness/ah.py hook agent-deny cursor", "failClosed": True}


def compile_configs(policy: dict, clients: list[str], configs: dict[str, str]) -> dict[str, str]:
    declared = policy.get("deny") if isinstance(policy, dict) else None
    if not isinstance(declared, list) or not declared:
        raise ValueError(msg("perms_policy_missing"))
    entries = native.declared_denies(declared)
    if len(set(entries)) != len(entries):
        raise ValueError(msg("perms_policy_duplicate"))
    spec = ",".join(entries)
    outputs = {}
    for client in dict.fromkeys(clients):
        if client not in CLIENT_PATHS:
            raise ValueError(msg("perms_adapter_unsupported", client))
        path = CLIENT_PATHS[client]
        if client == "codex":
            outputs[path] = "\n".join(native.codex_deny(spec)) + "\n"
            continue
        try:
            data = json.loads(configs.get(path, "{}"))
        except ValueError as error:
            raise ValueError(path + ": " + str(error)) from error
        if not isinstance(data, dict):
            raise ValueError(msg("perms_expected_object", path))
        rights = data.setdefault("permissions", {})
        if not isinstance(rights, dict) or not isinstance(rights.get("deny", []), list) \
                or not all(isinstance(entry, str) for entry in rights.get("deny", [])):
            raise ValueError(msg("perms_deny_list", path))
        expected = entries if client == "claude-code" else native.cursor_deny(spec)
        rights["deny"] = list(dict.fromkeys([*rights.get("deny", []), *expected]))
        outputs[path] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if client == "cursor":
            hook_path = ".cursor/hooks.json"
            hooks = json.loads(configs.get(hook_path, '{"version":1,"hooks":{}}'))
            if not isinstance(hooks, dict) or not isinstance(hooks.setdefault("hooks", {}), dict):
                raise ValueError(msg("perms_hooks_object", hook_path))
            before = hooks["hooks"].setdefault("preToolUse", [])
            if not isinstance(before, list):
                raise ValueError(msg("perms_pretool_list", hook_path))
            before[:] = [hook for hook in before if not (isinstance(hook, dict)
                         and "check-agent-permissions.py --cursor-hook" in str(hook.get("command")))]
            if CURSOR_HOOK not in before:
                before.append(CURSOR_HOOK)
            outputs[hook_path] = json.dumps(hooks, ensure_ascii=False, indent=2) + "\n"
    return outputs


def enabled_clients() -> list[str]:
    return [path.split("/", 1)[1] for path in module_paths() if path.startswith("agents/")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(ROOT)
    clients = enabled_clients()
    problems = []
    try:
        records = get_records("policies.permissions.deny", [], file="methodology.json")
        actual = {path: (root / path).read_text(encoding="utf-8")
                  for client, path in CLIENT_PATHS.items() if client in clients and (root / path).is_file()}
        if "cursor" in clients and (root / ".cursor/hooks.json").is_file():
            actual[".cursor/hooks.json"] = (root / ".cursor/hooks.json").read_text(encoding="utf-8")
        expected = compile_configs({"deny": records}, clients, actual)
        for client in clients:
            path = CLIENT_PATHS[client]
            target = root / path
            if args.write:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(expected[path], encoding="utf-8")
            elif path not in actual:
                problems.append(msg("perms_adapter_missing", path))
            elif client == "codex":
                lines = {line.strip() for line in actual[path].splitlines()}
                if not set(expected[path].splitlines()).issubset(lines):
                    problems.append(msg("perms_rules_missing", path))
            else:
                current = json.loads(actual[path]).get("permissions", {}).get("deny", [])
                required = json.loads(expected[path])["permissions"]["deny"]
                if not set(required).issubset(current):
                    problems.append(msg("perms_rules_missing", path))
        if "cursor" in clients:
            hook_path = ".cursor/hooks.json"
            if args.write:
                (root / hook_path).write_text(expected[hook_path], encoding="utf-8")
            elif hook_path not in actual or CURSOR_HOOK not in json.loads(actual[hook_path]).get("hooks", {}).get("preToolUse", []):
                problems.append(msg("perms_hook_missing", hook_path))
        pipeline = process_config
        if pipeline.fallback("disallowedTools", None) is not None:
            problems.append(msg("perms_fallback_denies"))
        shared = native.declared_denies(records)
        client = next(iter(extensions("ci-agent")), "")
        paths = set(module_paths()) if client else set()
        for module in (name for name in ("review", "issue-agent", "alert-triage") if name in paths):
            entries = pipeline.passes() if module == "review" else [
                entry for entry in pipeline.agent_prompts() if entry.get("id") == module]
            for entry in entries:
                runner = pipeline.runner_for(str(entry.get("runner", "")), client)
                spec = ",".join([*shared, runner.get("disallowedTools", ""),
                    get("processes.review.disallowedTools", "", file="methodology.json") if module == "review" else ""])
                if client == "codex":
                    native.codex_deny(native.executable_denies(spec, module == "review", client))
                elif client == "cursor":
                    native.cursor_deny(native.executable_denies(spec, module == "review", client))
                elif client != "claude-code":
                    raise ValueError(msg("perms_adapter_unsupported", client))

    except (OSError, ValueError, RuntimeError) as error:
        problems.append(str(error))
    if problems:
        print(msg("perms_unenforced"), file=sys.stderr)
        for problem in problems:
            print("  x " + problem, file=sys.stderr)
        return 1
    print(msg("perms_clean", ", ".join(clients) if clients else msg("perms_none")))
    return 0
