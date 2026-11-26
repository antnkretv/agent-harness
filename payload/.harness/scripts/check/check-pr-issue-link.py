#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import change_body, msg, tracker  # noqa: E402
from markdown_text import without_code  # noqa: E402

branch = os.environ.get("PR_BRANCH", "").strip()

number = tracker.branch_issue(branch)
if number is None:
    print(msg("pr_issue_no_issue", branch or msg("value_empty")))
    sys.exit(0)

body = change_body()

issue = tracker.issue_ref(number)

references = tracker.references(without_code(body))
if any(reference.number == number for reference in references):
    print(msg("pr_issue_linked", branch, issue))
    sys.exit(0)

found = sorted({reference.number for reference in references if reference.closing})

print(msg("pr_issue_missing", issue))
print()
print(msg("pr_issue_hint_branch", branch))
print()
print(msg("pr_issue_hint_not_number", issue))
if found:
    print(msg("pr_issue_hint_other", ", ".join(map(tracker.issue_ref, found))))
print()
print(msg("pr_issue_hint_link", tracker.link_phrase(number, True), tracker.link_phrase(number, False)))
sys.exit(1)
