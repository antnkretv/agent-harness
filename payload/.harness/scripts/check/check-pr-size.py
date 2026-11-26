#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import (BASE_BRANCH, LOCK, ROOT, block_lines, change_body, kit_owned,  # noqa: E402
                       lock_entries, lock_files, msg, release_change, run_strict)
from generated_sources import authored  # noqa: E402
from manifest import get_record  # noqa: E402
from markdown_text import without_code  # noqa: E402

REGISTRY = "methodology.json"
LARGEST = 5
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def git_text(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else ""


def numstat(fork: str) -> list[tuple[str, int]]:
    parts = run_strict("git", "diff", "--numstat", "-M", "-z", fork, "HEAD", unread=msg("size_no_diff", fork)).split("\0")
    rows, index = [], 0
    while index < len(parts):
        head, index = parts[index], index + 1
        if not head:
            continue
        added, deleted, path = head.split("\t", 2)
        if not path:
            path, index = parts[index + 1], index + 2
        rows.append((path, sum(int(value) for value in (added, deleted) if value.isdigit())))
    return rows


def project_lines(fork: str, paths: list[str]) -> dict[str, int]:
    if not paths:
        return {}
    spans = {}
    for path in paths:
        try:
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                current = handle.read()
        except OSError:
            current = ""
        spans[path] = (block_lines(git_text("show", f"{fork}:{path}")), block_lines(current))
    counts = dict.fromkeys(paths, 0)
    path, left, right = "", 0, 0
    for line in git_text("diff", "-U0", "--no-color", fork, "HEAD", "--", *paths).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else path
        elif (found := HUNK.match(line)) is not None:
            left, right = int(found.group(1)), int(found.group(2))
        elif line.startswith("-") and not line.startswith("--- ") and path in spans:
            counts[path] += left not in spans[path][0]
            left += 1
        elif line.startswith("+") and path in spans:
            counts[path] += right not in spans[path][1]
            right += 1
    return counts


def reason(body: str, label: str) -> str:
    found = re.search(rf"^[ \t]*{re.escape(label)}[ \t]*(\S.*)$", without_code(body), re.M)
    return found.group(1).strip() if found else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Размер PR против policies.size методологии.")
    parser.add_argument("--base", default=BASE_BRANCH, help="база сравнения диффа")
    args = parser.parse_args()

    branch = os.environ.get("PR_BRANCH", "").strip()
    if release_change(branch, args.base):
        print(msg("size_release", branch, args.base.removeprefix("origin/")))
        return 0
    policy = get_record("policies.size", {}, file=REGISTRY)
    files, lines, label = policy.get("files"), policy.get("lines"), policy.get("reason", "")
    if not (isinstance(files, int) and files > 0 and isinstance(lines, int) and lines > 0 and label):
        print(msg("size_unset"))
        return 1
    fork = run_strict("git", "merge-base", args.base, "HEAD", unread=msg("size_no_diff", args.base)).strip()
    entries = {**lock_entries(git_text("show", f"{fork}:{LOCK}")), **lock_files()}
    blocks = {path for path, entry in entries.items() if entry.get("role") == "block"}
    rows = [row for row in numstat(fork) if authored(row[0], kit_owned(entries), policy.get("exclude", []))]
    project = project_lines(fork, [path for path, _ in rows if path in blocks])
    rows = [(path, project.get(path, count)) for path, count in rows if path not in project or project[path]]
    total = sum(count for _, count in rows)
    print(msg("size_counted", len(rows), total, files, lines))
    if len(rows) <= files and total <= lines:
        return 0
    given = reason(change_body(), label)
    if given:
        print(msg("size_reason", given))
        return 0
    print(msg("size_over", label, policy.get("doc", "")))
    print(msg("size_largest", ", ".join(f"{path} ({count})" for path, count in
                                        sorted(rows, key=lambda row: -row[1])[:LARGEST])))
    return 1


if __name__ == "__main__":
    sys.exit(main())
