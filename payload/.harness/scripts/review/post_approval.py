from __future__ import annotations

import fnmatch
import functools
import re
import subprocess
from dataclasses import dataclass

from ci_common import ROOT, check_context, get_record, msg
from text_scan import TESTS

PIPELINE = "methodology.json"
DEPTH = 100
SUMMARY = re.compile(r"(OK  |SKIP|FAIL|ERR |NONE) (.+?) \(\d+ s\)")
TOKEN = re.compile(r"[\s'\"`()\[\]{}<>,;=|]+")


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


def present(*shas: str) -> bool:
    return all(git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0 for sha in shas)


def fetched(approved: str, head: str, fetch: str) -> bool:
    if present(approved, head):
        return True
    if fetch:
        git("fetch", "--quiet", "--no-tags", f"--depth={DEPTH}", "origin", fetch)
    return present(approved, head)


def commits(approved: str, head: str) -> tuple[tuple[str, str], ...]:
    listed = git("log", "--reverse", "--first-parent", "--no-merges", "--format=%H%x00%s", f"{approved}..{head}")
    return tuple(tuple(line.split("\0", 1)) for line in listed.stdout.splitlines() if "\0" in line) \
        if listed.returncode == 0 else ()


def resolved(approved: str, head: str) -> list[str]:
    merges = git("rev-list", "--first-parent", "--merges", f"{approved}..{head}").stdout.split()
    return [sha for sha in merges if git("diff-tree", "--cc", "--no-commit-id", sha).stdout.strip()]


def changes(approved: str, head: str) -> tuple[tuple[str, ...], int] | None:
    stat = git("log", "--first-parent", "--no-merges", "--numstat", "--format=", f"{approved}..{head}")
    if stat.returncode:
        return None
    rows = [line.split("\t", 2) for line in stat.stdout.splitlines() if line.count("\t") >= 2]
    files = tuple(dict.fromkeys(path for _, _, path in rows))
    return files, sum(int(added) + int(removed) for added, removed, _ in rows if added.isdigit() and removed.isdigit())


def failed_output(log: str) -> str:
    lines = log.splitlines()
    rows = [found.groups() for line in lines if (found := SUMMARY.search(line))]
    failed = {title for mark, title in rows if mark in ("FAIL", "ERR ")}
    if not failed:
        return log
    titles, kept, keep = {title for _, title in rows}, [], False
    for line in lines:
        text = line.rstrip()
        summary = SUMMARY.search(text)
        opened = None if summary else next((title for title in titles if text.endswith(title)), None)
        if opened is not None:
            keep = opened in failed
        elif summary:
            keep = False
            if summary.group(2) in failed:
                kept.append(text)
        elif keep:
            kept.append(text)
    return "\n".join(kept)


def named(approved: str) -> str | None:
    import forge

    try:
        check = next((item for item in forge.jobs(approved) if item.name == check_context()), None)
        if check is None or check.state != "failure" or not check.url:
            return ""
        return failed_output(forge.job_log(check.url) or "")
    except forge.ForgeError:
        return None


def mentioned(path: str, text: str) -> bool:
    for token in TOKEN.split(text):
        token = re.sub(r"(:\d+)+:?$|[.:]+$", "", token.removeprefix("./"))
        if token == path or token.startswith("/") and token.endswith("/" + path):
            return True
    return False


def built(path: str, rule: dict) -> bool:
    return bool(TESTS.search(path)) or any(fnmatch.fnmatch(path, pattern) for pattern in rule.get("build", []))


@functools.cache
def judge(approved: str, head: str, fetch: str = "") -> Fix:
    rule = policy()
    limit = rule.get("lines", 0)
    unreadable = Fix(False, msg("fix_unreadable", approved[:9], head[:9]))
    if not limit or not fetched(approved, head, fetch):
        return unreadable
    if git("merge-base", "--is-ancestor", approved, head).returncode:
        return Fix(False, msg("fix_rewritten", approved[:9], head[:9]))
    fixes, found = commits(approved, head), changes(approved, head)
    if found is None:
        return unreadable
    files, lines = found
    merges = resolved(approved, head)
    if merges:
        return Fix(False, msg("fix_merge_resolved", ", ".join(sha[:9] for sha in merges)), files, lines, fixes)
    if lines >= limit:
        return Fix(False, msg("fix_too_large", lines, limit), files, lines, fixes)
    foreign = [path for path in files if not built(path, rule)]
    text = named(approved) if foreign else ""
    if text is None:
        return Fix(False, msg("fix_log_unreadable", approved[:9]), files, lines, fixes)
    foreign = [path for path in foreign if not mentioned(path, text)]
    if foreign:
        return Fix(False, msg("fix_unnamed_files", ", ".join(foreign)), files, lines, fixes)
    return Fix(True, msg("fix_accepted", lines, limit), files, lines, fixes)


def stale(approved: str, head: str, fetch: str = "") -> Fix | None:
    if not approved or approved == head:
        return None
    fix = judge(approved, head, fetch)
    return None if fix.accepted else fix


def accepted(number: int) -> tuple[tuple[str, str], ...]:
    import forge
    import pipeline_next
    import review_policy

    pull = forge.change(number)
    if pull is None:
        return ()
    approved = review_policy.covered(pipeline_next.verdicts(pull)[2])
    fixes = [judge(sha, pull.head_sha, pull.fetch_ref) for sha in set(approved.values()) - {pull.head_sha}]
    if not fixes or not all(fix.accepted for fix in fixes):
        return ()
    return max(fixes, key=lambda fix: len(fix.commits)).commits
