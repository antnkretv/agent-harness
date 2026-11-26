#!/usr/bin/env python3
"""Файлы и блоки комплекта harness совпадают с текстом, который записал apply: sha256 из .harness/state/lock.json.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-harness-files.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import HARNESS_BLOCK, LOCK, ROOT, git, installed_record, lock_files, lock_kit, msg  # noqa: E402


def written(path: str, entry: dict) -> bytes | None:
    try:
        with open(os.path.join(ROOT, path), "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    if entry.get("role") != "block":
        return data
    block = entry.get("block", "core")
    found = next((m for m in HARNESS_BLOCK.finditer(data.decode("utf-8", "replace")) if m.group(1) == block), None)
    return found.group(0).encode("utf-8") if found else None


def main() -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    files = lock_files()
    if not files:
        print(msg("harness_files_no_lock", LOCK))
        return 1
    checked = failed = 0
    pinned = lock_kit().get("sha256", "")
    record = installed_record()
    if record and pinned and record.get("kit") != pinned:
        failed += 1
        print(msg("harness_files_stale", record.get("kit", "")[:12], pinned[:12]))
    for path, digest in record.get("files", {}).items():
        files.setdefault(path, {"role": "managed", "sha256": digest, "installed": True})
    tracked = set(git("ls-files", "-z").split("\0"))
    for path in sorted(path for path, entry in files.items() if entry.get("installed") and path in tracked):
        failed += 1
        print(msg("harness_files_tracked", path))
    for path, entry in sorted(files.items()):
        if not entry.get("sha256"):
            continue
        checked += 1
        current = written(path, entry)
        if current is not None and hashlib.sha256(current).hexdigest() == entry["sha256"]:
            continue
        failed += 1
        if entry.get("role") == "block":
            print(msg("harness_files_block", path))
        else:
            print(msg("harness_files_managed", path, entry.get("extend") or msg("harness_files_extend")))
    print(msg("harness_files_failed", failed) if failed else msg("harness_files_clean", checked))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
