from __future__ import annotations

from session_results import parse_session_result
from functools import cache
import hashlib
import json
import os
import re
import subprocess
import sys

from ci_common import REVIEW_SCHEMA as SCHEMA, ROOT, SCRIPT_TREES, base_paths, base_read, forge, get, msg, named_scripts, tracker
from manifest import layer_manifest
from authorship import signed
import change_cost
from markdown_text import without_code
from review_policy import Mark, OK, PIPELINE, counts, headline, mine, passes, prose_only, round_paths

KIT = ".harness/scripts/agent/agent_provider.py"
PINNED = (SCHEMA, KIT)
TRUSTED = "trusted"
APPROVED_STEP = "approved"
UNTRUSTED = ""


def human_note(note) -> bool:
    if getattr(note, "bot", False) or getattr(note, "system", False) or note.author.endswith("[bot]"):
        return False
    if not note.body.strip() or signed(note.body):
        return False
    lines = [line.strip() for line in without_code(note.body).splitlines() if not line.strip().startswith(">")]
    meaningful = [line for line in lines if line]
    if not meaningful:
        return True
    return not meaningful[-1].startswith((forge.ROUND_MARK, "<!-- harness-snapshot:", "<!-- harness-review:",
                                          "<!-- harness-text:", "<!-- harness-event:"))


def comment_context(notes: list) -> str:
    rows = [{"id": str(note.id), "author": note.author, "body": note.body,
             "path": getattr(note, "path", ""), "line": getattr(note, "line", None),
             "created": getattr(note, "created", "")}
            for note in notes if human_note(note)]
    return json.dumps(sorted(rows, key=lambda item: (item["created"], item["id"])), ensure_ascii=False, sort_keys=True)


def body_references(change) -> dict[str, bool]:
    body = without_code(change.body)
    references: dict[str, bool] = {}
    for item in tracker.references(body):
        key = str(item.number)
        references[key] = references.get(key, False) or item.closing
    return references


def task_references(change, parsed_body: dict[str, bool] | None = None) -> dict[str, bool]:
    references = body_references(change) if parsed_body is None else dict(parsed_body)
    for key in tracker.closing_issues({change.number}):
        references[str(key)] = True
    return references


def task_context(number: int) -> str:
    change = forge.change(number)
    if change is None:
        raise RuntimeError(msg("runtime_pr_context_is_unavailable"))
    text = change.title + "\n\n" + change_cost.authored(change.body)
    text += "\n\n" + comment_context(forge.task_notes(number))
    for key in sorted(task_references(change)):
        issue = tracker.issue(key)
        if issue is None:
            raise RuntimeError(msg("runtime_linked_task_context_is_unavailable") + str(key))
        text += "\n\n" + str(key) + "\n" + issue.title + "\n\n" + issue.body
        text += "\n\n" + comment_context(tracker.issue_notes(key))
    return text.strip()


def task_hash(number: int) -> str:
    return hashlib.sha256(task_context(number).encode()).hexdigest()


def readiness_fingerprint(change, task_context_digest: str, *, policy: str | None = None) -> str:
    value = [change.head_sha, change.base_sha, policy_hash(change.base_sha) if policy is None else policy, task_context_digest]
    return "pipeline-ready:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


BASE_PATH = re.compile(r"\$\{ReviewBase\}/([\w./-]+\.(?:md|json))")


@cache
def policy_hash(base: str = "") -> str:
    digest = hashlib.sha256()

    @cache
    def read(path: str) -> bytes:
        if base:
            return base_read(base, path) or b""
        full = os.path.join(ROOT, path)
        if not os.path.isfile(full):
            return b""
        with open(full, "rb") as handle:
            return handle.read()
    paths = {".harness/methodology.json", ".harness/project/manifest.json", *PINNED}
    if base:
        tree = base_paths(base)
        layer = next((root for root in (scripts.split("/", 1)[0] for scripts in SCRIPT_TREES)
                      if f"{root}/methodology.json" in tree), SCRIPT_TREES[0].split("/", 1)[0])
        manifest = layer_manifest(lambda name: f"{layer}/{name}" in tree)
        paths = {f"{layer}/methodology.json", f"{layer}/{manifest}",
                 *(found for path in PINNED for found in named_scripts(tree, os.path.basename(path)) or [path])}
        raw = read(f"{layer}/methodology.json")
        if not raw:
            raise RuntimeError(msg('runtime_trusted_base_methodology_is_unavailable'))
        review = json.loads(raw).get("processes", {}).get("review", {})
        paths.update(path.partition("#")[0] for entry in [review, *review.get("steps", []), review.get("fallback", {})]
                     for key in ("doc", "prompt", "commonPrompt") if (path := entry.get(key)))
    else:
        paths |= round_paths()
    for path in [path for path in sorted(paths) if path.endswith(".md")]:
        paths |= set(BASE_PATH.findall(read(path).decode("utf-8", "replace")))
    for path in sorted(paths):
        digest.update(path.encode() + b"\0" + read(path))
    return digest.hexdigest()


def receipt(body: str) -> dict:
    found = re.search(r"<!-- harness-snapshot: (\{.*?\}) -->", body)
    if not found:
        return {}
    try:
        value = json.loads(found.group(1))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def review_mark(number: int, item, headline: str, body: str) -> Mark | None:
    trust = trusted_review(number, item)
    if trust == UNTRUSTED:
        return None
    snapshot = receipt(item.body)
    return Mark(item.state, headline, item.sha, body, item.created, snapshot.get("base", ""),
                snapshot.get("policy", ""), snapshot.get("task", ""))


def evidence_result(files: dict[str, bytes], path: str) -> dict:
    return parse_session_result(files.get(path, b"").decode("utf-8", "replace")) or {}


def evidence_decision(number: int, url: str, snapshot: dict, name: str) -> str:
    evidence = forge.review_run(number, url, snapshot)
    entry = next((entry for entry in evidence["plan"]["passes"] if entry.get("id") == name), {})
    if not entry.get("run") or not any(job.get("name") == entry.get("job")
                                        and job.get("conclusion") == "success" for job in evidence["jobs"]):
        return ""
    path = name + ".result.jsonl"
    files = forge.review_artifact(url, "review-result-" + name)
    try:
        proof = json.loads(files.get(path + ".receipt.json", b""))
    except (ValueError, UnicodeError):
        return ""
    if proof != {**snapshot, "pass": name, "log": hashlib.sha256(files.get(path, b"")).hexdigest(),
                 "exit": 0, "run": url}:
        return ""
    return decision(evidence_result(files, path))


VERDICT = re.compile(r"^[ \t]*VERDICT:[ \t]*(approve|changes)[ \t]*$", re.I | re.M)


def verdict_line(text: object) -> re.Match | None:
    matches = list(VERDICT.finditer(text)) if isinstance(text, str) else []
    return matches[0] if len(matches) == 1 and not text[matches[0].end():].strip() else None


def decision(result: dict) -> str:
    found = verdict_line(result.get("result"))
    if result.get("is_error") is not False or result.get("subtype") != "success" or found is None:
        return ""
    return found.group(1).lower()


def lasting_decision(number: int, url: str, entry: dict, head: str) -> str:
    jobs = [job for job in forge.review_jobs(url) if job.get("name") == entry.get("job")]
    if len(jobs) != 1 or jobs[0].get("conclusion") != "success":
        return ""
    mark = f"{APPROVED_STEP} {number} {head}"
    return "approve" if any(step.get("name") == mark and step.get("conclusion") == "success"
                            for step in jobs[0].get("steps") or []) else "changes"


def review_author(item) -> bool:
    return forge.name == "gitlab" or forge.name == "github" and item.author == "github-actions[bot]"


def trusted_review(number: int, item) -> str:
    if forge.name == "gitlab":
        return TRUSTED
    if not review_author(item):
        return UNTRUSTED
    head = headline(item.body)
    entries = [entry for entry in passes() if mine(Mark(item.state, head, item.sha), entry.get("marker", ""))]
    if len(entries) != 1:
        return UNTRUSTED
    name = entries[0].get("id", "")
    snapshot = receipt(item.body)
    rounds = re.findall(re.escape(forge.ROUND_MARK) + r" (\S+) (\S+) -->", item.body)
    if (set(snapshot) != {"head", "base", "policy", "task"} or snapshot.get("head") != item.sha
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)
                   for value in snapshot.values()) or len(rounds) != 1 or rounds[0][1] != name):
        return UNTRUSTED
    try:
        try:
            found = evidence_decision(number, rounds[0][0], snapshot, name)
        except forge.ForgeExpired:
            found = lasting_decision(number, rounds[0][0], entries[0], snapshot["head"])
    except (forge.ForgeError, KeyError, TypeError, ValueError) as error:
        print(msg("evidence_unchecked", name, error), file=sys.stderr)
        return UNTRUSTED
    return TRUSTED if found and verdict_review(entries[0], head, item.state, found) else UNTRUSTED


def verdict_review(entry: dict, headline: str, state: str, decision: str) -> bool:
    word = get(f"processes.review.verdicts.{decision}", "", file=PIPELINE)
    return (headline == f"{entry.get('marker', '')}. {word}."
            and state in (("approved", "commented") if decision == "approve" else ("changes", "commented")))


def trusted_status(number: int, check, snapshot: dict, name: str, marks: list[Mark]) -> bool:
    if forge.name == "gitlab":
        return True
    if forge.name != "github":
        return False
    if check.author != "github-actions[bot]" or check.kind != "status" or check.state != "success":
        return False
    try:
        evidence = forge.review_run(number, check.url, snapshot)
        planned = [entry for entry in evidence["plan"]["passes"] if entry.get("id") == name]
        entry = next(entry for entry in passes() if entry.get("id") == name)
        if len(planned) != 1 or planned[0].get("result") != check.name:
            return False
        if planned[0].get("run"):
            return evidence_decision(number, check.url, snapshot, name) == "approve"
        outcome = planned[0].get("outcome")
        if outcome == "approved":
            return counts(marks, entry.get("marker", ""), snapshot["head"], snapshot["base"],
                          snapshot["policy"], snapshot["task"])[1] == OK
        changed = evidence["plan"].get("changed")
        return outcome == "not-applicable" and isinstance(changed, list) and prose_only(entry, changed)
    except forge.ForgeExpired:
        marker = next((entry.get("marker", "") for entry in passes() if entry.get("id") == name), "")
        return counts(marks, marker, snapshot["head"])[1] == OK
    except (forge.ForgeError, KeyError, TypeError, ValueError, StopIteration) as error:
        print(msg("evidence_unchecked", name, error), file=sys.stderr)
        return False
