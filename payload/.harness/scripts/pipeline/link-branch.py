#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import annotation, forge, msg, tracker  # noqa: E402

RECOVERY = os.environ.get("LINK_RECOVERY") or os.path.join(tempfile.gettempdir(), "link-branch-recovery.json")

DENIED = (
    "forbidden",
    "unauthorized",
    "insufficient",
    "permission",
    "not accessible",
    "http 401",
    "http 403",
)


def say(key: str, *args: object) -> None:
    print(msg(key, *args))


def warn(key: str, *args: object) -> None:
    print(annotation("warning", key, *args))


def fail(key: str, *args: object) -> None:
    print(annotation("error", key, *args))
    sys.exit(1)


def denied(problems: list[str]) -> bool:
    text = " ".join(problems).lower()
    return any(mark in text for mark in DENIED)


def keep_record(record: dict) -> None:
    try:
        with open(RECOVERY, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
    except OSError as error:
        fail("link_recovery_unwritten", RECOVERY, error)


def read_record():
    try:
        with open(RECOVERY, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def drop_record() -> None:
    try:
        os.remove(RECOVERY)
    except OSError:
        pass


def issue_state(number: str):
    try:
        found = tracker.issue_branches(number)
    except tracker.TrackerError as error:
        return None, [str(error)]
    return ({"id": found.id, "linked": found.linked} if found else None), []


def link(issue_id: str, oid: str, name: str, number: str):
    try:
        made, made_id = tracker.link_branch(issue_id, oid, name)
    except tracker.TrackerError as error:
        return {"ok": False, "name": "", "id": ""}, [str(error)]
    if made == name:
        return {"ok": True, "name": made, "id": made_id}, []
    state, problems = issue_state(number)
    if state and name in state["linked"]:
        return {"ok": True, "name": name, "id": state["linked"][name]}, []
    if made:
        return {"ok": False, "name": made, "id": made_id}, [msg("link_created_other", made, name)]
    return {"ok": False, "name": "", "id": ""}, problems or [msg("link_empty_link")]


def refusal(call, *args) -> str:
    try:
        call(*args)
    except (forge.ForgeError, tracker.TrackerError) as error:
        return str(error)
    return ""


def head_sha(branch: str):
    try:
        return forge.branch_head(branch), ""
    except forge.ForgeError as error:
        return "", str(error)


def open_pulls(branch: str):
    try:
        return forge.open_changes(branch)
    except forge.ForgeError:
        return None


def drop_link(made: dict) -> None:
    name, linked_id = str(made.get("name") or ""), str(made.get("id") or "")
    unlinked = refusal(tracker.unlink_branch, linked_id) if linked_id else msg("link_no_id")
    left = [problem for problem in (unlinked, refusal(forge.delete_branch, name)) if problem]
    if left:
        warn("link_branch_left", name, '; '.join(left))


def restore() -> int:
    record = read_record()
    branch = str((record or {}).get("branch") or "")
    if not branch:
        return 0
    sha = str(record.get("sha") or "")
    probe = str(record.get("probe") or "")

    now, problem = head_sha(branch)
    if now:
        say("link_branch_in_place", branch, now)
    else:
        back = refusal(forge.create_branch, branch, sha)
        if back:
            fail("link_branch_lost", branch, back, sha, probe, sha, branch)
        say("link_branch_restored", branch, sha)

    if head_sha(probe)[0]:
        drop_link({"id": record.get("link") or "", "name": probe})
    drop_record()
    return 0


def main(argv: list[str]) -> int:
    problem = tracker.missing() or forge.missing()
    if problem:
        fail("link_unavailable", problem)

    if "--restore" in argv:
        return restore()

    branch = os.environ.get("BRANCH", "").strip()
    number = tracker.branch_issue(branch)
    if number is None:
        say("link_branch_no_issue", branch or msg("value_empty"))
        return 0
    issue = tracker.issue_ref(number)

    state, problems = issue_state(number)
    if problems:
        fail("link_issue_unread", issue, '; '.join(problems))
    if state is None:
        warn("link_issue_missing", number, tracker.repo(), branch)
        return 0
    if branch in state["linked"]:
        say("link_already_linked", branch, issue)
        return 0

    sha, problem = head_sha(branch)
    if not sha:
        warn("link_branch_missing", branch, forge.repo(), problem)
        return 0

    outcome, problems = link(state["id"], sha, branch, number)
    if outcome["ok"]:
        say("link_linked", branch, issue)
        return 0
    if outcome["name"]:
        drop_link(outcome)
        fail("link_extra_dropped", '; '.join(problems), branch, outcome['name'])
    if denied(problems):
        fail("link_no_rights", '; '.join(problems), branch)

    say("link_mutation_failed", '; '.join(problems))

    pulls = open_pulls(branch)
    if pulls is None:
        fail("link_prs_unread", branch)
    if pulls:
        warn("link_open_pr", branch, ', '.join(map(forge.change_ref, pulls)), issue)
        return 0

    sha, problem = head_sha(branch)
    if not sha:
        warn("link_branch_gone", branch, forge.repo(), problem)
        return 0

    probe = f"link-probe/{'-'.join(part for part in (forge.run_id(), forge.run_attempt()) if part) or sha[:8]}"
    trial, problems = link(state["id"], sha, probe, number)
    if not trial["ok"]:
        if trial["name"]:
            drop_link(trial)
        fail("link_probe_failed", probe, '; '.join(problems), branch)
    say("link_probe_ok", probe, sha)

    fresh, problem = head_sha(branch)
    if not fresh:
        drop_link(trial)
        warn("link_branch_gone", branch, forge.repo(), problem)
        return 0
    if fresh != sha:
        say("link_branch_moved", branch, fresh)
        sha = fresh

    keep_record({"branch": branch, "sha": sha, "probe": probe, "link": trial.get("id", "")})

    problem = refusal(forge.delete_branch, branch)
    if problem:
        fail("link_drop_failed", branch, problem, probe)

    outcome, problems = link(state["id"], sha, branch, number)
    if not outcome["ok"]:
        if outcome["name"]:
            drop_link(outcome)
        problem = refusal(forge.create_branch, branch, sha)
        if problem:
            fail("link_not_restored", branch, '; '.join(problems), problem, sha, probe, sha, branch)
        drop_link(trial)
        drop_record()
        fail("link_ref_restored", branch, '; '.join(problems), sha)

    now = head_sha(branch)[0]
    if not now:
        refusal(tracker.unlink_branch, outcome["id"])
        fail("link_branch_elsewhere", branch, forge.repo())
    drop_link(trial)
    drop_record()
    if now != sha:
        warn("link_wrong_commit", branch, now, sha)
        return 0

    say("link_done", branch, issue, sha)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
