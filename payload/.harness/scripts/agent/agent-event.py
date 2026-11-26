#!/usr/bin/env python3
"""Событие агента задач: будить ли агента, куда направить событие GitLab, рабочая копия и сессия.

    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py gate [--agent LOGIN]
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py duplicate
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py account
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py route
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py invalidate --change 12
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py invalidate --source-run 123
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py after-round --change 12
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py workdir <каталог>
    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-event.py session --workdir <каталог> --log <файл> --reviewer <публикатор>

Событие прогона дает forge.event(). Агента зовет комментарий или ревью участника проекта, не бота,
с обращением `@claude`, `@codex` или `@cursor` в начале текста; исполнителя задает модуль ci-agent,
а не имя в обращении. Конец круга ревью зовет агента, когда круг состоялся - его страж
не пропущен, - на открытом запросе, который ведет конвейер: запрос открыт учетной записью агента или
несет метку processes.review.managedLabel.

gate пишет выходы джоба enabled и number; учетную запись агента называет --agent, без него - токен
агента площадки, без него - токен прогона. duplicate пишет выход duplicate - адрес конвейера, который
событие прогона подняло раньше (forge.earlier_run), пусто - такого нет. account пишет выход login - учетную запись токена прогона. route - задание harness-event
GitLab: добавленная метка круга ревью поднимает прогон ревью, обращение к агенту - прогон агента с
событием, прочее печатает строку. after-round зовет конец круга ревью GitLab. workdir клонирует
репозиторий токеном площадки без содержимого истории; открытый запрос читается на его head.
Ответ на note идет без авторской ветки, итог публикует обвязка после текстового гейта. session пишет промпт записи issue-agent из .harness/methodology.json рядом с логом и зовет
pipeline-run.py run change в рабочей копии; ожидание ревью возобновляется после круга.

Коды возврата: 0 - решено, 1 - отказ площадки, git, манифеста конвейера или сессии.
"""

from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from pipeline_state import journals, stage_ids  # noqa: E402
from task_workspace import Workspace  # noqa: E402
from text_data import inert  # noqa: E402
from session_results import session_result  # noqa: E402
import agent_provider  # noqa: E402
import review_evidence  # noqa: E402
import review_policy  # noqa: E402

import process_config  # noqa: E402
import process_validation  # noqa: E402
import pipeline_next  # noqa: E402
import review_verdict  # noqa: E402
import session_cost  # noqa: E402
import session_log  # noqa: E402
import text_gate  # noqa: E402
import text_templates  # noqa: E402

import argparse  # noqa: E402
from collections import Counter  # noqa: E402
from dataclasses import asdict  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from ci_common import (  # noqa: E402
    place_trusted,
    log_tail,
    agent_run_command,
    annotation,
    agent_context,
    check_context,
    forge,
    get,
    msg,
    tracker,
)
from manifest import module_paths  # noqa: E402
from pipeline_state import digest  # noqa: E402

AGENT = "issue-agent"
MANAGED = forge.review_label("managedLabel")

pipeline = process_config


class GitError(RuntimeError):
    pass


def reference(event: forge.Event | None) -> str:
    if event is None or event.number is None:
        return ""
    return tracker.issue_ref(event.number) if event.target == "issue" else forge.change_ref(event.number)


def same(login: str, other: str) -> bool:
    return bool(login) and login.casefold() == other.casefold()


def mentioned(event: forge.Event | None) -> bool:
    return (event is not None and event.kind in ("note", "review") and event.number is not None
            and event.member and not event.bot and forge.MENTION.match(event.body or "") is not None)


def called(event: forge.Event | None, agent: str = "") -> bool:
    return mentioned(event) and not same(account(agent), event.author)


def account(agent: str = "") -> str:
    return agent or forge.agent_login() or forge.login()


def guard_job() -> str:
    jobs = process_validation.ci_jobs(get("processes.review.workflow", "", file="methodology.json"))
    return process_validation.calling(process_validation.round_jobs(jobs), "guard")


def ran(run: str) -> bool:
    guard = guard_job()
    state = forge.run_jobs(run).get(guard) if guard else None
    return state is not None and state != "skipped"


def driven(change: forge.Change | None, agent: str) -> bool:
    return change is not None and change.state == "open" and (same(change.author, agent) or MANAGED in change.labels)


def wakes(event: forge.Event | None, agent: str) -> bool:
    if event is None:
        return False
    if event.kind != "round":
        return called(event, agent)
    if event.number is None or not event.run or not ran(event.run):
        return False
    return driven(forge.change(event.number), account(agent))


def verdict(event: forge.Event | None, enabled: bool) -> str:
    if event is None:
        return msg("event_none")
    if enabled:
        return msg("event_called", event.kind, reference(event))
    if event.kind == "round":
        return msg("event_round_idle", reference(event) or msg("value_absent"))
    return msg("event_not_called", event.kind)


def command_gate(args: argparse.Namespace) -> int:
    event = None
    try:
        event = forge.event()
        enabled = wakes(event, args.agent)
        print(verdict(event, enabled))
    except forge.ForgeError as error:
        if event is None or event.kind != "round":
            print(annotation("warning", "event_unread", error))
        else:
            print(annotation("warning", "event_round_unread", event.run, reference(event), error))
        enabled = False
    forge.set_output("enabled", "true" if enabled else "false")
    forge.set_output("number", "" if event is None or event.number is None else str(event.number))
    return 0


def invalidate(change) -> None:
    if change is None or change.state != "open":
        return
    target = review_verdict.Target(change.number, change.head_sha, forge.run_url(), change.base_sha)
    if not review_verdict.trusted_publisher(target):
        raise RuntimeError(msg("event_invalidation_requires_publisher"))
    current = forge.commit_results(target.head)
    login = forge.login()
    owned = {name: result for name, result in current.items() if result.author == login}
    ready = owned.get("pipeline/ready")
    passes = pipeline.passes()
    proofs = [(entry, proof) for entry in passes
              if (proof := owned.get(review_verdict.context(entry["id"]))) is not None
              and proof.state in ("success", "failure") and not proof.description.startswith("not-applicable:")]
    if not (ready and ready.state == "success") and not proofs:
        return
    try:
        context = review_evidence.task_context(change.number)
        target = review_verdict.Target(change.number, target.head, target.url, change.base_sha,
                               review_evidence.policy_hash(change.base_sha),
                               hashlib.sha256(context.encode()).hexdigest())
        task_digest = digest(context)
        stale = bool(ready and ready.state == "success" and ready.description !=
                     review_evidence.readiness_fingerprint(change, task_digest, policy=target.policy))
        reviews = None
        marks = None
        snapshot = {"head": target.head, "base": target.base, "policy": target.policy, "task": target.task}
        for entry, proof in proofs:
            name = entry["id"]
            if reviews is None:
                reviews = forge.reviews(change.number)
            if proof.state == "success":
                if stale:
                    continue
                if marks is None:
                    marks = [review_policy.Mark(review.state, review_policy.headline(review.body), review.sha, review.body,
                                                  review.created, target.base, target.policy, target.task)
                             for review in reversed(reviews) if review.author == login
                             and review_evidence.trusted_review(change.number, review) == review_evidence.TRUSTED]
                    marks.reverse()
                approved = review_policy.counts(marks, entry.get("marker", ""), target.head, target.base,
                                                 target.policy, target.task)[1] == review_policy.OK
                stale = not (approved and review_evidence.trusted_status(change.number, proof, snapshot, name, marks))
                continue
            matching = [review for review in reviews if review.author == login and review.sha == target.head
                        and review_verdict.round_mark(review_verdict.Target(change.number, target.head, proof.url), name) in review.body]
            if not any(review_evidence.receipt(review.body) for review in matching):
                continue
            stale = stale or not any(review_verdict.snapshot_matches(target, review.body) for review in matching)
        if not stale:
            return
        if not review_verdict.current(target):
            raise RuntimeError(msg("event_input_snapshot_changed"))
    except Exception:
        invalidate_statuses(change, target, login)
        raise
    invalidate_statuses(change, target, login)


def invalidate_statuses(change, target, login) -> None:
    observed = forge.change(change.number)
    if (observed is None or observed.state != "open" or observed.head_sha != target.head
            or observed.base_sha != target.base):
        raise RuntimeError(msg("event_input_snapshot_changed"))
    description = msg("event_review_inputs_changed")
    forge.set_commit_state(target.head, "pipeline/ready", "pending", description, target.url)
    for entry in pipeline.passes():
        if not review_verdict.status(target, entry["id"], "pending", description):
            raise RuntimeError(msg("event_invalidation_unobserved", entry["id"]))
    observed = forge.commit_results(target.head).get("pipeline/ready")
    if not observed or observed.state != "pending" or observed.author != login or observed.url != target.url:
        raise RuntimeError(msg("event_invalidation_unobserved", "pipeline/ready"))
    label = forge.review_label()
    if label in change.labels:
        forge.remove_label(change.number, label)
    print(description)


def command_invalidate(args: argparse.Namespace) -> int:
    try:
        change = forge.review_input_change(args.source_run) if args.source_run else forge.change(args.change)
        invalidate(change)
        return 0
    except (RuntimeError, OSError, ValueError) as error:
        print(forge.annotation("error", str(error)))
        return 1


def command_duplicate(args: argparse.Namespace) -> int:
    try:
        earlier = forge.earlier_run()
    except forge.ForgeError as error:
        print(annotation("error", "event_duplicate_unread", error))
        return 1
    forge.set_output("duplicate", earlier)
    if earlier:
        print(msg("event_duplicate", earlier))
    return 0


def command_account(args: argparse.Namespace) -> int:
    try:
        login = forge.login()
        forge.set_output(args.output, login)
    except forge.ForgeError as error:
        print(annotation("error", "event_account_unread", error))
        return 1
    print(msg("event_account", login))
    return 0


def launch(module: str, variables: dict[str, str], ref: str, event: forge.Event) -> int:
    if module not in module_paths():
        print(msg("event_module_off", module, ref))
        return 0
    try:
        url = forge.start_pipeline(variables, event=event)
    except forge.ForgeError as error:
        print(annotation("error", "event_start_failed", ref, error))
        return 1
    print(msg("event_started", module, ref, url))
    return 0


def command_route(args: argparse.Namespace) -> int:
    label = forge.review_label()
    try:
        event = forge.event()
        review = (event is not None and event.kind == "label" and event.target == "change"
                  and event.number is not None and label in event.labels)
        agent = not review and called(event)
    except forge.ForgeError as error:
        print(annotation("error", "event_unread", error))
        return 1
    if event is None:
        print(msg("event_none"))
        return 0
    if event.kind == "inputs" and event.target == "change":
        if type(event.number) is not int or event.number < 1:
            print(annotation("error", "event_unread", msg("forge_no_change", event.number)))
            return 1
        return launch("review", {"HARNESS_INVALIDATE": str(event.number)}, reference(event), event)
    if review:
        return launch("review", {"HARNESS_REVIEW": str(event.number)}, reference(event), event)
    if agent:
        return wake(event, reference(event))
    print(msg("event_route_idle", event.kind))
    return 0


def command_after_round(args: argparse.Namespace) -> int:
    ref = forge.change_ref(args.change)
    if AGENT not in module_paths():
        print(msg("event_module_off", AGENT, ref))
        return 0
    try:
        change = forge.change(args.change)
        if not driven(change, account()):
            print(msg("event_after_round_idle", ref))
            return 0
        earlier = forge.earlier_run()
        if earlier:
            print(msg("event_duplicate", earlier))
            return 0
        run = forge.run_id()
        event = forge.Event(kind="round", target="change", number=change.number, author="", member=False,
                            bot=False, body="", labels=(), sha=change.head_sha, run=run,
                            id=f"round:{change.number}:{run}" if run else "")
    except forge.ForgeError as error:
        print(annotation("error", "event_start_failed", ref, error))
        return 1
    return wake(event, ref)


def wake(event: forge.Event, ref: str) -> int:
    return launch(AGENT, {"HARNESS_AGENT": f"{event.target}-{event.number}"}, ref, event)


def git(*args: str, cwd: str | None = None, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        why = done.stderr.strip().splitlines()
        raise GitError(why[0] if why else msg("gate_exit_code", done.returncode))
    return done.stdout.strip()


def command_workdir(args: argparse.Namespace) -> int:
    path = os.path.abspath(args.path)
    try:
        event = forge.event()
        number = event.number if event is not None and event.target == "change" else None
        change = forge.change(number) if number is not None else None
        env = forge.git_env()
        git("clone", "--quiet", "--no-tags", "--filter=blob:none", forge.clone_url(), path, env=env)
        if event is None or replying(event, change):
            if change and change.state == "open":
                if subprocess.run(["git", "cat-file", "-e", change.head_sha + "^{commit}"], cwd=path,
                                  capture_output=True, check=False).returncode:
                    git("fetch", "--quiet", "origin", change.fetch_ref, cwd=path, env=env)
                git("checkout", "--quiet", "--detach", change.head_sha, cwd=path, env=env)
            place_trusted(path)
            where = git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
        else:
            workspace = Workspace(path)
            data = workspace.start(issue=str(event.number) if event and event.target == "issue" else "",
                                   change=change.number if change else 0)
            where = data["branch"]
    except (RuntimeError, OSError, ValueError) as error:
        print(annotation("error", "event_workdir_failed", path, error))
        return 1
    print(msg("event_workdir", path, where))
    return 0


def replying(event, change: forge.Change | None) -> bool:
    return event.kind == "note" or change is not None and (change.state != "open" or change.cross_repo)


def notes_for(event) -> list:
    return tracker.issue_notes(str(event.number)) if event.target == "issue" else forge.notes(event.number)


def reply_context(event, change: forge.Change | None, notes: list) -> str:
    item = tracker.issue(str(event.number)) if event.target == "issue" else change or forge.change(event.number)
    if item is None:
        raise RuntimeError(msg("runtime_current_pr_is_unavailable"))
    return json.dumps({"question": event.body, "object": asdict(item), "notes": [asdict(note) for note in notes]},
                      ensure_ascii=False)


class Rejected(RuntimeError):
    pass


REPLY_ERRORS = (RuntimeError, OSError, ValueError)


def reply_text(log: str) -> str:
    result = session_result(log) or {}
    if result.get("is_error") is not False or not str(result.get("result") or "").strip():
        raise RuntimeError(msg("event_reply_empty"))
    return result["result"]


def reply_marker(event) -> str:
    if not event.id:
        raise RuntimeError(msg("event_reply_id_missing"))
    return "<!-- harness-reply: " + digest([event.target, event.number, event.id, event.body]) + " -->"


def replied(event, marker: str, notes: list) -> bool:
    login = tracker.login() if event.target == "issue" else forge.login()
    return any(note.author == login and marker in note.body for note in notes)


def publish_reply(event, template: str, text: str, marker: str) -> None:
    body = text_templates.published(template, text, None, marker)
    if reason := text_gate.refusal(body):
        raise Rejected(reason)
    if event.target == "issue":
        tracker.comment_issue(str(event.number), body)
    else:
        forge.comment(event.number, body, marker)


def command_session(args: argparse.Namespace) -> int:
    try:
        event = forge.event()
    except forge.ForgeError as error:
        print(annotation("error", "event_unread", error))
        return 1
    if event is None or event.number is None:
        print(annotation("error", "event_none"))
        return 1
    change = None
    try:
        if event.kind != "note":
            change = forge.change(event.number) if event.target == "change" else None
            if event.target == "change" and change is None:
                raise RuntimeError(msg("runtime_current_pr_is_unavailable"))
        reply = replying(event, change)
        if not reply:
            workspace = Workspace(args.workdir)
            branch = change.head if change else workspace.find(issue=str(event.number))
            if not branch:
                raise RuntimeError(msg("workspace_identity_required"))
            data = workspace.read(branch)
            workspace.validate(Path(data["path"]))
            args.workdir = data["path"]
        fields, prompt = pipeline.agent_session(AGENT, {"EventKind": event.kind, "EventTarget": event.target,
                                                        "EventNumber": str(event.number), "EventRef": reference(event)})
    except (RuntimeError, OSError, ValueError) as error:
        print(forge.annotation("error", str(error)))
        return 1
    if reply:
        try:
            marker, notes = reply_marker(event), notes_for(event)
            if replied(event, marker, notes):
                return 0
            prompt += "\n\n" + text_templates.prompt("agent/event-reply", {"EventContext": reply_context(event, change, notes)})
        except REPLY_ERRORS as error:
            print(forge.annotation("error", str(error)))
            return 1
    prompt_file = os.path.join(os.path.dirname(os.path.abspath(args.log)), "agent-prompt.md")
    with open(prompt_file, "w", encoding="utf-8") as handle:
        handle.write(prompt.rstrip("\n") + "\n")
    print(msg("event_prompt", prompt_file, fields["kind"], fields["model"]), flush=True)
    if reply:
        command = agent_run_command(fields["kind"], "reply", fields["model"], fields["effort"], prompt_file, args.log,
                                    workdir=args.workdir, allowed=fields["allowed_tools"],
                                    disallowed=fields["disallowed_tools"], minutes=50)
        for _ in range(2):
            code = subprocess.run(command, check=False).returncode
            if code:
                return code
            try:
                text = text_templates.render("comment/note", {
                    "NoteType": text_templates.choice("NoteType", "reply"),
                    "AgentReply": inert(reply_text(args.log).rstrip()),
                    "SessionModel": session_cost.outcome(session_result(args.log) or {})["SessionModel"],
                    "SessionLink": agent_provider.session_link(), "SHA": git("rev-parse", "HEAD", cwd=args.workdir)})
                publish_reply(event, "comment/note", text, marker)
                return 0
            except Rejected as error:
                print(forge.annotation("warning", str(error)))
                with open(prompt_file, "a", encoding="utf-8") as handle:
                    handle.write("\n" + text_templates.prompt("agent/event-reply-retry", {"ReplyRejection": error}) + "\n")
            except REPLY_ERRORS as error:
                print(forge.annotation("error", str(error)))
                return 1
        try:
            publish_reply(event, "comment/reply-rejected", text_templates.render("comment/reply-rejected", {}), marker)
        except REPLY_ERRORS as error:
            print(forge.annotation("error", str(error)))
        return 1
    if event.target == "change" and MANAGED:
        try:
            forge.add_label(event.number, MANAGED)
        except forge.ForgeError as error:
            print(forge.annotation("error", str(error)))
            return 1
    command = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline", "pipeline-run.py"), "run", "change",
               "--task", prompt_file, "--workdir", args.workdir, "--reviewer", args.reviewer,
               "--required-check", check_context(),
               "--required-check", "pipeline/ready", "--ignore-check", agent_context()]
    if event.target == "change":
        command += ["--change", str(event.number)]
    else:
        command += ["--issue", str(event.number)]
    started, since = time.monotonic(), time.time()
    with open(args.log, "w", encoding="utf-8") as output:
        code = 1
        if event.kind == "round":
            ready = [*command]
            ready[2] = "ready"
            task_flag = ready.index("--task")
            del ready[task_flag:task_flag + 2]
            code = subprocess.run(ready, cwd=args.workdir, stdout=output, stderr=subprocess.STDOUT, check=False).returncode
        if event.kind != "round" or code == 1:
            draft, review = stage_ids("change", "draft-pr")
            for mode, until in (("run", draft), ("verify", draft), ("run", review)):
                phase = [*command, "--until", until]
                phase[2] = mode
                code = subprocess.run(phase, cwd=args.workdir, stdout=output, stderr=subprocess.STDOUT, check=False).returncode
                if code:
                    break
    if code not in (0, 2):
        print(log_tail(args.log), end="", file=sys.stderr, flush=True)
    waiting = code == 2
    result = {"type": "result", "subtype": "success" if code in (0, 2) else "error",
              "is_error": code not in (0, 2), "result": msg("event_pipeline_waiting") if waiting else
              msg("event_pipeline_verified") if code == 0 else msg("event_pipeline_failed"),
              "duration_ms": (time.monotonic() - started) * 1000, **stage_spent(Path(args.workdir), since)}
    with open(args.log, "a", encoding="utf-8") as output:
        output.write("\n" + json.dumps(result) + "\n")
    print(result["result"], flush=True)
    return 0 if waiting else code


def stage_spent(workdir: Path, since: float) -> dict:
    states = [pipeline_next.read_json(path, {}) for path in
              journals(pipeline_next.common(workdir)).glob("*/state.json") if path.stat().st_mtime >= since]
    models = Counter(state["model"] for state in states if state.get("model"))
    return {"usage": session_cost.total(state.get("usage", {}) for state in states),
            "documents": session_log.merged(*(state.get("documents") for state in states)),
            "model": models.most_common(1)[0][0] if models else ""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate", help="будить ли агента: выходы enabled и number")
    gate.add_argument("--agent", default="", metavar="LOGIN", help="учетная запись агента; пусто - токен прогона")
    gate.set_defaults(handler=command_gate)
    duplicate = commands.add_parser("duplicate", help="выход duplicate: конвейер, который событие подняло раньше")
    duplicate.set_defaults(handler=command_duplicate)
    account_parser = commands.add_parser("account", help="учетная запись токена")
    account_parser.add_argument("--output", default="login", help="имя выхода CI")
    account_parser.set_defaults(handler=command_account)
    commands.add_parser("route", help="задание harness-event GitLab").set_defaults(handler=command_route)
    invalidate = commands.add_parser("invalidate", help="снять актуальность ревью после изменения входов PR")
    invalidation_source = invalidate.add_mutually_exclusive_group(required=True)
    invalidation_source.add_argument("--change", type=int)
    invalidation_source.add_argument("--source-run", default="")
    invalidate.set_defaults(handler=command_invalidate)
    after = commands.add_parser("after-round", help="конец круга ревью GitLab")
    after.add_argument("--change", type=int, required=True)
    after.set_defaults(handler=command_after_round)
    workdir = commands.add_parser("workdir", help="клон репозитория для сессии агента")
    workdir.add_argument("path")
    workdir.set_defaults(handler=command_workdir)
    session = commands.add_parser("session", help="промпт и сессия агента в рабочей копии")
    session.add_argument("--workdir", required=True, help="рабочая копия от workdir")
    session.add_argument("--log", required=True, help="лог сессии; промпт ложится рядом")
    session.add_argument("--reviewer", required=True, help="учетная запись защищенного публикатора ревью")
    session.set_defaults(handler=command_session)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
