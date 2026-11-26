from __future__ import annotations

from session_results import session_result

import agent_results
import change_cost
import review_evidence
import review_policy as planner
import session_cost
import text_templates
import text_data
from text_data import inert
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from ci_common import annotation, forge, get, get_records, msg

PIPELINE = "methodology.json"

STATE = {state: (result, get(f"processes.review.verdicts.{state}", state, file=PIPELINE))
         for state, result in (("approve", "success"), ("changes", "failure"))}

MAX_BODY = 60000

CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

TRIES = 3


@dataclass(frozen=True)
class Target:
    change: int
    head: str
    url: str = ""
    base: str = ""
    policy: str = ""
    task: str = ""


def say(message: str, stream=None) -> None:
    print(f"review-verdict: {message}", file=stream or sys.stdout, flush=True)


def entry(name: str) -> dict:
    found = next((item for item in get_records("processes.review.steps", [], file=PIPELINE)
                  if item.get("id") == name), None)
    if found is None:
        raise RuntimeError(msg("pipeline_no_pass", name))
    return found


def context(name: str) -> str:
    prefix = get("processes.review.statusPrefix", "", file=PIPELINE)
    if not prefix:
        raise RuntimeError(msg("verdict_no_prefix"))
    return f"{prefix}/{name}"


def transient(error: forge.ForgeError) -> bool:
    code = error.status
    if code is None:
        return True
    return code >= 500 or code in (409, 429) or (code == 403 and "rate limit" in str(error).lower())


def refused(error: forge.ForgeError) -> bool:
    return error.status is not None and 400 <= error.status < 500


def attempt(what: str, call, applied=None) -> tuple[bool, object]:
    for number in range(1, TRIES + 1):
        try:
            return True, call()
        except forge.ForgeUnavailable as error:
            say(msg("verdict_refused", what, error), sys.stderr)
            return False, None
        except forge.ForgeError as error:
            failure = error
        if applied is not None:
            known, mark = applied(failure)
            if mark:
                say(msg("verdict_applied_silent", what, failure))
                return True, None
            if not known:
                say(msg("verdict_unknown_silent", what, failure), sys.stderr)
                return False, None
        if number >= TRIES or not transient(failure):
            say(msg("verdict_refused", what, failure), sys.stderr)
            return False, None
        say(msg("verdict_retry", what, number, TRIES, failure, number * 5), sys.stderr)
        time.sleep(number * 5)
    return False, None


def trusted_publisher(target: Target) -> bool:
    return bool(forge.in_ci() and os.environ.get("REVIEW_TRUSTED_PUBLISHER") == "true"
                and target.url and target.url == forge.run_url()
                and forge.publisher(target.change, target.head, target.base))


def status(target: Target, name: str, state: str, description: str) -> bool:
    if not trusted_publisher(target):
        say(msg('runtime_publishing_requires_the_protected_ci_publisher_job_local_runtime_'), sys.stderr)
        return False
    written = attempt(f"{context(name)}@{target.head[:7]}",
                      lambda: forge.set_commit_state(target.head, context(name), state, description, target.url))[0]
    if not written:
        return False
    read, found = attempt(f"{context(name)}@{target.head[:7]}", lambda: forge.commit_results(target.head))
    observed = found.get(context(name)) if read else None
    return bool(observed and observed.state == state and observed.url == target.url and observed.author == forge.login())


def round_mark(target: Target, name: str) -> str:
    return f"{forge.ROUND_MARK} {target.url} {name} -->"


def snapshot_mark(target: Target) -> str:
    return "<!-- harness-snapshot: " + json.dumps({"head": target.head, "base": target.base,
                                                   "policy": target.policy, "task": target.task}, sort_keys=True) + " -->"


def snapshot_matches(target: Target, body: str) -> bool:
    return bool(target.base and target.policy and target.task and review_evidence.receipt(body) == {
        "head": target.head, "base": target.base, "policy": target.policy, "task": target.task})


def current(target: Target) -> bool:
    change = forge.change(target.change)
    return bool(change and change.head_sha == target.head and target.base
                and getattr(change, "base_sha", "") == target.base
                and target.policy == review_evidence.policy_hash(target.base)
                and target.task and target.task == review_evidence.task_hash(target.change))


def execution_receipt(log: str, target: Target, name: str) -> bool:
    try:
        with open(log + ".receipt.json", encoding="utf-8") as handle:
            found = json.load(handle)
        with open(log, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except (OSError, ValueError):
        return False
    return found == {"head": target.head, "base": target.base, "policy": target.policy,
                     "pass": name, "log": digest, "exit": 0, "run": target.url, "task": target.task}


def reply_mark(target: Target, name: str, item: forge.InlineComment) -> str:
    side = "" if item.side == forge.RIGHT else f":{item.side}"
    digest = hashlib.sha256(f"{item.path}:{item.line}{side}:{item.body}".encode()).hexdigest()[:16]
    return round_mark(target, f"{name} {digest}")


def listed(target: Target) -> tuple[bool, list[forge.Review]]:
    return attempt(forge.change_ref(target.change), lambda: [item for item in reversed(forge.reviews(target.change))
                                                            if item.author != forge.login()
                                                            or review_evidence.trusted_review(target.change, item) == review_evidence.TRUSTED])


def own(found: list[forge.Review], target: Target, name: str) -> forge.Review | None:
    mark = round_mark(target, name)
    reviewer = forge.login()
    return next((item for item in found
                 if item.author == reviewer and item.sha == target.head and mark in item.body
                 and snapshot_matches(target, item.body)), None)


def landed(target: Target, name: str) -> tuple[bool, forge.Review | None]:
    ok, found = listed(target)
    return ok, own(found, target, name) if ok else None


def decide(target: Target, name: str, decision: str) -> bool:
    result, said = STATE[decision]
    return status(target, name, result, f"{entry(name).get('marker', '')}: {said}")


def restate(target: Target, name: str, state: str, headline: str) -> bool:
    say(msg("verdict_already", forge.change_ref(target.change)))
    if planner.held(state, headline):
        return decide(target, name, "changes")
    if planner.settled(state, headline):
        return decide(target, name, "approve")
    return True


def review(target: Target, name: str, decision: str, body: str, comments: list, rejected=None) -> bool:
    what = forge.change_ref(target.change)
    unknown = False

    def seen(error: forge.ForgeError) -> tuple[bool, bool]:
        nonlocal unknown
        read, found = landed(target, name)
        unknown = not read and not refused(error)
        return not unknown, found is not None

    def submit(kind: str):
        return lambda: forge.submit_review(target.change, target.head, kind, body, comments=comments, rejected=rejected)

    if attempt(what, submit(decision), seen)[0]:
        return landed(target, name)[1] is not None
    if unknown:
        say(msg("verdict_no_fallback_retry", what), sys.stderr)
        return False
    return attempt(what, submit("comment"), seen)[0] and landed(target, name)[1] is not None


def plain(text: str, limit: int = MAX_BODY) -> str:
    text = inert(CONTROL.sub("", text))
    return text[:limit] + "\n" + msg("verdict_body_cut") if len(text) > limit else text


def inline(found: object) -> tuple[list, list[str]]:
    comments, loose = [], []
    for item in found if isinstance(found, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("body"), str) or not item["body"].strip():
            continue
        text, path = plain(item["body"].strip()), item.get("path")
        if agent_results.valid_comment(item):
            comments.append(forge.InlineComment(path=path.strip(), line=item["line"], body=text,
                                                side=item.get("side", forge.RIGHT)))
        else:
            loose.append(f"- {path}: {text}" if isinstance(path, str) and path.strip() else f"- {text}")
    return comments, loose


def refuse(target: Target, name: str, why: str) -> bool:
    print(annotation("error", "verdict_missing", name, why))
    status(target, name, "error", msg("verdict_missing_status", why))
    return False


def pending(target: Target, name: str) -> bool:
    say(msg("verdict_pending_note", name))
    return status(target, name, "pending", msg("verdict_pending_status", entry(name).get("marker", "")))


def skip(target: Target, name: str, why: str, outcome: str, changed: list[str] | None) -> bool:
    say(msg("verdict_skip_note", name, why))
    if not current(target):
        return refuse(target, name, msg('runtime_stale_or_missing_review_snapshot'))
    if outcome == "approved":
        read, found = listed(target)
        reviewer = forge.login()
        heads = [(item, planner.headline(item.body)) for item in found if item.author == reviewer]
        valid = read and any(planner.mine(planner.Mark(item.state, head, item.sha), entry(name).get("marker", ""))
                             and planner.settled(item.state, head) and not planner.local(head) for item, head in heads)
        return decide(target, name, "approve") if valid else refuse(target, name, msg('runtime_current_approval_missing'))
    if outcome == "not-applicable" and changed is not None and planner.prose_only(entry(name), changed):
        return status(target, name, "success", "not-applicable: " + why)
    return refuse(target, name, outcome + ": " + why)


def publish(target: Target, name: str, log: str) -> bool:
    if not trusted_publisher(target):
        say(msg('runtime_refusing_review_publication_outside_the_protected_ci_publisher_jo'), sys.stderr)
        return False
    marker = entry(name).get("marker", "")
    if not current(target):
        return refuse(target, name, msg('runtime_stale_or_missing_review_snapshot'))
    read, found = landed(target, name)
    if found is not None:
        return restate(target, name, found.state, planner.headline(found.body))
    if not read:
        say(msg("verdict_marks_unread", forge.change_ref(target.change)), sys.stderr)
        return False
    if not execution_receipt(log, target, name):
        return refuse(target, name, msg('runtime_missing_trusted_runner_receipt_or_changed_result'))
    try:
        result = session_result(log)
    except OSError as error:
        return refuse(target, name, msg("verdict_log_unread", error.strerror))
    if not result:
        return refuse(target, name, msg("verdict_log_no_result"))
    if result.get("is_error") is not False or result.get("subtype") != "success":
        return refuse(target, name, msg("verdict_log_error"))

    text = result.get("result")
    last = review_evidence.verdict_line(text)
    if last is None:
        return refuse(target, name, msg("verdict_log_no_line"))

    decision = last.group(1).lower()
    word = STATE[decision][1]
    mark = round_mark(target, name)
    found, loose = inline(result.get("comments"))
    comments = [replace(item, key=reply_mark(target, name, item)) for item in found]
    narrative = text[:last.start()].strip()
    if decision == "changes" and not narrative and not comments:
        return refuse(target, name, msg("verdict_log_no_line"))
    covered = result.get("coverage")
    if not isinstance(covered, dict) or not agent_results.listed(covered.get("read")):
        return refuse(target, name, msg("verdict_no_coverage"))
    head = {"ReviewType": marker, "ReviewVerdict": word, "ReviewMode": msg("review_mode_ci"), "SHA": target.head,
            "ReviewRound": change_cost.round_of(target.change, target.head)}
    values = {**head, "ReviewIcon": planner.icon(decision), "ReviewWorkflow": forge.run_url() or target.url,
              **session_cost.outcome(result)}
    frame = len(text_templates.render("review/verdict", {**values, "ReviewResult": ""}))

    def compose(refused=()) -> str:
        result = "\n".join(part for part in (narrative, "\n".join(loose), forge.inline_fallback("", refused).strip())
                           if part)
        text = text_templates.render("review/verdict", {**values, "ReviewResult": plain(result, MAX_BODY - frame)})
        data = {**head, **session_cost.marked(values), "Digest": text_data.digest(text)}
        return "\n".join([text, "", text_templates.mark("review/verdict", data), mark, snapshot_mark(target)])

    told = review(target, name, decision, compose(), comments, compose)
    posted = told and current(target) and decide(target, name, decision)
    if posted and told:
        say(msg("verdict_published", name, word, context(name), target.head[:7]))
        return True
    if not posted:
        print(annotation("error", "verdict_status_failed", name, word))
    if not told:
        print(annotation("error", "verdict_body_failed", name))
    return False
