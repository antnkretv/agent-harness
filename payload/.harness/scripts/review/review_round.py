from __future__ import annotations

from session_results import session_result

import agent_provider
import review_evidence
import review_inputs
import process_config as pipeline
import review_policy as planner
import review_threads as threads
import review_verdict as verdict
import text_data
import text_templates
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from ci_common import ROOT, bash, agent_run_command, forge, get, msg, place_trusted, placed_digest, tracker

PIPELINE = "methodology.json"

HERE = os.path.dirname(os.path.abspath(__file__))

PLAN_VERSION = 2

PROVISIONING = os.path.join(ROOT, ".harness", "provisioning", "scripts")

TRIES = 3

FETCH_DEPTH = 50

SESSION_MINUTES = 25

PLAN_LIMIT = 64 * 1024

HEAD_REF = "refs/review/head"
BASE_REF = "refs/review/base"

LOW_SPEED = (("http.lowSpeedLimit", "1024"), ("http.lowSpeedTime", "60"))

VERDICT_STATES = ("approved", "changes", "commented")

DEFERRED = "{}.result.jsonl"
STAGE = "{}.stage"


class RoundError(RuntimeError):
    pass


class OwnerUnread(RoundError):
    pass


def say(message: str) -> None:
    print(f"review-round: {message}", flush=True)


def report(level: str, text: str) -> None:
    print(forge.annotation(level, text), flush=True)


def flat(text: str) -> str:
    return (text or "").replace("\r\n", " ").replace("\n", " ")


def phrase(name: str, number: int) -> str:
    return text_templates.fill(pipeline.phrase(name), {"ChangeRef": forge.change_ref(number)})


def needs() -> dict:
    try:
        found = json.loads(os.environ.get("NEEDS") or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def job_outputs() -> dict[str, str]:
    found: dict[str, str] = {}
    for info in needs().values():
        outputs = info.get("outputs") if isinstance(info, dict) else None
        if isinstance(outputs, dict):
            found.update({key: str(value) for key, value in outputs.items()})
    return found


def load_plan(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError) as error:
        raise RoundError(msg("round_no_plan", error)) from None
    if not isinstance(found, dict) or found.get("v") != PLAN_VERSION or not isinstance(found.get("passes"), list):
        raise RoundError(msg("round_no_plan", path))
    return found


def registry(number: int, reviewer: str, notes: list) -> list:
    try:
        reviews = forge.reviews(number)
    except forge.ForgeError as error:
        raise RoundError(msg("round_reviews_failed", forge.change_ref(number), error)) from None
    words = "|".join(re.escape(get(f"processes.review.verdicts.{state}", "", file=PIPELINE)) for state in ("approve", "changes"))
    decision = re.compile(rf"\b(?:{words})[.\s]*$")
    rows = [mark for item in reversed(reviews) if item.author == reviewer and item.state in VERDICT_STATES
            and (mark := review_evidence.review_mark(number, item, planner.headline(item.body), flat(item.body)))]
    rows.reverse()
    rows += [planner.Mark("commented", planner.headline(note.body), "", flat(note.body), note.created)
             for note in notes if note.author != reviewer and decision.search(planner.headline(note.body)) and note.member]
    return sorted(rows, key=lambda mark: mark.created)


def changed_files(number: int) -> list[str]:
    try:
        found = forge.change_files(number)
    except forge.ForgeError:
        found = None
    if found is None:
        report("warning", msg("round_files_partial", forge.change_ref(number)))
        return []
    return list(found)


def resolved(number: int, reviewer: str) -> list[str]:
    try:
        items = forge.review_threads(number)
    except forge.ForgeError:
        items = None
    if items is None:
        report("warning", msg("round_threads_unread", forge.change_ref(number)))
        return []
    return threads.resolved_context(items, [reviewer])


def newest(lines: list[str], budget: int, cut: str) -> list[str]:
    if budget <= 0:
        return lines
    kept, used = [], 0
    for line in reversed(lines):
        used += len(line) + 1
        if used > budget:
            break
        kept.append(line)
    kept.reverse()
    return ([cut] if len(kept) < len(lines) else []) + kept


def decided(entry: dict, marks: list, texts: dict[str, str], closed: list[str]) -> str:
    marker = entry.get("marker", "")
    depth = get("processes.review.stallRounds", 0, file=PIPELINE)
    chars = get("processes.review.decidedChars", 0, file=PIPELINE)
    kept = []
    for mark in marks:
        if not planner.mine(mark, marker):
            continue
        body = mark.body[:chars] + " " + texts["decided-cut"] if 0 < chars < len(mark.body) else mark.body
        kept.append(f"- {mark.state} ({mark.sha[:7]}) {mark.headline}: {body}")
    if depth > 0:
        kept = kept[-depth:]
    text = "\n".join(kept) or texts["decided-empty"]
    if entry.get("threads") and closed:
        text += "\n\n" + texts["decided-resolved"] + "\n" + "\n".join(newest(closed, chars * depth, texts["decided-cut"]))
    say(msg("round_decided_count", entry.get("id", ""), text.count("\n") + 1))
    return text


def notice(number: int, reviewer: str, notes: list, lines: list[str]) -> None:
    said = [note.body for note in notes if note.author == reviewer]
    for line in lines:
        data = text_data.marked(line, "comment/review-stalled")
        data.pop("template", None)
        if text_data.said(said, "comment/review-stalled", data, line.split(".", 1)[0]):
            continue
        try:
            forge.comment(number, line)
        except forge.ForgeError as error:
            report("warning", msg("round_stall_unsent", error))


def compose(number: int, label: str, failed: str) -> dict:
    change = forge.change(number)
    if change is None:
        raise RoundError(msg("round_no_change", forge.change_ref(number), forge.repo()))
    why = yielded(forge.run_url(), number, change.head_sha)
    if why:
        say(why)
        return {"v": PLAN_VERSION, "change": number, "head": change.head_sha, "base": change.base,
                "fetch": change.fetch_ref, "reviewer": "", "url": forge.run_url(), "yielded": why,
                "passes": [{"id": entry.get("id", ""), "title": entry.get("title", ""), "job": entry.get("job", ""),
                            "result": verdict.context(entry.get("id", "")), "run": False,
                            "why": why, "decided": ""}
                           for entry in planner.passes()]}
    forge.remove_label(number, label)
    try:
        forge.remove_label(number, failed)
    except forge.ForgeError as error:
        report("warning", msg("round_unlabel_failed", failed, error))

    provider = pipeline.ci_kind()
    if provider not in pipeline.RUNNER_KINDS:
        raise RoundError(msg("round_provider_unknown", provider, ", ".join(pipeline.RUNNER_KINDS)))
    entries = planner.passes()
    if not entries:
        raise RoundError(msg("round_passes_empty"))
    if not agent_provider.authorized(provider):
        raise RoundError(msg("round_no_auth", provider, ", ".join(agent_provider.CREDENTIALS.get(provider, ())) or "-"))
    if not all(get(f"processes.review.verdicts.{state}", "", file=PIPELINE) for state in ("approve", "changes")):
        raise RoundError(msg("round_verdict_words"))
    texts = {name: pipeline.phrase(name) for name in ("decided-missing", "decided-empty", "decided-cut",
                                                      "decided-resolved")}
    if not all(texts.values()):
        raise RoundError(msg("round_phrases_missing"))
    for key in ("allowedTools", "disallowedTools"):
        if not get(f"processes.review.{key}", "", file=PIPELINE):
            raise RoundError(msg("round_tools_unset", key))

    reviewer = forge.login()
    if change.author == reviewer:
        raise RoundError(msg("round_self_review", forge.change_ref(number), reviewer))
    try:
        notes = forge.notes(number)
    except forge.ForgeError as error:
        raise RoundError(msg("round_notes_failed", forge.change_ref(number), error)) from None
    marks = registry(number, reviewer, notes)
    changed = changed_files(number)
    closed = resolved(number, reviewer) if any(entry.get("threads") for entry in entries) else []
    base_sha = getattr(change, "base_sha", "")
    policy = review_evidence.policy_hash(base_sha)
    task = review_evidence.task_hash(number)
    if not base_sha:
        raise RoundError(msg('runtime_current_base_sha_is_unavailable_review_cannot_be_planned'))
    routes = {item.name: item for item in planner.route(marks, changed, change.head_sha, base_sha, policy, task,
                                                        change.fetch_ref)}

    target = verdict.Target(number, change.head_sha, forge.run_url(), base_sha, policy, task)
    if not verdict.trusted_publisher(target):
        raise RoundError(msg("runtime_readiness_publication_requires_the_protected_ci_publisher_job"))
    forge.set_commit_state(target.head, "pipeline/ready", "pending", msg("runtime_ready_evidence_pending"), target.url)
    for item in routes.values():
        own = verdict.round_mark(target, item.name)
        done = next((mark for mark in marks if mark.sha == change.head_sha and mark.base == base_sha
                     and mark.policy == policy and mark.task == task and own in mark.body), None)
        if item.run and done is not None:
            written = verdict.restate(target, item.name, done.state, done.headline)
        elif item.run:
            written = verdict.pending(target, item.name)
        else:
            written = verdict.skip(target, item.name, item.why, item.outcome, changed)
        if not written:
            raise RoundError(msg("round_pending_failed", item.name) if item.run else msg("round_skip_failed", item.name))
    notice(number, reviewer, notes, [item.stalled for item in routes.values() if item.stalled])

    plan = {
        "v": PLAN_VERSION,
        "change": number,
        "head": change.head_sha,
        "base": change.base,
        "base_sha": base_sha,
        "policy": policy,
        "task": task,
        "changed": changed,
        "fetch": change.fetch_ref,
        "reviewer": reviewer,
        "url": forge.run_url(),
        "passes": [{
            "id": entry.get("id", ""),
            "title": entry.get("title", ""),
            "job": entry.get("job", ""),
            "result": verdict.context(entry.get("id", "")),
            "run": routes[entry.get("id", "")].run,
            "why": routes[entry.get("id", "")].why,
            "outcome": routes[entry.get("id", "")].outcome,
            "since": routes[entry.get("id", "")].since,
            "decided": decided(entry, marks, texts, closed),
        } for entry in entries],
    }
    size = len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > PLAN_LIMIT:
        raise RoundError(msg("round_plan_too_big", size, PLAN_LIMIT))
    raised = [item["id"] for item in plan["passes"] if item["run"]]
    rest = [item["id"] for item in plan["passes"] if not item["run"]]
    say(msg("round_planned", change.head_sha[:7], ", ".join(raised) or "-", ", ".join(rest) or "-"))
    return plan


def prepare(args: argparse.Namespace) -> int:
    label = forge.review_label()
    failed = forge.review_label("failedLabel")
    try:
        plan = compose(args.change, label, failed)
        os.makedirs(os.path.dirname(os.path.abspath(args.plan)), exist_ok=True)
        with open(args.plan, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if args.outputs:
            for item in plan["passes"]:
                forge.set_output(f"run_{item['id']}", "true" if item["run"] else "false")
            forge.set_output("run_audit", "false" if plan.get("yielded") else "true")
        return 0
    except OwnerUnread as error:
        report("error", str(error))
        return 1
    except Exception as error:  # noqa: BLE001
        report("error", msg("round_prepare_failed", error, failed, label))
        if not mark(args.change, failed):
            report("error", msg("round_mark_failed", forge.change_ref(args.change), failed))
        return 1


def mark(number: int, failed: str) -> bool:
    try:
        forge.add_label(number, failed)
        return True
    except Exception:  # noqa: BLE001
        return False


def git(cwd: str, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return review_inputs.git(cwd, *args, env=env, check=check)
    except RuntimeError as error:
        raise RoundError(str(error)) from None


def ready(tree: str, head: str) -> tuple[bool, str]:
    present = git(tree, "cat-file", "-e", f"{head}^{{commit}}", check=False).returncode == 0
    found = git(tree, "merge-base", head, BASE_REF, check=False) if present else None
    return present, found.stdout.decode().strip() if found is not None and found.returncode == 0 else ""


def checkout(tree: str, plan: dict) -> str:
    env = forge.git_env({**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    refs = [f"+{plan['fetch']}:{HEAD_REF}", f"+refs/heads/{plan['base']}:{BASE_REF}"]

    def fetch() -> None:
        shutil.rmtree(tree, ignore_errors=True)
        os.makedirs(tree)
        git(tree, "init", "--quiet")
        git(tree, "remote", "add", "origin", forge.clone_url())
        for key, value in LOW_SPEED:
            git(tree, "config", key, value)
        git(tree, "fetch", "--quiet", "--no-tags", f"--depth={FETCH_DEPTH}", "origin", *refs, env=env)

    try:
        retried(f"git fetch {plan['fetch']}", fetch, pause=15)
    except RoundError:
        raise RoundError(msg("round_tree_failed", TRIES, plan["fetch"])) from None

    head = plan["head"]
    present, base = ready(tree, head)
    if not (present and base) and git(tree, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true":
        git(tree, "fetch", "--quiet", "--no-tags", "--unshallow", "origin", *refs, env=env)
        present, base = ready(tree, head)
    if not present:
        tip = git(tree, "rev-parse", HEAD_REF, check=False).stdout.decode().strip()
        raise RoundError(msg("round_head_mismatch", forge.change_ref(plan["change"]), head[:7], tip[:7] or "-"))
    if git(tree, "rev-parse", BASE_REF).stdout.decode().strip() != plan.get("base_sha"):
        raise RoundError(msg('runtime_fetched_base_no_longer_matches_the_planned_snapshot'))
    if not base:
        raise RoundError(msg("round_no_merge_base", head[:7], plan["base"]))
    git(tree, "checkout", "--quiet", "--force", "--detach", head)
    place_trusted(tree)
    say(msg("round_tree", tree, head[:7], base[:7]))
    return base


def retried(what: str, call, pause: int = 5):
    for number in range(1, TRIES):
        try:
            return call()
        except (forge.ForgeUnavailable, tracker.TrackerUnavailable):
            raise
        except (forge.ForgeError, tracker.TrackerError, RoundError) as error:
            report("warning", msg("round_input_retry", what, number, TRIES, error, number * pause))
            time.sleep(number * pause)
    return call()


def context_text(number: int, chars: int, full: str) -> str:
    try:
        raw = retried(forge.change_ref(number), lambda: review_evidence.task_context(number))
    except RuntimeError as error:
        raise RoundError(str(error)) from None
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(raw)
    if 0 < chars < len(raw):
        raw = raw[:chars] + "\n" + phrase("input-context-cut", number) + "\n" + msg("runtime_full_task_context", full)
    return phrase("input-context", number) + "\n\n" + raw


def collect(tree: str, data: str, base: str, plan: dict, found: dict, entry: dict) -> None:
    os.makedirs(data, exist_ok=True)
    number = plan["change"]
    try:
        written = review_inputs.write_inputs(tree, data, base, plan["head"], phrase("input-diff", number))
        written += [name for name in [review_inputs.write_fixes(tree, data, entry.get("since", ""), plan["head"], base)] if name]
    except RuntimeError as error:
        raise RoundError(str(error)) from None
    say(msg("round_maps_built", ", ".join(written)))
    if found.get("context"):
        with open(os.path.join(data, "context.md"), "w", encoding="utf-8") as handle:
            handle.write(context_text(number, int(found.get("context_chars") or 0), os.path.join(data, "task-context.md")))


def converse(name: str, found: dict, tree: str, data: str, work: str, log: str) -> int:
    prompt = os.path.join(work, "prompt.md")
    with open(prompt, "w", encoding="utf-8") as handle:
        handle.write(found["prompt"])
    command = agent_run_command(found["kind"], "review", found["model"], found.get("effort", ""), prompt, log,
                                workdir=tree, add_dirs=(ROOT, data),
                                allowed=get("processes.review.allowedTools", "", file=PIPELINE),
                                disallowed=get("processes.review.disallowedTools", "", file=PIPELINE),
                                minutes=SESSION_MINUTES, inputs=data)
    say(msg("round_session_start", name, found["kind"], found["model"], found.get("effort") or "-"))
    return subprocess.run(command, check=False).returncode


def install_cli(provider: str) -> None:
    done = subprocess.run([bash(), os.path.join(PROVISIONING, "ensure-agent-cli.sh"), provider],
                          stdout=subprocess.PIPE, text=True, encoding="utf-8", check=False)
    print(done.stdout, end="", flush=True)
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        raise RoundError(msg("round_cli_failed", provider, done.returncode))
    shell_path = '. "$0" && ' + ('cygpath -wp "$PATH"' if os.name == "nt" else 'printf %s "$PATH"')
    found = subprocess.run([bash(), "-c", shell_path, os.path.join(PROVISIONING, "toolchain-env.sh")],
                           capture_output=True, text=True, encoding="utf-8", check=False)
    path = found.stdout.rstrip("\r\n") if found.returncode == 0 and found.stdout else os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join((os.path.dirname(lines[-1]), path))


def digest(name: str, kind: str, log: str, tree: str) -> None:
    script = os.path.join(os.path.dirname(HERE), "agent", "session-log-digest.py")
    if not os.path.exists(script):
        report("warning", msg("round_no_digest", script))
        return
    subprocess.run([sys.executable, script, log, kind], env=dict(os.environ, PASS=name, SESSION_TREE=tree), check=False)


def entry_of(plan: dict, name: str) -> dict:
    found = next((item for item in plan["passes"] if item.get("id") == name), None)
    if found is None:
        raise RoundError(msg("round_pass_unknown", name))
    return found


def run_pass(args: argparse.Namespace) -> int:
    name = args.id
    try:
        plan = load_plan(args.plan)
        entry = entry_of(plan, name)
    except RoundError as error:
        report("error", str(error))
        return 1
    if not entry.get("run"):
        say(msg("round_pass_not_raised", name, entry.get("why", "")))
        return 0
    number = plan["change"]
    target = round_target(plan)
    if not verdict.current(target):
        report("error", msg('runtime_review_plan_is_stale_or_its_base_policy_is_missing'))
        return 1
    try:
        owner = newer_round(plan.get("url", ""), number, plan["head"])
    except OwnerUnread as error:
        report("error", str(error))
        return 1
    if owner and args.defer_publish:
        say(msg("round_superseded_idle", entry.get("result", ""), owner))
        return 0
    if owner:
        report("error", msg("round_superseded", name, owner))
        return 1
    if not args.defer_publish:
        published = verdict.landed(target, name)[1]
        if published is not None:
            return 0 if verdict.restate(target, name, published.state, planner.headline(published.body)) else 1
    try:
        install_cli(pipeline.ci_kind())
    except (RoundError, OSError) as error:
        report("error", str(error))
        return 1
    work = tempfile.mkdtemp(prefix=f"review-{name}-", dir=os.environ.get("RUNNER_TEMP") or None)
    tree, data, log = (os.path.join(work, part) for part in ("head", "input", "session.jsonl"))
    stage, kind, failed = "", "", False
    try:
        try:
            base = checkout(tree, plan)
        except (RoundError, OSError, forge.ForgeError):
            stage = "checkout"
            raise
        found = pipeline.session(name, forge.change_link(number), data, entry.get("decided", ""))
        collect(tree, data, base, plan, found, entry)
        kind = found["kind"]
        before = tree_snapshot(tree)
        if converse(name, found, tree, data, work, log) != 0:
            stage, failed = "action", True
        elif before != tree_snapshot(tree) or not verdict.current(target):
            stage, failed = "snapshot", True
            raise RoundError(msg('runtime_review_source_base_or_policy_changed_during_analysis'))
        else:
            write_receipt(log, target, name)
    except (RoundError, OSError, ValueError, forge.ForgeError, tracker.TrackerError) as error:
        report("error", str(error))
        failed = True
    finally:
        if stage:
            failure_stage(args.plan, args.defer_publish, name, stage)

    if args.defer_publish:
        keep_result(log, deferred_result(args.plan, name), target, name, not failed)
        if not failed:
            settle(log, number, plan["head"])
        if kind:
            digest(name, kind, log, tree)
        return 1 if failed else 0
    try:
        owner = newer_round(plan.get("url", ""), number, plan["head"])
        if owner:
            report("error", msg("round_superseded", name, owner))
        published = not owner and verdict.publish(target, name, log)
    except RuntimeError as error:
        report("error", msg("verdict_publish_failed", error))
        published = False
    if kind:
        digest(name, kind, log, tree)
    return 0 if published and not failed else 1


def beside(plan_path: str, pattern: str, name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(plan_path)), pattern.format(name))


def round_target(plan: dict) -> verdict.Target:
    return verdict.Target(plan["change"], plan["head"], plan.get("url") or forge.run_url(),
                          plan.get("base_sha", ""), plan.get("policy", ""), plan.get("task", ""))


def deferred_result(plan_path: str, name: str) -> str:
    return beside(plan_path, DEFERRED, name)


def settle(log: str, number: int, head: str) -> None:
    try:
        if review_evidence.decision(session_result(log) or {}) == "approve":
            forge.set_output(review_evidence.APPROVED_STEP, f"{number} {head}")
    except (OSError, forge.ForgeError) as error:
        report("warning", msg("round_output_failed", review_evidence.APPROVED_STEP, error))


def failure_stage(plan_path: str, deferred: bool, name: str, stage: str) -> None:
    try:
        if deferred:
            with open(beside(plan_path, STAGE, name), "w", encoding="utf-8") as handle:
                handle.write(stage)
        else:
            forge.set_output(f"failure_stage_{name}", stage)
    except (OSError, forge.ForgeError) as error:
        report("warning", msg("round_output_failed", f"failure_stage_{name}", error))


def failure_stages(plan_path: str, plan: dict) -> dict[str, str]:
    found = {}
    for entry in plan["passes"]:
        name = entry.get("id", "")
        try:
            with open(beside(plan_path, STAGE, name), encoding="utf-8") as handle:
                found[f"failure_stage_{name}"] = handle.read().strip()
        except OSError:
            continue
    return found


def tree_snapshot(tree: str) -> str:
    digest = hashlib.sha256()
    digest.update(git(tree, "rev-parse", "HEAD").stdout)
    digest.update(git(tree, "diff", "--binary", "HEAD").stdout)
    for raw in git(tree, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0"):
        if not raw:
            continue
        path = os.path.join(tree, os.fsdecode(raw))
        digest.update(raw + b"\0")
        if os.path.islink(path):
            digest.update(os.readlink(path).encode())
        elif os.path.isfile(path):
            with open(path, "rb") as handle:
                digest.update(handle.read())
    digest.update(placed_digest(tree))
    return digest.hexdigest()


def write_receipt(log: str, target: verdict.Target, name: str) -> None:
    with open(log, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    with open(log + ".receipt.json", "w", encoding="utf-8") as handle:
        json.dump({"head": target.head, "base": target.base, "policy": target.policy,
                   "pass": name, "log": digest, "exit": 0, "run": target.url, "task": target.task}, handle, sort_keys=True)


def keep_result(log: str, path: str, target: verdict.Target, name: str, valid: bool) -> None:
    try:
        os.unlink(path + ".receipt.json")
    except FileNotFoundError:
        pass
    try:
        found = session_result(log)
    except OSError:
        return
    with open(path, "w", encoding="utf-8") as handle:
        if found:
            handle.write(json.dumps(found, ensure_ascii=False) + "\n")
    shutil.copyfile(log, path + ".session.jsonl")
    if valid and found and verdict.execution_receipt(log, target, name):
        write_receipt(path, target, name)


def job_results() -> dict[str, str] | None:
    given = needs()
    if given:
        return {job: str(info.get("result") or "") for job, info in given.items() if isinstance(info, dict)}
    try:
        return retried(msg("round_what_jobs"), forge.run_jobs)
    except forge.ForgeError:
        return None


def yielded(url: str, number: int, head: str) -> str:
    try:
        earlier = forge.earlier_run()
    except forge.ForgeError as error:
        raise OwnerUnread(msg("round_duplicate_unread", error)) from None
    if earlier:
        return msg("round_duplicate", earlier)
    return superseded(url, number, head)


def superseded(url: str, number: int, head: str) -> str:
    newer = newer_round(url, number, head)
    return msg("round_superseded_round", newer) if newer else ""


def newer_round(url: str, number: int, head: str) -> str:
    newer = newer_on(url, head)
    if newer:
        return newer
    current = read_change(number, OwnerUnread(msg("round_head_unread", forge.change_ref(number)))).head_sha
    return newer_on(url, current) if current != head else ""


def newer_on(url: str, head: str) -> str:
    found = head_results(head)
    if found is None:
        raise OwnerUnread(msg("round_owner_unread", head[:7]))
    contexts = {verdict.context(entry.get("id", "")) for entry in planner.passes()}
    newer = [result.url for name, result in found.items()
             if name in contexts and round_number(result.url) > round_number(url)]
    return max(newer, key=round_number) if newer else ""


def read_change(number: int, refusal: RoundError):
    try:
        change = retried(forge.change_ref(number), lambda: forge.change(number))
    except forge.ForgeError:
        change = None
    if change is None:
        raise refusal
    return change


def round_number(url: str) -> int:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def head_results(head: str) -> dict[str, forge.Check] | None:
    try:
        return retried(msg("round_what_results", head[:7]), lambda: forge.commit_results(head))
    except forge.ForgeError:
        return None


def why_missing(entry: dict, results: dict[str, str], outputs: dict[str, str]) -> str:
    job = entry.get("job", "")
    stage = outputs.get(f"failure_stage_{entry.get('id', '')}", "")
    result = results.get(job, "") if entry.get("run") else "skipped"
    if stage == "checkout":
        return msg("round_why_checkout", job)
    if stage == "action":
        return msg("round_why_action", job)
    if result == "success":
        return msg("round_why_silent", job)
    if result == "cancelled":
        return msg("round_why_cancelled", job)
    if result == "skipped":
        return msg("round_why_skipped", job)
    if not result:
        return msg("round_why_no_result", job)
    return msg("round_why_environment", job, result)


def verify(args: argparse.Namespace, lines: list[str]) -> str:
    def note(text: str) -> None:
        report("error", msg("round_round_failed", text))
        lines.append(msg("round_report_line", text))

    try:
        plan = load_plan(args.plan)
    except RoundError:
        plan = None
    head = (plan or {}).get("head", "")
    results = job_results()
    if results is None:
        note(msg("round_note_jobs_unread", TRIES))
        return head
    prepared = results.get(args.prepare_job, "")
    if prepared != "success":
        note(msg("round_note_prepare", prepared or msg("round_unknown")))
        return head
    if not plan or not plan["passes"]:
        note(msg("round_note_no_passes"))
        return head
    found = head_results(head)
    if found is None:
        note(msg("round_note_results_unread", TRIES))
        return head
    target = round_target(plan)
    if not verdict.current(target):
        note(msg("round_note_plan_stale"))
        return head
    read, reviews = verdict.listed(target)
    if not read:
        note(msg("round_note_reviews_unread", verdict.TRIES))
        return head

    outputs = {**job_outputs(), **(failure_stages(args.plan, plan))}
    for entry in plan["passes"]:
        title = entry.get("title") or entry.get("id", "")
        result = found.get(entry.get("result", ""))
        state = result.state if result else ""
        foreign = bool(entry.get("run") and result and result.url != plan.get("url"))
        if entry.get("outcome") == "budget-exhausted":
            note(msg("round_note_pass", title, entry.get("why") or msg("plan_budget_spent")))
            continue
        if not entry.get("run"):
            if not verdict.skip(target, entry.get("id", ""), entry.get("why", ""),
                                entry.get("outcome", "blocked"), plan.get("changed")):
                note(msg("round_note_skip_unverified", title))
            continue
        if state in ("success", "failure") and not foreign:
            if entry.get("run") and verdict.own(reviews, target, entry.get("id", "")) is None:
                report("error", msg("round_pass_unpublished", entry.get("id", ""), entry.get("result", "")))
                lines.append(msg("round_report_pass", title, msg("round_why_unpublished")))
            else:
                say(msg("round_result_state", entry.get("result", ""), head[:7], state))
            continue
        why = msg("round_why_foreign", result.url, entry.get("job", "")) if foreign else why_missing(entry, results, outputs)
        report("error", msg("round_pass_missing", entry.get("id", ""), entry.get("result", ""), why, entry.get("job", "")))
        lines.append(msg("round_report_pass", title, why))
    say(msg("round_results_title", head[:7]))
    for context, result in sorted(found.items()):
        print(f"{context}\t{result.state}")
    return head


def tell(number: int, lines: list[str], head: str) -> None:
    anchor = "\n".join(lines)
    data = {"Reasons": hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:16], "SHA": head}
    values = {"ReviewReasons": anchor, "ReviewMode": msg("review_mode_ci"), "ReviewWorkflow": forge.run_url(),
              "SHA": head or msg("round_unknown")}
    body = (text_templates.render("comment/review-guard", values)
            + "\n\n" + text_templates.mark("comment/review-guard", data))
    try:
        login = forge.login()
        said = text_data.said([note.body for note in forge.notes(number) if note.author == login
                               and head[:7] in note.body], "comment/review-guard", data, anchor)
    except forge.ForgeError:
        said = False
    if said:
        report("notice", msg("round_already_said", forge.change_ref(number)))
        return
    try:
        forge.comment(number, body)
    except forge.ForgeError:
        report("error", msg("round_comment_failed", forge.change_ref(number)))


def publish_deferred(plan: dict, path: str) -> None:
    target = round_target(plan)
    for entry in (entry for entry in plan["passes"] if entry.get("run")):
        try:
            verdict.publish(target, entry.get("id", ""), deferred_result(path, entry.get("id", "")))
        except RuntimeError as error:
            report("error", msg("verdict_publish_failed", error))


def guard(args: argparse.Namespace) -> int:
    failed = forge.review_label("failedLabel")
    try:
        try:
            plan = load_plan(args.plan)
        except RoundError:
            plan = {}
        change = None if plan else forge.change(args.change)
        head = plan.get("head") or (change.head_sha if change else "")
        check = superseded if plan else yielded
        why = plan.get("yielded") or (check(plan.get("url") or forge.run_url(), args.change, head) if head else "")
        if why:
            say(why)
            return 0
        if args.publish_deferred and plan:
            publish_deferred(plan, args.plan)
        lines: list[str] = []
        head = verify(args, lines) or head
        if not lines:
            return 0
        tell(args.change, lines, head)
    except OwnerUnread as error:
        report("error", str(error))
        return 1
    except Exception as error:  # noqa: BLE001
        report("error", msg("round_guard_failed", error))
    if not mark(args.change, failed):
        report("error", msg("round_label_failed", forge.change_ref(args.change), failed))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Круг ревью: подготовка, проход и страж.")
    commands = parser.add_subparsers(dest="command", required=True)

    first = commands.add_parser("prepare", help="состав круга, реестры и результаты ожидания")
    first.add_argument("--change", required=True, type=int, help="номер запроса на слияние")
    first.add_argument("--plan", required=True, help="куда записать план круга файлом")
    first.add_argument("--outputs", action="store_true", help="run_<id> и run_audit выходами задания")

    single = commands.add_parser("pass", help="один проход: CLI, дерево, данные, сессия, вердикт")
    single.add_argument("id")
    single.add_argument("--plan", required=True, help="план круга файлом")
    single.add_argument("--defer-publish", action="store_true",
                        help="только сессия: итог рядом с планом, вердикт публикует guard --publish-deferred")

    last = commands.add_parser("guard", help="сверка результатов проходов на голове запроса")
    last.add_argument("--change", required=True, type=int, help="номер запроса на слияние")
    last.add_argument("--prepare-job", required=True, help="имя задания подготовки в прогоне")
    last.add_argument("--plan", required=True, help="план круга файлом")
    last.add_argument("--publish-deferred", action="store_true",
                      help="сначала вердикты отложенных проходов из файлов итога рядом с планом")
    gave = commands.add_parser("yielded", help="код 0, если круг уступил другому и аудит не нужен")
    gave.add_argument("--plan", required=True, help="план круга файлом")
    args = parser.parse_args()

    if args.command == "prepare":
        return prepare(args)
    if args.command == "yielded":
        try:
            return 0 if load_plan(args.plan).get("yielded") else 1
        except RoundError as error:
            report("error", str(error))
            return 2
    if args.command == "pass":
        return run_pass(args)
    return guard(args)
