#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, base_ref, bash, git, msg, run_strict  # noqa: E402

CHECK = os.path.join(ROOT, ".harness", "scripts", "check", "commit-format.sh")
EXEMPT = 3


def main() -> int:
    parser = argparse.ArgumentParser(description=msg("commits_usage"))
    parser.add_argument("--base", help=msg("commits_base_help"))
    args = parser.parse_args()
    base = base_ref(args.base)
    if base is None:
        print(msg("commits_no_base"))
        return 0
    installed = git("log", "--format=%H", "--diff-filter=A", "HEAD", "--", ".harness/state/lock.json").split()
    older = ["--not", installed[-1] + "^@"] if installed else []
    log = run_strict("git", "log", "--no-merges", "--reverse", "--format=%H%x00%B%x1e", f"{base}..HEAD", *older,
                     unread=msg("commits_unread", base))
    commits = [record.lstrip("\n").split("\0", 1) for record in log.split("\x1e") if record.strip()]
    failed = []
    shell = bash()
    with tempfile.TemporaryDirectory() as folder:
        message = os.path.join(folder, "COMMIT_EDITMSG")
        for commit, text in commits:
            with open(message, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            done = subprocess.run([shell, CHECK, message, "--stored"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            if done.returncode not in (0, EXEMPT):
                failed.append(commit)
                print(msg("commits_bad", commit[:12], text.splitlines()[0] if text.strip() else msg("value_empty")))
                print((done.stdout + done.stderr).rstrip())
    if failed:
        print(msg("commits_failed", len(failed), len(commits)))
        return 1
    print(msg("commits_clean", len(commits), base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
