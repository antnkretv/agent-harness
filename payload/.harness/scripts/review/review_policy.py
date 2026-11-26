from __future__ import annotations
import json
import re
from dataclasses import dataclass
from ci_common import MACHINE_TEXT_PREFIXES, annotation, get, get_list, get_records, msg
from text_data import marked
import text_templates

PIPELINE = "methodology.json"

MARKUP = re.compile(r"^[#*_> \t]+")

BLOCK = "changes"
OK = "approved"

CHANGES = re.compile(rf"\b{re.escape(get('processes.review.verdicts.changes', 'changes', file=PIPELINE))}\b[.\s]*$")
APPROVE = re.compile(rf"\b{re.escape(get('processes.review.verdicts.approve', 'approve', file=PIPELINE))}\b[.\s]*$")

HEAD_LINE = re.compile(r"^SHA: ([0-9a-f]{40})\s*$", re.M)
TASK_LINE = re.compile(r"^Task: ([0-9a-f]{64})\s*$", re.M)

BUDGET_MARK = "review-budget-record"
RECORD_VERSION = 1


@dataclass(frozen=True)
class Mark:
    state: str
    headline: str
    sha: str
    body: str = ""
    created: str = ""
    base: str = ""
    policy: str = ""
    task: str = ""


@dataclass(frozen=True)
class Route:
    name: str
    run: bool
    why: str
    stalled: str
    outcome: str = "pending"
    since: str = ""


def passes() -> list[dict]:
    return get_records("processes.review.steps", [], file=PIPELINE)


def round_paths() -> set[str]:
    found = {get("processes.review.doc", "", file=PIPELINE)}
    for entry in passes():
        found.add(entry.get("doc", ""))
        found.add(entry.get("prompt", ""))
    for key in ("doc", "prompt", "runner"):
        found.add(get(f"processes.review.fallback.{key}", "", file=PIPELINE))
    return {path.partition("#")[0] for path in found if path}


def not_prose() -> set[str]:
    shared = set(MACHINE_TEXT_PREFIXES)
    return ((shared - set(get_list("processes.review.machineTextExcept", [], file=PIPELINE))) | round_paths()
            | set(get_list("processes.review.proseExcept", [], file=PIPELINE)))


def prose_only(entry: dict, changed: list[str]) -> bool:
    rule = entry.get("prose") or {}
    suffixes = tuple(rule.get("suffixes", []))
    if not suffixes or not changed:
        return False
    skipped = tuple(set(rule.get("except", [])) | not_prose())
    named = set(get_list("processes.review.proseAlso", [], file=PIPELINE))
    return all(path in named or (path.endswith(suffixes) and not path.startswith(skipped))
               for path in changed)


def icon(state: str) -> str:
    return msg("verdict_icon_approve") if state == "approve" else msg("verdict_icon_changes")


def headline(body: str) -> str:
    data = marked(body or "", "review/verdict")
    if not data:
        return (body or "").split("\n", 1)[0].rstrip("\r")
    mark = get("processes.review.fallback.mark", "", file=PIPELINE)
    mode = f"{mark}: " if mark and data.get("ReviewMode", "").startswith(mark) else ""
    return f"{data.get('ReviewType', '')}. {mode}{data.get('ReviewVerdict', '')}."


def head_of(body: str) -> str:
    data = marked(body or "", "review/verdict")
    found = HEAD_LINE.search(body or "")
    return data.get("SHA", "") if data else found.group(1) if found else ""


def task_of(body: str) -> str:
    data = marked(body or "", "review/verdict")
    found = TASK_LINE.search(body or "")
    return data.get("Task", "") if data else found.group(1) if found else ""


def directed(body: str) -> bool:
    return (marked(body or "", "review/verdict") or {}).get("LocalReasonKey") == "direct"


def held(state: str, headline: str) -> bool:
    return state == BLOCK or bool(CHANGES.search(headline))


def local(headline: str) -> bool:
    mark = get("processes.review.fallback.mark", "", file=PIPELINE)
    return bool(mark) and mark in headline


def settled(state: str, headline: str) -> bool:
    return state == OK or bool(APPROVE.search(headline))


def verdict(state: str, headline: str) -> str:
    return "approve" if settled(state, headline) else "changes" if held(state, headline) else ""


def mine(mark: Mark, marker: str) -> bool:
    return MARKUP.sub("", mark.headline).startswith(marker)


def counts(marks: list[Mark], marker: str, head: str, base: str = "", policy: str = "", task: str = "") -> tuple[int, str, int, str]:
    spent, last, streak, since = 0, "", 0, ""
    for mark in marks:
        if not mine(mark, marker):
            continue
        current = bool(head and mark.sha == head and (not base or mark.base == base)
                       and (not policy or mark.policy == policy) and (not task or mark.task == task))
        if current:
            spent += 1
        if held(mark.state, mark.headline):
            last = BLOCK
            streak += 1
            if streak == 1:
                since = mark.sha
        elif settled(mark.state, mark.headline) and not local(mark.headline) and mark.sha:
            streak, since = 0, ""
            last = OK
        elif current:
            last = mark.state
    return spent, last, streak, since


def route(marks: list[Mark], changed: list[str], head: str, base: str = "", policy: str = "", task: str = "") -> list[Route]:
    stall = get("processes.review.stallRounds", 0, file=PIPELINE)
    found = []
    for entry in passes():
        name = entry.get("id", "")
        spent, last, streak, since = counts(marks, entry.get("marker", ""), head, base, policy, task)
        budget = entry.get("budget", 0)

        within = spent < budget
        run, why = within, msg("plan_budget_spent")
        outcome = "pending" if run else "budget-exhausted"
        if last == OK:
            run, why, outcome = False, msg("plan_already_approved"), "approved"
            print(annotation("notice", "plan_approved_note", name))
        if run and last != BLOCK and prose_only(entry, changed):
            run, why, outcome = False, msg("plan_prose_only"), "not-applicable"
            print(annotation("notice", "plan_prose_note", name))

        print(msg("plan_counts", name, spent, budget, last or msg("value_no"), streak))
        if run or (not within and last != OK):
            limit = spent + (1 if run else 0) >= budget
            print(f"{BUDGET_MARK} {json.dumps({'v': RECORD_VERSION, 'pass': name, 'at_limit': limit})}")

        stalled = ""
        if stall and streak >= stall and since:
            values = {"ReviewTitle": entry.get("title") or name, "SHA": since, "StallRounds": streak,
                      "StallLimit": stall}
            stalled = (text_templates.render("comment/review-stalled", values) + "\n\n"
                       + text_templates.mark("comment/review-stalled", {"ReviewTitle": values["ReviewTitle"],
                                                                        "SHA": values["SHA"]}))
            print(annotation("warning", "plan_stalled_note", name, streak, stall))
        found.append(Route(name, run, "" if run else why, stalled, outcome, since))
    return found
