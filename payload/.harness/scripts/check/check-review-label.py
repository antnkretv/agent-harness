#!/usr/bin/env python3
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
import process_config  # noqa: E402
import process_validation  # noqa: E402

import re  # noqa: E402

from ci_common import ROOT, forge, msg, workflow_files  # noqa: E402

REQUEST = forge.review_label()
FAILED = forge.review_label("failedLabel")

pipeline = process_config

JOB_IF = re.compile(r"^ {4}if:[ \t]*(.*)$", re.M)
GITHUB_COVER = re.compile(r"(?<!!)always\(\)|!cancelled\(\)")
GITLAB_COVER = re.compile(r"^[ \t]+when:[ \t]*always\b", re.M)


def condition(body: str) -> str:
    found = JOB_IF.search(body)
    if not found:
        return ""
    value = [found.group(1)]
    for tail in body[found.end():].splitlines()[1:]:
        if tail.strip() and len(tail) - len(tail.lstrip()) <= 4:
            break
        value.append(tail.strip())
    return " ".join(value).replace(" ", "")


def inspect(path: str, problems: list[str]) -> int:
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    gitlab = not rel.startswith(".github/")
    jobs = process_validation.ci_jobs(rel)
    calls = process_validation.round_jobs(jobs)
    prepares = [job for job, found in calls.items() if any(call[0] == "prepare" for call in found)]
    if not prepares:
        return 0

    guard = process_validation.calling(calls, "guard")
    for prepare in prepares:
        if not guard:
            problems.append(msg("review_label_no_guard", rel, prepare, REQUEST, FAILED))
            continue
        if prepare not in process_validation.guard_needs(jobs[guard]):
            problems.append(msg("review_label_guard_needs", rel, guard, prepare))
    if guard:
        covered = GITLAB_COVER.search(jobs[guard]) if gitlab else GITHUB_COVER.search(condition(jobs[guard]))
        if not covered:
            problems.append(msg("review_label_guard_condition", rel, guard,
                                "when: always" if gitlab else "always() / !cancelled()"))
    return len(prepares)


def main() -> int:
    problems: list[str] = []
    seen = 0
    for path in workflow_files():
        seen += inspect(path, problems)

    if not seen:
        problems.append(msg("review_label_not_covered", REQUEST))

    if not problems:
        print(msg("review_label_clean", REQUEST, FAILED, seen))
        return 0

    print(msg("review_label_failed"))
    print()
    for problem in problems:
        print(f"  {problem}")
    print()
    print(msg("review_label_hint", REQUEST, FAILED))
    return 1


if __name__ == "__main__":
    sys.exit(main())
