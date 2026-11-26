from __future__ import annotations

import argparse
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import cache, cached_property
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from agent_hook import RELEASE_SKILL, harness_root, session_file, sessions_dir, skill_call
from ci_common import (BASE_BRANCH, CUT_PREFIX, READY_STATUS, agent_context, change_env, check_context, checks_state, failing, forge,
                       head_checks, log_tail, moment, msg, opened_into, protected_status, release_branches,
                       runs_finished, threads_resolved)
from executables import executable
from manifest import get_record, get_records, module_on, module_paths
from pipeline_state import Blocked, base_manifest, digest, locked, stages, write_json
from process_config import fallback
import change_cost
import review_policy
import session_cost
import text_data
import text_templates
from task_workspace import Workspace, dirty, git

NEXT = "./.harness/hooks/python.cmd .harness/scripts/pipeline/pipeline-run.py next change --session {}"
RELEASE_NEXT = "./.harness/hooks/python.cmd .harness/scripts/pipeline/pipeline-run.py next release --session {}"
WATCH_SECONDS = 300
WATCH_DAYS = 14
WAIT_SECONDS = 100
STALL = 3
ENTRY = {"base": re.compile(r".+"), "review": re.compile(r"ci|local"), "rounds": re.compile(r"0|[1-9][0-9]*|max"),
         "merge": re.compile(r"human|agent")}
RELEASE_ENTRY = ("merge",)
WAITS = ("review-wait", "gates-wait", "release-wait")
RELEASE_START_MINUTES = 180
BACKGROUND_HOURS = 2
PIPELINE_COMMANDS = ("pipeline-run.py", "review-local.py")
NOTIFIED = re.compile(r"<tool-use-id>([^<\\]+)</tool-use-id>")


@dataclass(frozen=True)
class Step:
    id: str
    text: str
    terminal: bool = False
    head: str = ""
    counted: bool = True
    anchor: str = ""
    report: dict | None = None


def agent_kind(agent: str) -> str:
    return {"claude": "claude-code"}.get(agent, agent)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def common(root: Path) -> Path:
    return harness_root(root).parent


def harness_dir(root: Path, name: str) -> Path:
    folder = harness_root(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def state_file(root: Path, session: str) -> Path:
    path = session_file(root, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def heard_file(root: Path, session: str) -> Path:
    return state_file(root, session).with_suffix(".entry")


def watch_file(root: Path, change: int) -> Path:
    return harness_dir(root, "watch") / f"{change}.json"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def live(root: Path, session: str) -> dict:
    state = read_json(state_file(root, session), {})
    return {} if state.get("done") else state


def save(root: Path, state: dict) -> None:
    write_json(state_file(root, state["session"]), state)


def update(root: Path, session: str, change) -> dict:
    with locked(state_file(root, session).with_suffix(".lock")):
        state = live(root, session)
        if state:
            change(state)
            save(root, state)
        return state


def forget(root: Path, session: str) -> None:
    path = state_file(root, session)
    for item in path.parent.glob(path.stem + ".*"):
        item.unlink(missing_ok=True)


def pin(root: Path, state: dict, **fields) -> None:
    state.update(fields)
    update(root, state["session"], lambda fresh: fresh.update(fields))


def fresh_state(session: str, agent: str, transcript: str = "") -> dict:
    return {"session": session, "agent": agent, "since": now(), "acks": {}, "rounds": [], "clarify": {},
            "assumptions": [], **({"transcript": transcript} if transcript else {})}


def engage(root: Path, session: str, agent: str, transcript: str = "") -> dict:
    state = live(root, session)
    if not state:
        state = fresh_state(session, agent)
        save(root, state)
    remember(root, state, transcript)
    return state


def remember(root: Path, state: dict, transcript: str) -> None:
    if transcript and state.get("transcript") != transcript:
        pin(root, state, transcript=transcript)


def background(transcript: str) -> bool:
    commands, launched, notified = set(), set(), set()
    since = time.time() - BACKGROUND_HOURS * 3600
    try:
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "<task-notification>" in line:
                    notified.update(NOTIFIED.findall(line))
                call = '"tool_use"' in line and any(name in line for name in PIPELINE_COMMANDS)
                if not call and '"backgroundTaskId"' not in line and '"isAsync"' not in line:
                    continue
                try:
                    item = json.loads(line)
                    blocks = [block for block in item["message"]["content"] if isinstance(block, dict)]
                except (ValueError, KeyError, TypeError):
                    continue
                for block in blocks:
                    command = str((block.get("input") or {}).get("command", ""))
                    if block.get("type") == "tool_use" and any(name in command for name in PIPELINE_COMMANDS):
                        commands.add(block.get("id"))
                result = item.get("toolUseResult")
                if not isinstance(result, dict) or moment(str(item.get("timestamp", ""))) < since:
                    continue
                for use in filter(None, (block.get("tool_use_id") for block in blocks)):
                    if result.get("isAsync") is True:
                        launched.add(use)
                    elif result.get("backgroundTaskId") and use in commands:
                        launched.add(use)
    except OSError:
        return False
    return bool(launched - notified)


def develop_doc() -> str:
    return get_record("processes.develop", {}, file="methodology.json").get("doc", "")


def launches(words: list[str]) -> bool:
    while words and (re.fullmatch(r"\w+=.*", words[0]) or re.search(r"(^|[/\\])(python[\d.]*|python\.cmd|py)$", words[0])
                     or words[0].startswith("-") and words[0] != "-"):
        words = words[1:]
    return (len(words) >= 3 and re.search(r"(^|[/\\])pipeline-run\.py$", words[0]) is not None
            and words[1] == "next" and words[2] in ("change", "release")
            and not any(word.split("=", 1)[0] == "--report" for word in words))


def starts(command) -> bool:
    if isinstance(command, list):
        return launches([str(word) for word in command]) or any(starts(word) for word in command[1:]
                                                               if isinstance(word, str) and " " in word)
    lexer = shlex.shlex(str(command or ""), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        words = list(lexer)
    except ValueError:
        return False
    segment: list[str] = []
    for word in [*words, ";"]:
        if set(word) <= set("();&|"):
            if launches(segment):
                return True
            segment = []
        else:
            segment.append(word)
    return False


def plan() -> list[dict]:
    seen: set[str] = set()
    found = []
    for stage in stages("change"):
        for step in ([{"id": stage["id"], "review": True}] if stage["process"] == "review"
                     else stage["definition"]["steps"]):
            if step["id"] not in seen:
                seen.add(step["id"])
                found.append(step)
    return found


def acked(step: dict) -> bool:
    return step.get("actor") == "agent" and not step.get("review") and not any(
        name in FACTS for name in step.get("produces", []))


def session_values(state: dict) -> dict:
    return {"SessionId": state["session"], "GateCommand": NEXT.format(state["session"])}


def step_values(state: dict, key: str, head: str = "", **extra) -> dict:
    return {**session_values(state), "StepKey": key, "Head": head[:9], **extra}


def terms(state: dict) -> str:
    entry = state.get("entry", {})
    base = entry.get("base") or BASE_BRANCH
    return msg("gate_entry", base.removeprefix("origin/"), entry.get("review") or review_default(base),
               entry.get("rounds") or round_limit(state) or "max", entry.get("merge") or "human")


def claim(state: dict, steps: list[dict]) -> str:
    return (text_templates.prompt("session/claim", {**session_values(state), "StepDoc": doc(producer(steps, "branch"))})
            + "\n" + terms(state))


def reported(state: dict, outcome: str) -> str:
    return text_templates.prompt("session/report", {**session_values(state), "PipelineState": outcome})


def described(text: str, flags: str) -> str:
    return text + "\n" + text_templates.prompt("session/description", {"DescriptionFlags": flags})


def drafted(state: dict, data: dict, step: dict) -> str:
    return described(text_templates.prompt("session/draft", step_values(
        state, step["id"], StepDoc=doc(step), BaseBranch=data["base"].removeprefix("origin/"))),
        "--session " + state["session"])


def merge_label(steps: list[dict]) -> str:
    return producer(steps, "merged").get("handoff", {}).get("label", "")


def doc(step: dict) -> str:
    return step.get("doc", "") or develop_doc() + "#" + step["id"]


def producer(steps: list[dict], fact: str) -> dict:
    return next((step for step in steps if fact in step.get("produces", [])), {"id": fact})


def bound(workspace: Workspace, state: dict) -> dict | None:
    branch = state.get("branch", "")
    if not branch:
        return None
    data = read_json(workspace.paths(branch)[0], {})
    if data.get("branch") != branch:
        return {"branch": branch, "change": state.get("change", 0), "phase": "retired", "path": ""}
    number = int(data.get("change") or 0)
    if number and state.get("change") != number:
        pin(workspace.root, state, change=number)
    return data


def spoken(prompt: str) -> dict | None:
    called = skill_call(prompt)
    if not called:
        return None
    release = called.group("skill") == RELEASE_SKILL
    words, entry = prompt[called.end():].split(), {"release": True} if release else {}
    while words and words[0].startswith("--"):
        flag, value = words[0][2:], "".join(words[1:2])
        if flag not in ENTRY or not ENTRY[flag].fullmatch(value) or release and flag not in RELEASE_ENTRY:
            return {"error": " ".join(words[:2])}
        entry[flag] = value
        words = words[2:]
    return entry


def enter(root: Path, state: dict) -> dict:
    path = heard_file(root, state["session"])
    claimed = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        os.replace(path, claimed)
    except OSError:
        return state
    try:
        heard = read_json(claimed, None)
        if heard is None:
            return state
        if "error" in heard:
            raise RuntimeError(msg("gate_entry_flag", heard["error"]))
        if "base" in heard:
            heard["base"] = "origin/" + heard["base"].removeprefix("origin/")
        known = state.get("entry", {})
        if heard.get("release") and "release/tag-flow" not in module_paths():
            raise RuntimeError(msg("gate_release_module"))
        if known and bool(known.get("release")) != bool(heard.get("release")):
            leave = (RELEASE_NEXT if known.get("release") else NEXT).format(state["session"]) + " --leave"
            raise RuntimeError(msg("gate_entry_pipeline", RELEASE_SKILL if heard.get("release") else "issue-dev", leave))
        entry = {**known, **heard}
        pin(root, state, heard=heard)
        if entry != known:
            if state.get("branch"):
                raise RuntimeError(msg("gate_entry_bound", state["branch"], NEXT.format(state["session"]) + " --leave"))
            pin(root, state, entry=entry)
    except BaseException:
        with suppress(FileExistsError):
            os.link(claimed, path)
        raise
    finally:
        claimed.unlink(missing_ok=True)
    return state


@cache
def review_default(base: str) -> str:
    try:
        project = base_manifest(base, "project/manifest.json")
    except Blocked:
        project = {}
    if project and not module_on(project.get("modules", {}).get("review")):
        return "local"
    return project.get("modules", {}).get("agents", {}).get("settings", {}).get("review", "ci")


def local_model(agent: str) -> str:
    from agent_provider import KINDS

    return KINDS.get(agent, {}).get("model") or msg("gate_session_model")


def entry_base(root: Path, session: str, base: str, branch: str) -> str:
    state = live(root, session)
    state = enter(root, state) if state else state
    wanted = state.get("entry", {}).get("base", "") if state.get("branch", branch) == branch else ""
    if base and wanted and base != wanted:
        raise RuntimeError(msg("gate_entry_base", wanted, base))
    return base or wanted


def attach(root: Path, session: str, data: dict) -> None:
    state = live(root, session)
    if not state:
        raise RuntimeError(msg("gate_session_unknown", session))
    if state.get("branch", data["branch"]) != data["branch"]:
        heard = state.get("heard", {})
        state = {**fresh_state(session, state["agent"], state.get("transcript", "")), "entry": dict(heard), "heard": heard}
    state.update(branch=data["branch"], change=data.get("change", 0))
    entry = state.setdefault("entry", {})
    if "review" not in entry:
        entry["review"] = review_default(data["base"])
    save(root, state)


def change_of(workspace: Workspace, data: dict, state: dict):
    number = int(data.get("change") or state.get("change") or 0)
    if number:
        pull = forge.change(number)
        if pull and not data.get("change") and data.get("phase") == "active":
            workspace.adopt(pull)
        return pull
    opened = workspace.open_changes(data["branch"])
    if len(opened) != 1:
        return None
    workspace.adopt(opened[0])
    pin(workspace.root, state, change=opened[0].number)
    return opened[0]


def verdicts(pull) -> tuple[dict[str, dict], set[str]]:
    import review_evidence
    entries = review_policy.passes()
    found: dict[str, dict] = {}
    returned: set[str] = set()

    def owner(headline: str) -> str:
        return next((entry["id"] for entry in entries
                     if review_policy.mine(review_policy.Mark("", headline, ""), entry.get("marker", ""))), "")

    for item in reversed(forge.reviews(pull.number)):
        headline = review_policy.headline(item.body)
        name = owner(headline)
        if not name or review_policy.local(headline):
            continue
        if name in found:
            if review_policy.verdict(item.state, headline) == "changes" and review_evidence.review_author(item):
                returned.add(item.sha)
            continue
        mark = review_evidence.review_mark(pull.number, item, headline, item.body)
        state = review_policy.verdict(mark.state, headline) if mark else ""
        if state:
            found[name] = {"state": state, "sha": mark.sha, "at": mark.created, "source": "remote",
                           "task": mark.task}
            if state == "changes":
                returned.add(mark.sha)
    login = forge.login()
    for note in forge.notes(pull.number):
        headline = review_policy.headline(note.body)
        name, sha = owner(headline), review_policy.head_of(note.body)
        state, task = review_policy.verdict("", headline), review_policy.task_of(note.body)
        if not (name and sha and state and note.author == login and review_policy.local(headline)):
            continue
        if state == "changes":
            returned.add(sha)
        if moment(note.created) >= moment(found.get(name, {}).get("at", "")):
            found[name] = {"state": state, "sha": sha, "at": note.created, "source": "local",
                           "task": task, "direct": review_policy.directed(note.body)}
    return found, returned


def pending_passes(pull, current: dict) -> list[dict]:
    entries = review_policy.passes()
    if reopened(pull, current):
        return entries
    missing = {entry["id"] for entry in entries if entry["id"] not in current}
    files = (forge.change_files(pull.number) or []) if missing else []
    return [entry for entry in entries
            if current.get(entry["id"], {}).get("state") != "approve"
            and not (entry["id"] in missing and review_policy.prose_only(entry, files))]


def approved_head(current: dict) -> str:
    items = [current.get(entry["id"], {}) for entry in review_policy.passes()]
    if not items or any(item.get("state") != "approve" for item in items):
        return ""
    return max(items, key=lambda item: moment(item["at"]))["sha"]


def reopened(pull, current: dict) -> bool:
    import post_approval

    approved = approved_head(current)
    return bool(approved and approved != pull.head_sha and not post_approval.judge(approved, pull.head_sha).accepted)


def watching(root: Path, change: int) -> bool:
    beat = read_json(watch_file(root, change), {}).get("beat", "")
    return bool(beat) and time.time() - moment(beat) < 3 * WATCH_SECONDS


def clarify_step(state: dict, step: dict, path: Path) -> Step | None:
    clarify = state["clarify"]
    if clarify.get("done") or clarify.get("sdd") and (path / clarify["sdd"] / "spec.md").is_file():
        return None
    values = step_values(state, step["id"], StepDoc=doc(step),
                         ClarifyQuestions="\n".join("- " + item for item in clarify.get("questions", [])))
    if clarify.get("questions") and not clarify.get("answered"):
        return Step(step["id"] + "-wait", text_templates.prompt("session/clarify-wait", values), terminal=True)
    if clarify.get("questions"):
        return Step(step["id"] + "-answered", text_templates.prompt("session/clarify-answered", values))
    return Step(step["id"], text_templates.prompt("session/clarify", values))


def round_limit(state: dict) -> int | None:
    rounds = state.get("entry", {}).get("rounds", "")
    if rounds:
        return None if rounds == "max" else int(rounds)
    review = next((stage for stage in stages("change") if stage["process"] == "review"), {})
    return review.get("transitions", {}).get("changes", {}).get("maxVisits")


def review_step(root: Path, state: dict, data: dict, pull, head: str, current: dict, returned: set[str],
                pending: list[dict], stage: str) -> Step | None:
    import review_evidence
    values = step_values(state, stage, head)

    @cache
    def context() -> str:
        return review_evidence.task_context(pull.number)

    def fresh(verdict: dict) -> bool:
        task = verdict.get("task")
        return verdict["sha"] == head and (not task or task == hashlib.sha256(context().encode()).hexdigest())

    if not pending:
        if not current or any(item["source"] != "remote" for item in current.values()) or carried(head, current):
            return None
        return carry_step(state, pull, current, head, stage)
    fixes = [entry["id"] for entry in pending
             if current.get(entry["id"], {}).get("state") == "changes" and fresh(current[entry["id"]])]
    limit = round_limit(state)
    if limit is not None and len(returned) >= limit and (fixes or head not in returned):
        return final("blocked", state, pull, current, msg("gate_round_limit", len(returned), limit))
    if fixes:
        return Step("fix", text_templates.prompt("session/fix", {**values, "ReviewPasses": ", ".join(fixes),
                                                                 "ChangeLink": pull.url}))
    values["ReviewPasses"] = ", ".join(entry["id"] for entry in pending)
    answered = max((moment(current[entry["id"]]["at"]) for entry in pending
                    if current.get(entry["id"], {}).get("sha") == head), default=0)
    rounds = [item for item in state["rounds"] if item["head"] == head and moment(item["at"]) > answered]
    if not rounds:
        return Step("review", text_templates.prompt("session/review", values))
    last = rounds[-1]
    wait = fallback("waitMinutes", 45)
    values.update(WaitMinutes=wait, RoundReason=msg("gate_local_timeout", wait))
    if last["mode"] == "remote" and time.time() - moment(last["at"]) < wait * 60:
        return Step("review-wait", text_templates.prompt("session/review-wait", values), counted=False)
    if last["mode"] == "remote" and sum(item["mode"] == "remote" for item in rounds) <= fallback("retries", 1):
        return Step("review", text_templates.prompt("session/review-retry", values))
    path = state_file(root, state["session"]).with_suffix(f".{head[:9]}.context.md")
    path.write_text(context(), encoding="utf-8")
    key, why = local_reason(last, wait)
    passes = " ".join("--pass " + entry["id"] for entry in pending)
    agent = agent_kind(state["agent"])
    model = local_model(agent)
    return Step("local-review", text_templates.prompt("session/local-review", {
        **values, "RoundReason": why.splitlines()[0][:160], "WorkTree": data["path"], "ChangeNumber": pull.number,
        "LocalReasonKey": key, "LocalContext": path, "PassFlags": passes, "SessionAgent": agent,
        "ReviewModel": model}))


def local_reason(last: dict, wait: int) -> tuple[str, str]:
    key = last.get("reason") or "timeout"
    return key, last.get("error") or (msg("gate_local_timeout", wait) if key == "timeout"
                                      else text_templates.choice("LocalReason", key))


def carried(head: str, current: dict) -> bool:
    import review_verdict
    states = forge.commit_results(head)
    return all(getattr(states.get(review_verdict.context(name)), "state", "") == "success" for name in current)


def carry_step(state: dict, pull, current: dict, head: str, stage: str) -> Step:
    answered = max((moment(item["at"]) for item in current.values() if item["sha"] == head), default=0)
    rounds = [item for item in state["rounds"] if item["head"] == head and moment(item["at"]) > answered]
    wait = fallback("waitMinutes", 45)
    last = rounds[-1] if rounds else {}
    values = step_values(state, stage, head, WaitMinutes=wait)
    if last.get("mode") == "remote" and time.time() - moment(last["at"]) < wait * 60:
        return Step("review-wait", text_templates.prompt("session/review-wait", values), counted=False)
    if not rounds or last["mode"] == "remote" and len(rounds) <= fallback("retries", 1):
        return Step("review", text_templates.prompt("session/carry", values))
    why = local_reason(last, wait)[1]
    outcome = msg("gate_carry_failed", why)
    return final("blocked", state, pull, current, outcome)


def report(state: dict, pull, current: dict | None, outcome: str) -> dict:
    if current is None:
        lines = msg("value_unavailable")
    else:
        lines = ", ".join(f"{name}: {item['state']} ({item['source']}, {item['sha'][:9]})"
                          for name, item in sorted(current.items())) or msg("value_no")
    return {"PipelineState": outcome, "EntryTerms": "" if releasing(state) else terms(state), "ChangeLink": forge.change_link(pull.number) if pull else str(state.get("change") or "-"),
            "ReviewVerdicts": lines,
            "Assumptions": "\n".join("- " + item for item in state["assumptions"]) or msg("value_no")}


def final(kind: str, state: dict, pull, current: dict | None, outcome: str, **fields) -> Step:
    return Step(kind, reported(state, outcome), terminal=True, anchor=outcome,
                report=report(state, pull, current, outcome), **fields)


def summary(root: Path, state: dict, text: str) -> str:
    if not text.strip():
        raise RuntimeError(msg("gate_report_empty"))
    step = evaluate(root, state, reporting=True)
    if step.report is None:
        raise RuntimeError(msg("gate_report_none", state["session"]))
    return text_templates.render("report/session", {**step.report, "SessionSummary": text_data.inert(text.strip()),
                                                    **session_cost.cost(change_cost.transcript(state))})


def key_of(step: Step) -> str:
    return step.id + ":" + (step.head or digest(step.anchor or step.text)[:16])


def stalled(state: dict, step: Step) -> bool:
    stall = state.get("stall", {})
    return stall.get("key") == key_of(step) and stall.get("count", 0) >= STALL


class Flow:
    def __init__(self, root: Path, state: dict, workspace: Workspace, data: dict | None, steps: list[dict]):
        self.root, self.state, self.workspace, self.data, self.steps = root, state, workspace, data, steps
        self.path = Path(data["path"]) if data else None

    @cached_property
    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD") if self.path else ""

    @cached_property
    def dirty(self) -> bool:
        return dirty(self.path)

    @cached_property
    def committed(self) -> bool:
        return git(self.path, "rev-list", "--count", self.data["base"] + "..HEAD") != "0"

    @cached_property
    def pull(self):
        return change_of(self.workspace, self.data, self.state)

    @cached_property
    def history(self) -> tuple[dict, set[str]]:
        return verdicts(self.pull) if self.pull else ({}, set())

    @cached_property
    def current(self) -> dict:
        return self.history[0]

    @cached_property
    def returned(self) -> set[str]:
        return self.history[1]

    @cached_property
    def pending(self) -> list[dict]:
        return pending_passes(self.pull, self.current) if self.pull and not unreviewed(self.state) else []

    @cached_property
    def labels(self) -> set[str]:
        return {step["handoff"]["label"] for step in self.steps if step.get("handoff", {}).get("label")}

    def values(self, step: dict, **extra) -> dict:
        return step_values(self.state, step["id"], self.head, **{"StepDoc": doc(step), "WorkTree": self.path, **extra})

    def step(self, step: dict, text: str, **fields) -> Step:
        return Step(step["id"], text, head=self.head, **fields)

    def final(self, kind: str, outcome: str, **fields) -> Step:
        return final(kind, self.state, self.pull, self.current, outcome, **fields)

    def branch(self, step: dict) -> Step | None:
        return None if self.data else Step(step["id"], claim(self.state, self.steps))

    def clarified(self, step: dict) -> Step | None:
        return clarify_step(self.state, step, self.path)

    def merged_base(self, step: dict) -> Step | None:
        base = self.data["base"]
        behind = subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", base, "HEAD"],
                                capture_output=True, check=False).returncode == 1
        if not behind or self.committed and self.pull and self.pull.state == "open" and self.pending:
            return None
        return self.step(step, text_templates.prompt("session/sync-base", self.values(step, BaseRef=base)))

    def commits(self, step: dict) -> Step | None:
        if self.dirty:
            return self.step(step, text_templates.prompt("session/commit", self.values(step)))
        if self.committed:
            return None
        return self.step(step, text_templates.prompt("session/implement", self.values(step)))

    def clean(self, step: dict) -> Step | None:
        return self.step(step, text_templates.prompt("session/commit", self.values(step))) if self.dirty else None

    def pushed(self, step: dict) -> Step | None:
        remote = git(self.path, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + self.data["branch"],
                     optional=True)
        return None if remote == self.head else self.step(step, text_templates.prompt(
            "session/push", self.values(step, TaskBranch=self.data["branch"])))

    def opened(self, step: dict) -> Step | None:
        if not self.pull:
            return self.step(step, drafted(self.state, self.data, step))
        kept = change_cost.kept(self.pull.body)
        if not kept or kept[0] == self.state["session"] and change_cost.missing(kept[1]) and change_cost.journaled(self.state):
            text = text_templates.prompt("session/draft-stats", self.values(step, ChangeNumber=self.pull.number))
            return self.step(step, described(text, f"--change {self.pull.number} --session {self.state['session']}"))
        return None

    def ready(self, step: dict) -> Step | None:
        if not self.pull.draft:
            return None
        skipped = unreviewed(self.state)
        if not skipped and all(item["source"] == "remote" for item in self.current.values()):
            return self.step(step, text_templates.prompt("session/ready",
                                                         self.values(step, ChangeNumber=self.pull.number)))
        if skipped or self.state.get("entry", {}).get("review") == "local" and all(
                item.get("direct") for item in self.current.values() if item["source"] == "local"):
            values = self.values(step, ChangeNumber=self.pull.number)
            text = (text_templates.prompt("session/ready-unreviewed", values) if skipped
                    else text_templates.prompt("session/ready-local", values))
            return Step("ready-local", text, head=self.head)
        return None

    def verified(self, step: dict) -> Step | None:
        check = head_checks(self.head).get(check_context())
        state = getattr(check, "state", "")
        if state == "success" and getattr(check, "kind", "") == "job":
            return self.by_actor(step)
        import post_approval

        values = self.values(step, ChangeNumber=self.pull.number, CheckName=check_context(),
                             CheckUrl=getattr(check, "url", ""),
                             FixLines=post_approval.policy().get("lines", 0))
        if state in ("", "pending", "skipped", "success"):
            return Step("gates-wait", text_templates.prompt("session/gates-wait", values), head=self.head,
                        counted=False)
        return self.step(step, text_templates.prompt("session/gates-fix", values))

    def merged(self, step: dict) -> Step | None:
        return None if self.pull.state == "merged" else self.by_actor(step)

    def retired(self, step: dict) -> Step | None:
        return self.step(step, text_templates.prompt("session/cleanup", self.values(
            step, ChangeNumber=self.pull.number, MainClone=self.workspace.common.parent)))

    def by_actor(self, step: dict) -> Step | None:
        name, actor = step["id"], step.get("actor")
        ack = self.state["acks"].get(name, {})
        if actor == "command":
            if ack.get("head") == self.head and ack.get("ok"):
                return None
            tail = "\n" + ack["tail"] if ack.get("tail") else ""
            return self.step(step, text_templates.prompt("session/command-step", self.values(step)) + tail)
        if actor == "agent":
            epoch = len({item["head"] for item in self.state["rounds"] if item["head"] != self.head})
            if (ack.get("epoch") == epoch) if step.get("scope") == "round" else (ack.get("head") == self.head):
                return None
            return self.step(step, text_templates.prompt("session/agent-step", self.values(
                step, StepDoc=step.get("doc", ""))))
        if actor != "human":
            return None
        label = merge_label(self.steps)
        handed = self.state["acks"].get("handoff", {}).get("head")
        if (label and self.state.get("entry", {}).get("merge") == "agent" and not self.pull.draft
                and not handed == self.pull.head_sha == self.head):
            return Step("handoff", text_templates.prompt("session/handoff", self.values(
                step, MergeLabel=label, ChangeNumber=self.pull.number)), head=self.head)
        if self.labels & set(self.pull.labels):
            handoff = step.get("handoff", {})
            return self.step(step, text_templates.prompt("session/merge", self.values(
                step, MergeLabel=handoff["label"], MergeMethod=handoff.get("method", ""),
                ChangeNumber=self.pull.number, MergeHead=self.pull.head_sha))) \
                if handoff.get("label") else None
        if watching(self.root, self.pull.number):
            return self.final(step["id"], msg("gate_outcome_waiting"), head=self.head)
        return self.step(step, text_templates.prompt("session/watch", self.values(step, ChangeNumber=self.pull.number)))

    def check(self, step: dict) -> Step | None:
        if step.get("review"):
            if not self.pull:
                return self.opened(step)
            found = None if unreviewed(self.state) else review_step(
                self.root, self.state, self.data, self.pull, self.head, self.current, self.returned, self.pending, step["id"])
            if found is None and self.state.get("costed") != self.head:
                self.cost("costed", self.head, lambda: change_cost.publish(self.pull.number, self.state))
            return found
        known = [name for name in step.get("produces", []) if name in FACTS]
        for name in known:
            found = FACTS[name](self, step)
            if found:
                return found
        return None if known else self.by_actor(step)

    def result(self) -> None:
        if unreviewed(self.state):
            return
        try:
            done = list(self.current.values()) if self.pull and self.pull.state == "open" else []
            head = max(done, key=lambda item: moment(item["at"]))["sha"] if done else ""
            verdict = "changes" if any(item["sha"] == head and item["state"] == "changes" for item in done) \
                else "" if not done or self.pending else "approve"
        except forge.ForgeError as error:
            print(msg("gate_cost_unwritten", error), file=sys.stderr)
            return
        if verdict and self.state.get("resulted") != [head, verdict]:
            self.cost("resulted", [head, verdict],
                      lambda: change_cost.round_result(self.pull.number, self.state, head, verdict))

    def cost(self, flag: str, key: object, write) -> None:
        if change_cost.pending(self.state):
            print(msg("cost_journal_missing", self.state["session"]), file=sys.stderr)
            return
        try:
            write()
        except (forge.ForgeError, OSError, ValueError) as error:
            print(msg("gate_cost_unwritten", error), file=sys.stderr)
            return
        self.state = update(self.root, self.state["session"], lambda fresh: fresh.update({flag: key})) or self.state

    def ended(self) -> Step | None:
        if not (self.path and not self.dirty and self.committed and self.pull):
            return None
        if self.pull.state == "closed":
            return self.final("done", msg("gate_outcome_closed"))
        if self.pull.state != "merged":
            return None
        merge = next(index for index, step in enumerate(self.steps) if "merged" in step.get("produces", []))
        return next(filter(None, map(self.check, self.steps[merge + 1:])), None)

    def next(self) -> Step:
        if self.data:
            self.result()
        for step in self.steps:
            found = self.check(step)
            if found:
                return self.ended() or (replace(found, head=self.head) if found.head == "" and self.path else found)
        return final("done", self.state, self.pull, self.current, msg("gate_outcome_done"))


FACTS = {"branch": Flow.branch, "clarified": Flow.clarified, "merged-base": Flow.merged_base,
         "commits": Flow.commits, "simplification-checkpoint": Flow.clean, "pushed-head": Flow.pushed,
         "draft-pr": Flow.opened, "ready-pr": Flow.ready, "verified-head": Flow.verified, "merged": Flow.merged, "retired-workspace": Flow.retired}


def unreviewed(state: dict) -> bool:
    return round_limit(state) == 0


def releasing(state: dict) -> bool:
    return bool(state.get("entry", {}).get("release"))


class Release:

    def __init__(self, root: Path, state: dict):
        self.root, self.state = root, state
        self.branch, self.base = release_branches()
        self.found: dict[str, dict] = {}

    @cached_property
    def pull(self):
        number = self.state.get("release", 0)
        if number:
            return forge.change(number)
        found = opened_into(self.branch, CUT_PREFIX)
        if found:
            pin(self.root, self.state, release=found[0].number)
        return found[0] if found else None

    @cached_property
    def fix(self):
        return forge.change(self.state["fix"]) if self.state.get("fix") else None

    def values(self, **extra) -> dict:
        pull, fix = self.pull, self.fix
        return {**session_values(self.state), "GateCommand": RELEASE_NEXT.format(self.state["session"]),
                "ProductionBranch": self.branch, "BaseBranch": self.base,
                "ChangeNumber": pull.number if pull else "", "ChangeLink": forge.change_link(pull.number) if pull else "",
                "FixRef": forge.change_ref(fix.number) if fix else "", "PollMinutes": fallback("pollMinutes", 5),
                **extra}

    def checks(self, sha: str, merged: bool) -> dict:
        if sha not in self.found:
            self.found[sha] = {check.name: check for check in forge.checks(sha)} if merged else head_checks(sha)
        return self.found[sha]

    @staticmethod
    def failed(checks: dict) -> str:
        return ", ".join(f"{check.name} ({check.url})" if check.url else check.name for check in failing(checks, skipped))

    def cut(self, head: str = "") -> Step:
        return Step("release-cut", text_templates.prompt("session/release-cut", self.values()), head=head)

    def outcome(self, kind: str, text: str) -> Step:
        return final(kind, self.state, self.pull, None, text)

    def wait(self, sha: str, waited: str) -> Step:
        return Step("release-wait", text_templates.prompt("session/release-wait", self.values(ReleaseState=waited)),
                    head=sha, counted=False)

    def next(self) -> Step:
        acks, pull = self.state["acks"], self.pull
        if pull is None:
            if "release-cut" in acks:
                if self.cuttable():
                    return self.outcome("blocked", msg("gate_release_cut_missing", self.base))
                return self.outcome("done", msg("gate_release_empty"))
            return self.cut()
        if self.fix is not None and not (pull.state == "merged" and self.fix.base == self.base):
            return self.fixing(self.fix)
        if pull.state == "closed":
            return self.outcome("done", msg("gate_outcome_closed"))
        if pull.state == "open":
            return self.opened(pull)
        return self.delivered(pull.merge_sha)

    def cuttable(self) -> bool:
        from release import plan
        git(self.root, "fetch", "--quiet", "--tags", "origin", self.base, optional=True)
        return plan(git(self.root, "rev-parse", f"origin/{self.base}^{{commit}}"), major=False)["mode"] == "release"

    def opened(self, pull) -> Step:
        sha = pull.head_sha
        checks = self.checks(sha, False)
        check = checks.get(check_context())
        if getattr(check, "state", "") == "failure":
            return Step("release-fix", text_templates.prompt(
                "session/release-fix", self.values(FailedChecks=self.failed(checks))), head=sha)
        state = checks_state(checks, skipped)
        if state == "failure":
            return self.wait(sha, msg("gate_release_red", self.failed(checks)))
        checked = getattr(check, "state", "") == "success" and green(checks) and not state
        if checked and pull.draft:
            from pipeline_readiness import cut_of
            if not cut_of(sha, self.base):
                return self.outcome("blocked", msg("gate_release_not_cut", forge.change_ref(pull.number), self.base))
            return Step("release-ready", text_templates.prompt("session/release-ready", self.values()), head=sha)
        return self.wait(sha, msg("gate_release_merge") if checked else msg("gate_release_checks"))

    def fixing(self, fix) -> Step:
        if fix.state == "closed":
            return self.outcome("done", msg("gate_release_fix_closed", forge.change_ref(fix.number)))
        if fix.state == "open":
            checks = self.checks(fix.head_sha, False)
            state = checks_state(checks, skipped)
            if state == "failure":
                return Step("release-fix-checks", text_templates.prompt("session/release-fix-checks", self.values(
                    FailedChecks=self.failed(self.checks(fix.head_sha, False)))), head=fix.head_sha)
            if fix.base == self.base and getattr(checks.get(READY_STATUS), "state", "") != "success":
                return self.reviewing(fix, checks)
            ready = not fix.draft and not state and green(checks)
            if ready and self.state.get("entry", {}).get("merge") == "agent":
                return Step("release-merge-fix", text_templates.prompt("session/release-merge-fix", self.values()),
                            head=fix.head_sha)
            return self.wait(fix.head_sha, msg("gate_release_fix", forge.change_ref(fix.number)))
        if fix.base == self.branch:
            return self.delivered(fix.merge_sha)
        if self.pull.state == "open" or git(self.root, "ls-remote", "--heads", "origin", "refs/heads/" + self.pull.head):
            return Step("release-close", text_templates.prompt("session/release-close", self.values()), head=fix.merge_sha)

        def recut(fresh: dict) -> None:
            for key in ("release", "fix", "started", "fix_round"):
                fresh.pop(key, None)
            fresh["acks"] = {}

        update(self.root, self.state["session"], recut)
        recut(self.state)
        return self.cut(fix.merge_sha)

    def reviewing(self, fix, checks: dict) -> Step:
        mark = self.state.get("fix_round") or {}
        if mark.get("head") != fix.head_sha:
            mark = {"head": fix.head_sha, "tries": 0}
        pending = any(protected_status(name) and check.state == "pending" for name, check in checks.items())
        if pending and not mark.get("started"):
            mark = {**mark, "started": True, "since": mark.get("since") or now()}
            pin(self.root, self.state, fix_round=mark)
        if pending or mark.get("since") and not mark.get("started"):
            if time.time() - moment(mark["since"]) <= fallback("waitMinutes", 45) * 60:
                return self.wait(fix.head_sha, msg("gate_release_fix_round", forge.change_ref(fix.number)))
            if mark["tries"] >= fallback("retries", 1):
                return self.outcome("blocked", msg("gate_release_fix_stalled", forge.change_ref(fix.number)))
            mark = {"head": fix.head_sha, "tries": mark["tries"] + 1}
            pin(self.root, self.state, fix_round=mark)
        return Step("release-fix-review", text_templates.prompt("session/release-fix-review", self.values()),
                    head=fix.head_sha)

    def released(self, sha: str) -> dict:
        steps = get_records("processes.release-deliver.steps", [], file="methodology.json")
        workflow = next((gate for step in steps if step.get("id") == "deliver" for gate in step.get("gates", [])), "")
        paths: dict[str, str] = {}

        def own(check) -> bool:
            run = forge.run_of(check.url)
            if run and run not in paths:
                paths[run] = forge.run_path(run)
            return not run or paths[run] == workflow

        return {name: check for name, check in self.checks(sha, True).items() if own(check)}

    def delivered(self, sha: str) -> Step:
        found = self.released(sha)
        if not found:
            started = self.state.get("started", {}).get(sha) or now()
            if time.time() - moment(started) > RELEASE_START_MINUTES * 60:
                return self.outcome("done", msg("gate_release_missing", sha[:9], RELEASE_START_MINUTES))
            pin(self.root, self.state, started={sha: started})
        state = checks_state(found, skipped)
        if not found or state == "pending" or not runs_finished(found):
            return self.wait(sha, msg("gate_release_running"))
        if state == "failure":
            return self.accepted(sha) or Step("release-hotfix", text_templates.prompt("session/release-hotfix", self.values(
                FailedChecks=self.failed(found), SHA=sha)), head=sha)
        return self.accepted(sha) or Step("release-accept", text_templates.prompt("session/release-accept", self.values(
            SHA=sha, ReleaseJobs=", ".join(sorted(found)))), head=sha)

    def accepted(self, sha: str) -> Step | None:
        if self.state["acks"].get("release-accept", {}).get("head") == sha:
            return self.outcome("done", msg("gate_release_done"))
        return None


def skipped(name: str) -> bool:
    return name == agent_context() or protected_status(name)


def green(found: dict) -> bool:
    return getattr(found.get(check_context()), "kind", "") == "job"


RELEASE_DONE = ("release-cut", "release-accept")
RELEASE_RUN = {"ready": "release-ready", "close": "release-close", "review": "release-fix-review"}
FIXED = ("release-fix", "release-hotfix")


def release_act(root: Path, state: dict, args: argparse.Namespace) -> None:
    if args.local or args.question or args.assume or args.clarified or args.sdd or args.run and args.run not in RELEASE_RUN \
            or args.done and args.done not in RELEASE_DONE:
        raise RuntimeError(msg("gate_release_flag"))
    if not (args.done or args.run or args.fix):
        return
    flow = Release(root, state)
    step = flow.next()
    if args.done == "release-cut" and flow.pull is not None:
        return
    wanted = FIXED if args.fix else ("release-accept", "release-hotfix") if args.done == "release-accept" \
        else (args.done or RELEASE_RUN[args.run],)
    if step.id not in wanted:
        raise RuntimeError(msg("gate_release_early", " / ".join(wanted)) + "\n" + step.text)
    pull = flow.pull
    if args.fix:
        fix = forge.change(args.fix)
        target = flow.branch if step.id == "release-hotfix" else flow.base
        if fix is None or fix.state != "open" or fix.base != target:
            raise RuntimeError(msg("gate_release_fix_target", args.fix, target))
        head = git(root, "rev-parse", "HEAD")
        if head != fix.head_sha:
            raise RuntimeError(msg("gate_release_fix_head", args.fix, head, fix.head_sha))
        env = {**os.environ, **change_env(fix)}
        if subprocess.run([executable("python3"), ".harness/scripts/check/check-all.py", "--tree", "--base",
                           "origin/" + target], cwd=root, env=env, check=False).returncode:
            raise RuntimeError(msg("gate_release_fix_gates", args.fix))

    def ack(name: str, **fields) -> None:
        update(root, state["session"], lambda fresh: fresh["acks"].update({name: {"head": step.head, **fields}}))

    if args.run == "ready":
        mark_ready(pull.number, step.head)
    elif args.run == "review":
        fix = flow.fix
        forge.request_review(fix.number)
        mark = state.get("fix_round") or {}
        pin(root, state, fix_round={"head": fix.head_sha, "tries": mark.get("tries", 0) if mark.get("head") == fix.head_sha else 0,
                                    "since": now()})
    elif args.done:
        ack(args.done)
    elif args.fix:
        pin(root, state, fix=args.fix)
    else:
        if pull.state == "open":
            failed = flow.failed(flow.checks(pull.head_sha, False))
            fix = forge.change_ref(flow.fix.number)
            forge.close_noted(pull.number, text_templates.published(forge.CLOSE_NOTE, text_templates.render("comment/close-note", {
                "CloseReason": msg("gate_release_close_reason", failed), "CloseSuccessor": msg("gate_release_close_next", fix),
                "CloseKept": msg("gate_release_close_kept", fix), "CloseRemaining": msg("gate_release_close_left")})))
        try:
            forge.delete_branch(pull.head)
        except forge.ForgeError as error:
            if error.status != 404:
                raise


def waited(root: Path, state: dict, args: argparse.Namespace) -> Step:
    wait = fallback("waitMinutes", 45) * 60 if args.background else WAIT_SECONDS
    deadline, poll = time.time() + wait, min(fallback("pollMinutes", 5) * 60, wait / 4)
    step = evaluate(root, state)
    while args.wait and step.id in WAITS and time.time() < deadline:
        time.sleep(max(1.0, min(poll, deadline - time.time())))
        step = evaluate(root, state)
    return step


def evaluate(root: Path, state: dict, reporting: bool = False) -> Step:
    if releasing(state):
        try:
            release = Release(root, state)
            step = release.next()
            if reporting and step.report is None and stalled(state, step):
                step = replace(step, report=report(state, release.pull, None, msg("gate_stall", step.id, STALL)))
            return step
        except forge.ForgeError as error:
            return final("blocked", state, None, None, msg("gate_blocked", error))
    workspace = Workspace(root)
    flow = Flow(root, state, workspace, bound(workspace, state), plan())
    data = flow.data
    if data and (data.get("phase") == "retired" or not data["path"] or not Path(data["path"]).is_dir()):
        if not reporting:
            return final("done", state, None, {}, msg("gate_outcome_done"))
        try:
            return flow.final("done", msg("gate_outcome_done"))
        except forge.ForgeError:
            return final("done", state, None, None, msg("gate_outcome_done"))
    try:
        step = flow.next()
        if reporting and step.report is None and stalled(state, step):
            pull, current = (flow.pull, flow.current) if flow.data else (None, {})
            step = replace(step, report=report(state, pull, current, msg("gate_stall", step.id, STALL)))
        return step
    except forge.ForgeError as error:
        return final("blocked", state, None, None, msg("gate_blocked", error), head=flow.head)


def before_edit(root: Path, state: dict, paths: list[Path]) -> str:
    if releasing(state):
        main = Workspace(root).common.parent.resolve()
        inside = [path for path in paths if path.resolve().is_relative_to(main)]
        return msg("gate_release_edit") if inside else ""
    workspace = Workspace(root)
    if paths and all(path.resolve().is_relative_to(workspace.reviews) for path in paths):
        return ""
    data = bound(workspace, state)
    folder = Path(data["path"]).resolve() if data and data["path"] else None
    folders = [workspace.common.parent] + ([folder] if folder else [])
    if paths and not any(path.resolve().is_relative_to(place) for path in paths for place in folders):
        return ""
    steps = plan()
    if data is None:
        return claim(state, steps)
    if not data["path"]:
        return ""
    waiting = clarify_step(state, producer(steps, "clarified"), Path(data["path"]))
    track = state["clarify"].get("sdd")
    if waiting and not (track and paths and all(path.resolve().is_relative_to(folder / track) for path in paths)):
        return waiting.text
    if paths and not all(path.resolve().is_relative_to(folder) for path in paths):
        return text_templates.prompt("session/wrong-copy", {"WorkTree": data["path"]})
    return ""


def on_prompt(root: Path, session: str, transcript: str = "", prompt: str = "") -> None:
    entry = spoken(prompt)
    if entry is not None:
        write_json(heard_file(root, session), entry)

    def heard(state: dict) -> None:
        state.pop("stall", None)
        if entry is None:
            state.pop("heard", None)
        if state.get("clarify", {}).get("questions"):
            state["clarify"]["answered"] = True
        if transcript:
            state["transcript"] = transcript

    if live(root, session):
        update(root, session, heard)


def on_stop(root: Path, state: dict, transcript: str = "") -> str:
    remember(root, state, transcript)
    if background(transcript or change_cost.transcript(state)):
        return ""
    step = evaluate(root, state)
    key = key_of(step)
    if step.terminal and step.id == "done" and state.get("reported") == key:
        forget(root, state["session"])
        return ""
    reply = []

    def change(fresh: dict) -> None:
        if step.terminal:
            reply.append("" if fresh.get("reported") == key else step.text)
            fresh["reported"] = key
            return
        stall = fresh.get("stall", {})
        count = stall.get("count", 0) + 1 if stall.get("key") == key and step.counted else 1
        fresh["stall"] = {"key": key, "count": count}
        told = "\n\n" + reported(fresh, msg("gate_stall", step.id, STALL)) if count == STALL else ""
        reply.append("" if count > STALL else step.text + told)

    update(root, state["session"], change)
    return reply[0] if reply else ""


def resolve(root: Path, session: str) -> dict:
    if session:
        state = live(root, session)
        if not state:
            raise RuntimeError(msg("gate_session_unknown", session))
        return state
    if os.environ.get("HARNESS_AGENT_RUN"):
        raise RuntimeError(msg("gate_agent_run"))
    states = [read_json(path, {}) for path in sessions_dir(root).glob("*.json")]
    fresh = [state for state in states if state.get("session") and not state.get("done")
             and time.time() - moment(state.get("since", "")) < 120]
    if not fresh:
        raise RuntimeError(msg("gate_session_required"))
    return max(fresh, key=lambda state: state["since"])


def run_command(step: dict, data: dict, log: Path, green: str = "") -> dict:
    command = [executable(part) if part == "python3" else part for part in step["command"]]
    env = dict(os.environ)
    if ".harness/scripts/check/check-all.py" in command:
        command += ["--base", data["base"], *(["--since", green] if green else [])]
        change = forge.change(int(data["change"])) if data.get("change") else None
        if change is not None:
            env.update(change_env(change))
    with log.open("w", encoding="utf-8") as handle:
        code = subprocess.run(command, cwd=data["path"], env=env, stdout=handle, stderr=subprocess.STDOUT,
                              check=False).returncode
    tail = log_tail(log, 15).rstrip()
    print(tail)
    return {"ok": code == 0, "tail": tail}


OWNED = {"ready": "ready-local", "handoff": "handoff"}


def confirmed(pull, head: str, local: bool) -> None:
    if pull.head_sha != head:
        raise RuntimeError(msg("gate_owned_stale"))
    if not threads_resolved(pull.number):
        raise RuntimeError(msg("runtime_review_threads_are_unreadable_or_unresolved"))
    checks = head_checks(head)
    check = checks.get(check_context())
    if getattr(check, "state", "") != "success":
        raise RuntimeError(msg("runtime_missing_or_unsuccessful_required_check") + check_context())
    if getattr(check, "kind", "") != "job":
        raise RuntimeError(msg("runtime_required_ci_job_is_missing_a_same_name_status_is_insufficient") + check_context())
    if checks_state(checks, lambda name: name == agent_context() or local and protected_status(name)):
        raise RuntimeError(msg("runtime_a_pr_check_is_pending_or_failed"))
    ready = checks.get(READY_STATUS)
    if not local and (getattr(ready, "state", "") != "success" or getattr(ready, "kind", "") != "status"):
        raise RuntimeError(msg("runtime_current_protected_process_readiness_does_not_match_the_review_rou"))


def own(flow: Flow, action: str, head: str) -> None:
    step, pull = flow.next(), flow.pull
    if step.id != OWNED[action]:
        raise RuntimeError(msg("gate_owned_early", action) + "\n" + step.text)
    confirmed(pull, head, flow.state.get("entry", {}).get("review") == "local" or unreviewed(flow.state))
    if action == "handoff":
        forge.add_label(pull.number, merge_label(flow.steps))
        return
    mark_ready(pull.number, head)


def mark_ready(number: int, head: str) -> None:
    forge.mark_ready(number)
    seen = forge.change(number)
    if seen is None or seen.head_sha != head:
        raise RuntimeError(msg("gate_ready_stale", number))
    if seen.draft:
        raise RuntimeError(msg("gate_ready_kept"))


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pipeline-run.py next", description=msg("gate_cli"))
    parser.add_argument("pipeline", choices=("change", "release"))
    parser.add_argument("--session", default="")
    parser.add_argument("--done", default="")
    parser.add_argument("--run", default="", metavar="STEP")
    parser.add_argument("--local", default="", metavar="KEY")
    parser.add_argument("--question", action="append", default=[])
    parser.add_argument("--assume", action="append", default=[])
    parser.add_argument("--clarified", action="store_true")
    parser.add_argument("--sdd", default="")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--leave", action="store_true")
    parser.add_argument("--fix", type=int, default=0, metavar="PR")
    parser.add_argument("--report", type=argparse.FileType("r", encoding="utf-8"), metavar="FILE")
    args = parser.parse_args(argv)
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    try:
        state = resolve(root, args.session)
        if args.report:
            print(summary(root, state, args.report.read()))
            return 0
        if args.leave:
            forget(root, state["session"])
            print(msg("gate_left", state["session"]))
            return 0
        state = enter(root, state)
        if args.pipeline == "release" and not releasing(state):
            if "release/tag-flow" not in module_paths():
                raise RuntimeError(msg("gate_release_module"))
            if state.get("branch"):
                raise RuntimeError(msg("gate_entry_bound", state["branch"], NEXT.format(state["session"]) + " --leave"))
            pin(root, state, entry={**state.get("entry", {}), "release": True})
        if args.fix and not releasing(state):
            raise RuntimeError(msg("gate_release_flag"))
        if releasing(state):
            release_act(root, state, args)
            print(waited(root, live(root, state["session"]), args).text)
            return 0
        workspace = Workspace(root)
        data = bound(workspace, state)
        steps = plan()
        if (args.done or args.run) and not (data and data["path"]):
            raise RuntimeError(claim(state, steps))
        head = git(Path(data["path"]), "rev-parse", "HEAD") if args.done or args.run else ""
        commands = {step["id"]: step for step in steps if step.get("actor") == "command"}
        named = {step["id"] for step in steps if acked(step)}
        if args.done and args.done not in named:
            raise RuntimeError(msg("gate_step_unknown", args.done, ", ".join(sorted(named))))
        if args.run and args.run not in ("review", *OWNED, *commands):
            raise RuntimeError(msg("gate_step_unknown", args.run, ", ".join(["review", *OWNED, *commands])))
        flow = Flow(root, state, workspace, data, steps) if args.run in ("review", *OWNED) else None
        if args.run in OWNED:
            own(flow, args.run, head)
        green = state["acks"].get(args.run, {}).get("green", "")
        outcome = clean = None
        if args.run in commands:
            clean = not dirty(Path(data["path"]))
            outcome = run_command(commands[args.run], data,
                                  state_file(root, state["session"]).with_suffix(f".{args.run}.log"), green)
        entry = None
        if args.run == "review":
            pull = flow.pull
            if pull is None:
                raise RuntimeError(drafted(state, data, producer(steps, "draft-pr")))
            ordered = state.get("entry", {}).get("review") == "local" and flow.pending
            local = args.local or ("direct" if ordered else "")
            if local:
                text_templates.choice("LocalReason", local)
            entry = {"head": head, "at": now(), "mode": "local", "reason": local}
            if not local:
                try:
                    forge.request_review(pull.number)
                    entry.update(mode="remote", reason="")
                except forge.ForgeUnsettled as error:
                    entry.update(mode="remote", reason="unavailable", error=str(error))
                except (forge.ForgeError, RuntimeError, OSError) as error:
                    entry.update(reason="unavailable", error=str(error))

        def change(fresh: dict) -> None:
            clarify = fresh["clarify"]
            if args.question:
                clarify.update(questions=clarify.get("questions", []) + args.question, answered=False)
            if args.sdd:
                clarify["sdd"] = args.sdd
            if args.clarified:
                clarify["done"] = True
            fresh["assumptions"] += args.assume
            if args.done:
                fresh["acks"][args.done] = {
                    "head": head, "epoch": len({item["head"] for item in fresh["rounds"] if item["head"] != head})}
            if outcome is not None:
                fresh["acks"][args.run] = {"head": head, **outcome, "green": head if outcome["ok"] and clean else green}
            if args.run == "handoff":
                fresh["acks"]["handoff"] = {"head": head}
            if entry:
                fresh["rounds"].append(entry)

        state = update(root, state["session"], change)
        print(waited(root, state, args).text)
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, forge.ForgeError) as error:
        print(msg("gate_failed", error), file=sys.stderr)
        return 1


def await_merge(root: Path, change: int, detach: bool) -> int:
    main = common(root).parent
    if watching(root, change):
        print(msg("gate_watch_running", change))
        return 0
    if detach:
        flags = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        log = watch_file(root, change).with_suffix(".log")
        with log.open("a", encoding="utf-8") as handle:
            subprocess.Popen([sys.executable, str(Path(__file__).with_name("task-worktree.py")), "await-merge",
                              "--change", str(change), "--root", str(main)],
                             cwd=main, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, **flags)
        print(msg("gate_watch_started", change, log))
        return 0
    beat = watch_file(root, change)
    stop = time.time() + WATCH_DAYS * 86400
    try:
        while time.time() < stop:
            write_json(beat, {"pid": os.getpid(), "beat": now()})
            try:
                pull = forge.change(change)
            except forge.ForgeError as error:
                print(msg("gate_watch_retry", WATCH_SECONDS // 60, error), file=sys.stderr, flush=True)
                pull = None
            if pull and pull.state in ("merged", "closed"):
                if pull.state == "merged":
                    print(Workspace(main).cleanup(change), flush=True)
                return 0
            time.sleep(WATCH_SECONDS)
        return 1
    finally:
        beat.unlink(missing_ok=True)
