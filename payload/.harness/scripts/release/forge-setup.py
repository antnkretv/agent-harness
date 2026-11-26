#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, forge, module_paths, msg, release_branches, secrets, setting  # noqa: E402
from manifest import secrets_use  # noqa: E402

METHODOLOGY = os.path.join(ROOT, ".harness", "methodology.json")
EVENT_READERS = {"review", "issue-agent"}


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def wanted_labels() -> dict[str, str]:
    data = load(METHODOLOGY)
    labels = dict.fromkeys(forge.declared_labels(data), msg("setup_label_description"))
    if "alert-triage" in module_paths():
        labels[str(setting("alert-triage", "label", "alert"))] = msg("setup_label_description")
    if "tracker/github-issues" in module_paths():
        issues = data.get("policies", {}).get("issues", {})
        labels.update({**{name: label["about"] for name, label in issues.get("types", {}).items()},
                       **issues.get("priorities", {})})
    return labels


def required_secrets() -> dict[str, str]:
    needed: dict[str, str] = {}
    for path in module_paths():
        for secret in secrets(path):
            needed.setdefault(secret, path)
    return needed


def ensure_labels() -> int:
    present = forge.labels()
    wanted = wanted_labels()
    failed = found = 0
    for label, about in sorted(wanted.items()):
        if label in present:
            found += 1
            continue
        try:
            forge.create_label(label, about)
            print(msg("setup_label_created", label))
        except forge.ForgeError as error:
            failed += 1
            print(msg("setup_label_failed", label, error), file=sys.stderr)
    print(msg("setup_labels_checked", found, len(wanted)))
    return failed


def print_secrets() -> None:
    needed = required_secrets()
    if not needed:
        return
    print(msg("setup_secrets_plan"))
    for secret, owner in sorted(needed.items()):
        print(msg("setup_secret_use", secret, owner, secrets_use(owner) or msg("value_absent")))


def check_secrets() -> int:
    needed = required_secrets()
    if not needed:
        return 0
    try:
        names = forge.secret_names()
    except forge.ForgeError as error:
        print(msg("setup_secrets_unread", error), file=sys.stderr)
        return 1
    scoped: dict[str, set[str]] = {}
    missing = 0
    for secret, owner in sorted(needed.items()):
        environment = str(setting(owner, "environment", "") or "")
        if environment and environment not in scoped:
            try:
                scoped[environment] = forge.secret_names(environment)
            except forge.ForgeError as error:
                print(msg("setup_secrets_unread", error), file=sys.stderr)
                return 1
        if secret in names or secret in scoped.get(environment, set()):
            continue
        missing += 1
        if environment:
            print(msg("setup_secret_missing_env", secret, environment, owner), file=sys.stderr)
        else:
            print(msg("setup_secret_missing", secret, owner), file=sys.stderr)
    print(msg("setup_secrets_checked", len(needed) - missing, len(needed)))
    return missing


def ensure_event_hook() -> int:
    if not EVENT_READERS & set(module_paths()):
        return 0
    try:
        status = forge.ensure_event_hook()
    except forge.ForgeError as error:
        print(msg("setup_hook_failed", error), file=sys.stderr)
        print(msg("setup_hook_manual"), file=sys.stderr)
        return 1
    if status:
        print(status)
    return 0


def main() -> int:
    print_secrets()
    if forge.missing():
        print(msg("setup_unavailable", forge.missing()), file=sys.stderr)
        return 3
    try:
        failed = ensure_labels()
    except forge.ForgeError as error:
        print(msg("setup_unavailable", error), file=sys.stderr)
        return 3 if isinstance(error, forge.ForgeUnavailable) else 1
    secrets_missing = check_secrets()
    failed += ensure_event_hook()
    release, base = release_branches()
    try:
        problems = forge.setup_problems(base, release)
    except forge.ForgeError as error:
        problems = [msg("setup_settings_unread", error)]
    for problem in problems:
        print(problem, file=sys.stderr)
    failed += len(problems)
    steps = forge.owner_steps(base, release)
    if steps:
        print()
        print(steps)
    return 1 if failed else 4 if secrets_missing else 0


if __name__ == "__main__":
    sys.exit(main())
