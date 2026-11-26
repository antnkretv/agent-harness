#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, app_prefix, generated_path, get_records, msg, setting, tracked  # noqa: E402

CONFIGS = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts", "eslint.config.mts")
SOURCE = re.compile(r"\.(ts|tsx|mts|cts)$")
LOCAL_IMPORT = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""")
IMPORT_SUFFIXES = ("", ".js", ".mjs", ".cjs", "/index.js", "/index.mjs")
ERROR_LEVELS = (2, "error")


def config_of(folder: str) -> str | None:
    return next((name for name in CONFIGS if os.path.isfile(os.path.join(ROOT, folder, name))), None)


def severity(rule) -> object:
    return rule[0] if isinstance(rule, list) and rule else rule


def options(rule) -> list:
    return rule[1:] if isinstance(rule, list) else []


def covers(given, wanted) -> bool:
    if isinstance(wanted, list):
        return isinstance(given, list) and all(any(covers(item, need) for item in given) for need in wanted)
    if isinstance(wanted, dict):
        return isinstance(given, dict) and given.keys() == wanted.keys() \
            and all(covers(given[key], need) for key, need in wanted.items())
    return given == wanted


def holds(wanted, given) -> bool:
    if severity(given) not in ERROR_LEVELS:
        return False
    declared, used = options(wanted), options(given)
    return covers(used, declared) if declared else not used


def rule_modules(config: str) -> list[str]:
    root, seen, pending = os.path.realpath(ROOT), [], [os.path.realpath(os.path.join(ROOT, config))]
    while pending:
        current = pending.pop()
        if current in seen or not os.path.isfile(current):
            continue
        seen.append(current)
        with open(current, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        for target in LOCAL_IMPORT.findall(text):
            base = os.path.normpath(os.path.join(os.path.dirname(current), target))
            found = next((base + suffix for suffix in IMPORT_SUFFIXES if os.path.isfile(base + suffix)), None)
            if found and found.startswith(root + os.sep) and "node_modules" not in found.split(os.sep):
                pending.append(found)
    return [os.path.relpath(path, root).replace(os.sep, "/") for path in seen]


def adapter_config(folder: str, config: str, sources: list[str], modules: list[str]) -> tuple[dict | None, str]:
    app = os.path.join(ROOT, folder)
    done = subprocess.run(["node", os.path.join(ROOT, ".harness", "scripts", "check", "eslint-adapter.mjs"), ROOT, app, config],
                          input=json.dumps({"sources": sources, "modules": modules}), cwd=app, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        return None, (done.stderr or done.stdout).strip()[-400:]
    return json.loads(done.stdout), ""


def check_app(app: dict, files: list[str]) -> list[str]:
    prefix = app_prefix(str(app["path"]))
    folder = prefix.rstrip("/") or "."
    config = config_of(folder)
    if config is None:
        return [msg("node_adapter_no_config", app["id"], folder)]
    layerless = bool(str(app.get("architectureWaiver") or "").strip())
    sources = [path for path in files if path.startswith(prefix) and SOURCE.search(path)
               and "/node_modules/" not in path and not path.endswith(".d.ts") and not generated_path(path)]
    if not sources:
        return [msg("node_adapter_no_sources", app["id"])]
    modules = rule_modules(prefix + config)
    declared = setting(f"{app.get('side', 'frontend')}/node", "rules", [])
    found, error = adapter_config(folder, config, [path[len(prefix):] for path in sources], modules)
    if found is None:
        return [msg("node_adapter_print_failed", app["id"], config, error)]
    problems = [msg("node_adapter_rules_unwired", app["id"], prefix + config, module)
                for module in declared if module not in modules]
    if not found["sources"]:
        return [msg("node_adapter_no_sources", app["id"])]
    problems += [msg("node_adapter_layer_empty", app["id"], prefix + folder) for folder in found["empty"]]
    for group in found["sources"]:
        rules, source = group["rules"], prefix + group["file"]
        problems += [msg("node_adapter_rule_weakened", app["id"], source, name)
                     for name, wanted in found["required"].items() if not holds(wanted, rules.get(name))]
        problems += [msg("node_adapter_rule_weakened", app["id"], source, name)
                     for name, rule in (group["imports"] or {}).items() if not holds(rule["wanted"], rule["given"])]
    if not found["layers"] and not layerless:
        problems.append(msg("node_adapter_no_layers", app["id"], config))
    problems += [msg("node_adapter_rule_weakened", app["id"], probe, name)
                 for probe, rules in found["layers"].items()
                 for name, rule in rules.items() if not holds(rule["wanted"], rule["given"])]
    return problems


def main() -> int:
    apps = [app for app in get_records("apps", []) if app.get("stack") == "node"]
    files = tracked()
    problems = [problem for app in apps for problem in check_app(app, files)]
    for problem in problems:
        print(f"  {problem}")
    if problems:
        print(msg("node_adapter_failed", len(problems)))
        return 1
    print(msg("node_adapter_clean", len(apps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
