from __future__ import annotations

import fnmatch
import functools
import subprocess
from dataclasses import dataclass

from ci_common import ROOT, check_context, get_record, head_checks, msg
from text_scan import TESTS

PIPELINE = "methodology.json"


@dataclass(frozen=True)
class Fix:
    accepted: bool
    why: str
    files: tuple[str, ...] = ()
    lines: int = 0
    commits: tuple[tuple[str, str], ...] = ()


def policy() -> dict:
    return get_record("policies.postApprovalFix", {}, file=PIPELINE)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False)


def commits(approved: str, head: str) -> tuple[tuple[str, str], ...]:
    listed = git("log", "--reverse", "--format=%H%x00%s", f"{approved}..{head}")
    return tuple(tuple(line.split("\0", 1)) for line in listed.stdout.splitlines() if "\0" in line) \
        if listed.returncode == 0 else ()


def named(approved: str, fixes: tuple[tuple[str, str], ...]) -> str:
    import forge

    found = []
    for sha in [approved, *(sha for sha, _ in fixes[:-1])]:
        check = head_checks(sha).get(check_context())
        if check is not None and getattr(check, "state", "") == "failure" and getattr(check, "url", ""):
            found.append(forge.job_log(check.url) or "")
    return "\n".join(found)


def built(path: str, rule: dict) -> bool:
    return bool(TESTS.search(path)) or any(fnmatch.fnmatch(path, pattern) for pattern in rule.get("build", []))


@functools.cache
def judge(approved: str, head: str) -> Fix:
    rule = policy()
    limit = rule.get("lines", 0)
    fixes = commits(approved, head)
    stat = git("diff", "--numstat", approved, head)
    if not limit or not fixes or stat.returncode:
        return Fix(False, msg("fix_unreadable", approved[:9], head[:9]))
    rows = [line.split("\t", 2) for line in stat.stdout.splitlines() if line.count("\t") >= 2]
    files = tuple(path for _, _, path in rows)
    lines = sum(int(added) + int(removed) for added, removed, _ in rows if added.isdigit() and removed.isdigit())
    if lines >= limit:
        return Fix(False, msg("fix_too_large", lines, limit), files, lines, fixes)
    text = named(approved, fixes)
    foreign = [path for path in files if path not in text and not built(path, rule)]
    if foreign:
        return Fix(False, msg("fix_unnamed_files", ", ".join(foreign)), files, lines, fixes)
    return Fix(True, msg("fix_accepted", lines, limit), files, lines, fixes)


def accepted(number: int) -> tuple[tuple[str, str], ...]:
    import forge
    import pipeline_next

    pull = forge.change(number)
    if pull is None:
        return ()
    approved = pipeline_next.approved_head(pipeline_next.verdicts(pull)[0])
    if not approved or approved == pull.head_sha:
        return ()
    fix = judge(approved, pull.head_sha)
    return fix.commits if fix.accepted else ()
