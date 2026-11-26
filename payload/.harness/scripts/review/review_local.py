from __future__ import annotations

from session_results import session_result

import review_evidence
import review_policy
import review_inputs
import review_verdict
import agent_results
import change_cost
import session_cost
import text_data
import text_templates
from agent_models import Outcome, SessionError
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from ci_common import AGENT_RUN, BASE_BRANCH, ROOT, SCRIPT_TREES, agent_run_command, forge, get, get_records, msg
from process_config import RUNNER_KINDS, dev_kind
from authorship import session_agent, sign, signatures, signed
from review_trust import RUNNER, SCRIPTS, BASE_CONFIG, admin_of, DELEGATION, FETCH_TIMEOUT, base_runner, current_base, delegate, fetch, git, install_kit, pushed, say, say_err, snapshot

PIPELINE = "methodology.json"

GATE = "check-pipeline.py"

CONTEXT_FILE = "context.md"
INPUT_DIR = "input"

VERDICT = re.compile(r"^\s*[#*_>\s]*VERDICT:\s*(approve|changes)\b", re.I | re.M)

STATE_WORD = {state: get(f"processes.review.verdicts.{state}", state, file=PIPELINE) for state in ("approve", "changes")}

PLAIN = {0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2015: "-",
         0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x00AB: '"', 0x00BB: '"',
         0x2026: "...", 0x00B7: "-", 0x00D7: "x", 0x00A0: " "}

DECOR = re.compile("[\u2190-\u21FF]")
DROP = re.compile("[\u2700-\u27BF\U0001F300-\U0001FAFF\u2600-\u26FF\uFE0F\u200B-\u200D\uFEFF]")


def base_reader(base: str):
    path = next((os.path.join(base, tree.split("/", 1)[0], "lib", "manifest.py") for tree in SCRIPT_TREES
                 if os.path.isdir(os.path.join(base, tree))), os.path.join(base, SCRIPTS, "..", "lib", "manifest.py"))
    spec = importlib.util.spec_from_file_location("manifest_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(msg("local_no_base_reader", path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def base_script(base: str, name: str) -> Path | None:
    found = sorted(path for tree in SCRIPT_TREES for path in Path(base, tree).rglob(name))
    return found[0] if len(found) == 1 else None


def on_base(base: str) -> bool:
    return base_script(base, Path(RUNNER).name) is not None


def fallback_reader(reader, base: str):
    if reader.get("processes.review.fallback.label", "", file=PIPELINE):
        return reader.get
    if on_base(base):
        raise RuntimeError(msg("local_base_without_block"))
    say_err(msg("local_block_from_pr"))
    return get


def setting(reader, key: str, default):
    return reader(f"processes.review.fallback.{key}", default, file=PIPELINE)


def denies(reader) -> str:
    return reader("processes.review.disallowedTools", "", file=PIPELINE)


def phrase(reader, name: str) -> str:
    for records in (reader.get_records("prompts.phrases", [], file=PIPELINE),
                    get_records("prompts.phrases", [], file=PIPELINE)):
        found = next((item.get("text", "") for item in records if item.get("id") == name), "")
        if found:
            return found
    return ""


def bound(pr: str) -> str:
    prefix = get("project.gitConfigPrefix", "")
    if not prefix:
        return msg("local_no_identity")
    branch = git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    registered = git("config", "--get", f"branch.{branch}.{prefix}-pr", check=False)
    if not registered:
        return msg("local_pr_unregistered", branch, pr)
    if registered != str(pr):
        return msg("local_pr_other", branch, registered, pr)
    return ""


def prepare_tree(base_ref: str, head: str, base: str) -> None:
    git("worktree", "add", "--detach", head, "HEAD")
    git("worktree", "add", "--detach", base, base_ref)
    install_kit(base)
    rules = snapshot(base)
    install_kit(head)
    if snapshot(base) != rules:
        raise RuntimeError(msg("local_base_touched", base))


def drop_tree(head: str, base: str) -> None:
    for path in (base, head):
        if os.path.isdir(path):
            git("worktree", "remove", "--force", path, repo=path, check=False)


def provider(requested: str = "") -> str:
    chosen = os.environ.get("AGENT_PROVIDER") or ""
    if chosen:
        return chosen
    return next((kind for kind in (requested, session_agent(), dev_kind()) if kind in RUNNER_KINDS), "")


def emit_pass(base: str, name: str, pr: str, kind: str, data: str) -> dict:
    gate = base_script(base, GATE)
    if gate is None:
        raise RuntimeError(msg("local_base_gate_missing", name, base, GATE))
    env = dict(os.environ, AGENT_PROVIDER=kind, ReviewInput=data)
    result = subprocess.run([sys.executable, gate, "--emit-pass", name, "--change", pr],
                            capture_output=True, text=True, encoding="utf-8",
                            env=env, check=False)
    if result.returncode != 0:
        raise RuntimeError(msg("local_no_prompt", name, result.stderr.strip()))
    try:
        fields = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(msg("local_no_prompt", name, error)) from None
    if not isinstance(fields, dict) or not fields.get("prompt"):
        raise RuntimeError(msg("local_prompt_empty", name))
    return fields


def local_block(base: str, path: str, base_ref: str, reason: str, context: str, missing: str = "") -> str:
    full = os.path.join(base, path)
    if not os.path.exists(full):
        if on_base(base):
            raise RuntimeError(msg("local_base_without_prompt", path))
        full = os.path.join(ROOT, path)
        say_err(msg("local_prompt_from_pr", path))
    try:
        with open(full, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise RuntimeError(msg("local_prompt_missing", path, error))

    return text_templates.fill(text, {
        "LocalBaseRef": base_ref,
        "LocalReason": reason,
        "LocalContext": context or missing,
        "LocalContextMissing": missing,
    })


def start(entry: dict, prompt: str, head: str, allowed: str, disallowed: str, work: str, context: str,
          minutes: int, data: str):
    name = entry["id"]
    log = os.path.join(work, f"{name}.jsonl")
    prompt_file = os.path.join(work, f"{name}.prompt.md")
    with open(prompt_file, "w", encoding="utf-8") as handle:
        handle.write(prompt)
    command = agent_run_command(entry["kind"], "review", str(entry.get("model", "")), str(entry.get("effort") or ""),
                                prompt_file, log, workdir=head,
                                add_dirs=[data] + ([os.path.dirname(context)] if context else []),
                                allowed=allowed, disallowed=disallowed, minutes=minutes, inputs=data)
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_REMOTE", None)
    errors = os.path.join(work, f"{name}.err")
    handles = (open(os.path.join(work, f"{name}.out"), "w", encoding="utf-8"), open(errors, "w", encoding="utf-8"))
    process = subprocess.Popen(command, cwd=head, env=env, stdout=handles[0],
                               stderr=handles[1], text=True)
    return {"entry": entry, "process": process, "log": log, "errors": errors, "handles": handles}


def plain(text: str) -> str:
    return DROP.sub("", DECOR.sub("=>", text.translate(PLAIN)))


def stop(jobs: list) -> None:
    for job in jobs:
        job["process"].kill()
        for handle in job["handles"]:
            handle.close()


def verdict_body(entry: dict, mark: str, reason: str, state: str, body: str, stamp: dict, cost: dict,
                 work: str) -> str:
    head = {"ReviewType": entry.get("marker", ""), "ReviewVerdict": STATE_WORD.get(state, state),
            "ReviewMode": msg("local_review_mode", mark, reason), "SHA": stamp["SHA"],
            "ReviewRound": stamp.get("Round", "")}
    values = {**head, **cost, "ReviewIcon": review_policy.icon(state),
              "ReviewWorkflow": forge.run_url() or os.path.abspath(work)}
    frame = len(text_templates.render("review/verdict", {**values, "ReviewResult": ""}))
    text = text_templates.render("review/verdict", {
        **values, "ReviewResult": review_verdict.plain(body.strip(), review_verdict.MAX_BODY - frame)})
    data = {**head, "Task": stamp["Task"], "Provider": stamp["Provider"], "LocalReasonKey": stamp.get("Reason", ""),
            **session_cost.marked(cost),
            "Digest": text_data.digest(text)}
    return f"{text}\n\n{text_templates.mark('review/verdict', data)}\n"


def collect(job: dict, mark: str, reason: str, work: str, limit: float, stamp: dict) -> dict:
    entry, process = job["entry"], job["process"]
    name = entry["id"]
    try:
        process.wait(timeout=max(1.0, limit - time.time()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return {"id": name, "state": "", "problem": msg("local_pass_timeout", job['log'])}
    finally:
        for handle in job["handles"]:
            handle.close()

    try:
        row = session_result(job["log"])
    except OSError:
        row = None
    if row is None:
        return {"id": name, "state": "", "problem": msg("local_agent_output", job['log'], job['errors'])}
    if process.returncode != 0 or row.get("is_error"):
        return {"id": name, "state": "", "problem": msg("local_agent_code", process.returncode, job['log'])}
    cost = session_cost.outcome(row)
    return format_result(entry, str(row.get("result") or ""), mark, reason, work, row.get("coverage"),
                         stamp, cost, row.get("comments") or [])


def journal_coverage(name: str, provider: str, head: str, work: str) -> dict:
    journal = Path(work, f"{name}.journal.jsonl")
    if not journal.is_file():
        raise SessionError(msg("local_no_journal", name, journal))
    return agent_results.coverage(journal, Path(head), os.path.join(work, INPUT_DIR), f"{provider} {journal}")


def answer(entry: dict, text: str, provider: str, mark: str, reason: str, work: str, head: str, stamp: dict) -> dict:
    data = agent_results.json_object(text)
    structured = isinstance(data, dict) and "verdict" in data
    try:
        result, comments = agent_results.verdict(provider, Outcome(text=text, structured=data)) if structured \
            else (text, None)
        covered = journal_coverage(entry["id"], provider, head, work)
    except SessionError as error:
        return {"id": entry["id"], "state": "", "problem": str(error)}
    cost = session_cost.cost(os.path.join(work, f"{entry['id']}.journal.jsonl"))
    return format_result(entry, result, mark, reason, work, covered, stamp, cost, comments)


def format_result(entry: dict, text: str, mark: str, reason: str, work: str, covered: object, stamp: dict,
                  cost: dict, comments: list[dict] | None = None) -> dict:
    name = entry["id"]
    if not isinstance(covered, dict) or not agent_results.listed(covered.get("read")):
        return {"id": name, "state": "", "problem": msg("local_no_coverage", name)}
    with open(os.path.join(work, f"{name}.coverage.json"), "w", encoding="utf-8") as handle:
        json.dump(covered, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    for line in agent_results.coverage_report(covered):
        say(f"{name}: {line}")
    found = list(VERDICT.finditer(text))
    if not found or comments is None and (len(found) != 1 or text[found[-1].end():].strip()):
        return {"id": name, "state": "", "problem": msg("local_no_state", name)}

    last = found[-1]
    state = last.group(1).lower()
    body = VERDICT.sub("", text[:last.start()]).strip()
    if state == "changes" and not body:
        return {"id": name, "state": "", "problem": msg("local_changes_empty", name)}
    if comments is None:
        try:
            agent_results.check_body(name, body)
        except SessionError as error:
            return {"id": name, "state": "", "problem": str(error)}
    inline, loose = review_verdict.inline(comments)
    body = "\n".join(part for part in (forge.inline_fallback(body, inline).strip(), "\n".join(loose)) if part)
    normalized_body = text_data.inert(plain(body))
    if normalized_body != body:
        say(msg("local_chars_replaced", name))
    path = os.path.join(work, f"{name}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(verdict_body(entry, mark, reason, state, normalized_body, stamp, cost, work))
    return {"id": name, "state": state, "path": path}


def collect_native(args: argparse.Namespace) -> int:
    work = os.path.abspath(args.work)
    with open(os.path.join(work, "round.json"), encoding="utf-8") as handle:
        plan = json.load(handle)
    head = os.path.join(work, "head")
    base = os.path.join(head, BASE_CONFIG)
    if (str(plan["pr"]) != str(args.pr)
            or plan["head_sha"] != git("rev-parse", "HEAD")
            or git("rev-parse", "HEAD", repo=head) != plan["head_sha"]
            or git("rev-parse", "HEAD", repo=base) != plan["base_sha"]
            or snapshot(head) != plan.get("snapshot")
            or snapshot(base) != plan.get("base_snapshot")):

        raise RuntimeError(msg("local_diverged", args.pr))
    reader = base_reader(base)
    values = fallback_reader(reader, base)
    entries = {entry["id"]: entry for entry in reader.get_records("processes.review.steps", [], file=PIPELINE)}
    names = [item["id"] for item in plan["passes"]]
    if not names or len(names) != len(set(names)) or set(names) - entries.keys():
        raise RuntimeError(msg("local_unknown_passes", ", ".join(names)))
    results = []
    stamp = {"SHA": plan["head_sha"], "Provider": plan["provider"], "Task": plan.get("task_hash", ""),
             "Round": change_cost.round_of(int(args.pr), plan["head_sha"]), "Reason": plan.get("reason_key", "")}
    for name in names:
        response = os.path.join(work, f"{name}.response.md")
        try:
            with open(response, encoding="utf-8") as handle:
                result = answer(entries[name], handle.read(), plan["provider"], setting(values, "mark", ""),
                                plan["reason"], work, head, stamp)
            results.append(result)
        except OSError as error:
            results.append({"id": name, "state": "", "problem": str(error)})
    failed = report(results, work)
    if args.publish:
        publish(int(args.pr), results, setting(values, "label", ""))
    if failed or args.keep or not args.publish:
        say(msg("local_trees_kept", head))
    else:
        drop_tree(head, base)
    return failed


def foreign_work(args: argparse.Namespace) -> str:
    if not args.work or Path(ROOT).resolve() == admin_of(args.work).resolve():
        return ""
    from task_workspace import Workspace

    sessions = Workspace(ROOT).review(args.pr).resolve()
    return "" if Path(args.work).resolve().is_relative_to(sessions) else msg("local_work_outside", args.work, sessions)


def run(args: argparse.Namespace) -> int:
    problem = foreign_work(args) or bound(args.pr) or pushed()
    if problem:
        say_err(problem)
        return 1

    if git("status", "--porcelain"):
        say_err(msg("runtime_a_committed_clean_source_snapshot_is_required"))
        return 1
    if args.collect and Path(args.work, DELEGATION).exists():
        saved = json.loads(Path(args.work, DELEGATION).read_text())
        base_sha = saved["source"]["base_sha"]
        return delegated(args, args.work, delegate(args, args.work, base_sha, base_runner(base_sha), saved["base_ref"]))
    change = forge.change(int(args.pr))
    if not args.collect:
        base_ref = args.base or getattr(change, "base_sha", "") or BASE_BRANCH
        known = re.fullmatch(r"[a-f0-9]{40}", base_ref) and git(
            "rev-parse", "--verify", "--quiet", base_ref + "^{commit}", check=False)
        if not known and not fetch(base_ref.split("/", 1)[-1]):
            say_err(msg("local_base_stale", FETCH_TIMEOUT, base_ref))
            return 1
        base_sha = git("rev-parse", base_ref)
        runner = base_runner(base_sha)
        if args.work:
            work = os.path.abspath(args.work)
        else:
            from task_workspace import SESSIONS, Workspace

            workspace = Workspace(ROOT)
            exclude = workspace.common / "info" / "exclude"
            if SESSIONS + "/" not in (exclude.read_text(encoding="utf-8").split() if exclude.is_file() else []):
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n{SESSIONS}/\n")
            sessions = workspace.review(args.pr)
            os.makedirs(sessions, exist_ok=True)
            work = tempfile.mkdtemp(prefix=git("rev-parse", "--short=12", "HEAD") + "-", dir=sessions)
        os.makedirs(work, exist_ok=True)
        say(msg("local_workdir", work))
        if (not current_base(base_sha)
                and git("hash-object", os.path.join(ROOT, RUNNER)) != git("rev-parse", f"{base_sha}:{runner}")):
            proof_ref = BASE_BRANCH if re.fullmatch(r"[a-f0-9]{40}", base_ref) else base_ref
            return delegated(args, work, delegate(args, work, base_sha, runner, proof_ref))
    if change is None or change.head_sha != git("rev-parse", "HEAD") or not getattr(change, "base_sha", ""):
        say_err(msg("runtime_review_plan_is_stale_or_its_base_policy_is_missing"))
        return 1
    if args.collect:
        with open(os.path.join(args.work, "round.json"), encoding="utf-8") as handle:
            plan = json.load(handle)
        if (change.base_sha != plan.get("base_sha")
                or review_evidence.task_hash(int(args.pr)) != plan.get("task_hash")):
            say_err(msg("runtime_review_plan_is_stale_or_its_base_policy_is_missing"))
            return 1
        return collect_native(args)

    if base_sha != change.base_sha:
        say_err(msg("runtime_fetched_base_no_longer_matches_the_planned_snapshot"))
        return 1

    if args.execute and not os.path.exists(AGENT_RUN):
        say_err(msg("local_no_agent_run", AGENT_RUN))
        return 1

    head = os.path.join(work, "head")
    base = os.path.join(head, BASE_CONFIG)
    jobs: list = []
    try:
        prepare_tree(base_ref, head, base)
        return review(args, base_ref, work, head, base, jobs)
    finally:
        stop(jobs)
        if args.keep or args.dry_run:
            say(msg("local_trees_kept", head))
        else:
            drop_tree(head, base)


def review(args: argparse.Namespace, base_ref: str, work: str, head: str, base: str, jobs: list) -> int:
    reason = text_templates.choice("LocalReason", args.reason)
    reader = base_reader(base)
    entries = reader.get_records("processes.review.steps", [], file=PIPELINE)
    if not entries:
        say_err(msg("local_no_passes"))
        return 1

    wanted = args.passes or [entry.get("id") for entry in entries]
    unknown = [name for name in wanted if name not in [entry.get("id") for entry in entries]]
    if unknown:
        say_err(msg("local_unknown_passes", ', '.join(unknown)))
        return 1
    entries = [entry for entry in entries if entry.get("id") in wanted]

    values = fallback_reader(reader, base)
    mark = setting(values, "mark", "")
    missing = phrase(reader, "local-context-missing")
    prompt_path = setting(values, "prompt", ".harness/prompts/review/local.md")
    allowed = setting(values, "allowedTools", "Read,Grep,Glob")
    disallowed = denies(values)
    minutes = setting(values, "timeoutMinutes", 30)

    data = os.path.join(work, INPUT_DIR)
    context = ""
    if args.context:
        os.makedirs(data, exist_ok=True)
        context = os.path.join(data, CONTEXT_FILE)
        with open(args.context, encoding="utf-8") as source, open(context, "w", encoding="utf-8") as sink:
            sink.write(source.read())

    warn_drift(base)
    head_sha = git("rev-parse", "HEAD", repo=head)
    fork = git("merge-base", git("rev-parse", "HEAD", repo=base), head_sha, repo=head)
    review_inputs.write_inputs(head, data, fork, head_sha)
    say(msg("local_maps_built", data))

    original = (snapshot(head), snapshot(base), review_evidence.task_hash(int(args.pr)))
    kind = raise_passes(args, reason, entries, base, base_ref, work, head,
                        context, prompt_path, allowed, disallowed, jobs, missing, minutes, data)

    if args.context and not any(entry.get("context") for entry in entries):
        say_err(msg("local_context_unused"))

    if args.dry_run:
        plan = {"pr": str(args.pr), "head_sha": git("rev-parse", "HEAD", repo=head),
                "base_sha": git("rev-parse", "HEAD", repo=base), "head": head, "base": base,
                "snapshot": snapshot(head), "base_snapshot": snapshot(base),
                "task_hash": original[2],
                "reason": reason, "reason_key": args.reason, "label": setting(values, "label", ""), "mark": mark,
                "timeout_minutes": minutes, "provider": args.provider,
                "passes": [{"id": entry["id"],
                            "prompt": os.path.join(work, f"{entry['id']}.prompt.md"),
                            "response": os.path.join(work, f"{entry['id']}.response.md"),
                            "journal": os.path.join(work, f"{entry['id']}.journal.jsonl"),
                            "context": context if entry.get("context") else ""}
                           for entry in entries]}
        with open(os.path.join(work, "round.json"), "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return 0

    limit = time.time() + (float(minutes) + 1) * 60
    stamp = {"SHA": head_sha, "Provider": kind or args.provider, "Task": original[2],
             "Round": change_cost.round_of(int(args.pr), head_sha), "Reason": args.reason}
    results = [collect(job, mark, reason, work, limit, stamp) for job in jobs]
    if original != (snapshot(head), snapshot(base), review_evidence.task_hash(int(args.pr))):
        say_err(msg("runtime_review_source_base_or_policy_changed_during_analysis"))
        return 1
    failed = report(results, work)
    if args.publish:
        publish(int(args.pr), results, setting(values, "label", ""))
    return failed


def delegated(args: argparse.Namespace, work: str, code: int) -> int:
    if not args.publish or args.dry_run:
        return code
    saved = Path(work, "round.json")
    plan = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else {}
    names = [entry["id"] for entry in plan.get("passes", [])] or args.passes or [
        entry["id"] for entry in review_policy.passes()]
    head, provider = plan.get("head_sha") or git("rev-parse", "HEAD"), plan.get("provider") or args.provider
    task = plan.get("task_hash") or review_evidence.task_hash(int(args.pr))
    results = [{"id": name, "path": str(Path(work, name + ".md"))} for name in names]
    for result in results:
        path = Path(result["path"])
        result["state"] = path.is_file() and signed(path.read_text(encoding="utf-8"))
        if result["state"]:
            path.write_text(with_head(path.read_text(encoding="utf-8"), head, provider, task), encoding="utf-8")
    publish(int(args.pr), results, get("processes.review.fallback.label", "", file=PIPELINE))
    if not code and not args.keep and all(result["state"] for result in results):
        tree = os.path.join(work, "head")
        drop_tree(tree, os.path.join(tree, BASE_CONFIG))
    return code


def with_head(text: str, head: str, provider: str, task: str) -> str:
    if text_data.marked(text, "review/verdict"):
        return text
    lines = [line for line, present in ((f"SHA: {head}", review_policy.HEAD_LINE.search(text)),
                                        (f"Task: {task}", not task or review_policy.TASK_LINE.search(text)),
                                        (f"Provider: {provider}", re.search(r"^Provider: ", text, re.M)))
             if not present]
    if not lines:
        return text
    body, mark = text.rstrip(), next((mark for mark in signatures() if mark and text.rstrip().endswith(mark)), "")
    stamped = body[:len(body) - len(mark)].rstrip() + "\n" + "\n".join(lines) + "\n"
    return sign(stamped) if mark else stamped


def publish(number: int, results: list[dict], label: str) -> None:
    login = forge.login()

    def key(text: str) -> tuple[str, str, str]:
        return review_policy.headline(text), review_policy.head_of(text), review_policy.task_of(text)

    seen = {key(note.body) for note in forge.notes(number) if note.author == login and review_policy.head_of(note.body)}
    posted = 0
    for item in results:
        path = Path(item.get("path") or "")
        text = path.read_text(encoding="utf-8") if item.get("state", True) and path.is_file() else ""
        if text and key(text) not in seen:
            forge.comment(number, text)
            posted += 1
    if posted and label:
        forge.add_label(number, label)
    say(msg("local_published", posted, number))


def pass_prompt(text: str, base: str, prompt_path: str, base_ref: str, reason: str, context: str,
                missing: str, data: str) -> str:
    return (text.rstrip() + "\n\n" + local_block(base, prompt_path, base_ref, reason, context, missing)
            + "\n\n" + text_templates.prompt("review/local-inputs", {"LocalInputs": data}))


def raise_passes(args, reason, entries, base, base_ref, work, head,
                 context, prompt_path, allowed, disallowed, jobs: list, missing: str, minutes: int,
                 data: str) -> str:
    kind = provider(args.provider)
    for entry in entries:
        name = entry["id"]
        fields = emit_pass(base, name, str(args.pr), kind, data)
        entry = dict(entry, kind=fields["kind"], model=fields["model"], effort=fields["effort"])
        given = context if entry.get("context") else ""
        prompt = pass_prompt(fields["prompt"], base, prompt_path, base_ref, reason, given, missing, data)
        if args.dry_run:
            path = os.path.join(work, f"{name}.prompt.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(prompt)
            say(msg("local_prompt_built", name, len(prompt), path))
            continue
        say(msg("local_pass_start", name, entry['model'], entry['effort']))
        jobs.append(start(entry, prompt, head, allowed, disallowed, work, given, minutes, data))
    return kind


def report(results: list[dict], work: str) -> int:
    say(msg("local_summary"))
    failed = 0
    for item in results:
        if item.get("state"):
            verdict = Path(item["path"])
            verdict.write_text(sign(verdict.read_text(encoding="utf-8")), encoding="utf-8")
            print(f"  {item['id']}: {STATE_WORD.get(item['state'], item['state'])} - {item['path']}")
        else:
            failed = 1
            print(msg("local_no_verdict", item['id'], item.get('problem', '')), file=sys.stderr)
    print(msg("local_verdicts_at", work))
    return failed


def warn_drift(base: str) -> None:
    if not on_base(base):
        say_err(msg("local_script_not_on_base"))
        return
    paths = {RUNNER, os.path.relpath(os.path.abspath(__file__), ROOT)}
    for relative in sorted(paths):
        twin = Path(base, relative)
        if not twin.is_file() or twin.read_bytes() != Path(ROOT, relative).read_bytes():
            say_err(msg("local_script_drift"))
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальный круг ревью из сессии автора PR.")
    parser.add_argument("--pr", required=True, help="номер PR: он идет в промпт прохода")
    parser.add_argument("--reason", metavar="KEY",
                        help="ключ причины локального круга из записи LocalReason реестра: название уходит в вердикт")
    parser.add_argument("--pass", dest="passes", action="append", metavar="ID",
                        help="поднять только этот проход, повторяемый")
    parser.add_argument("--context", metavar="FILE", help="файл с постановкой и телом PR")
    parser.add_argument("--base", metavar="REF", help="база разбора: снимок базы PR у поставщика кода (base_sha), он же по умолчанию")
    parser.add_argument("--work", metavar="DIR", help="каталог круга из вывода подготовки: только внутри .sessions/review/<PR>/ основного клона")
    parser.add_argument("--provider", default="session", help="провайдер авторской сессии для подготовленного круга")
    parser.add_argument("--keep", action="store_true", help="оставить деревья круга после прогона")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="подготовить круг для агентов текущей сессии (по умолчанию)")
    mode.add_argument("--execute", action="store_true",
                      help="запустить сессии проходов через agent-run.py исполнителем модуля review")
    mode.add_argument("--collect", action="store_true", help="собрать ответы агентов подготовленного круга")
    parser.add_argument("--publish", action="store_true",
                        help="опубликовать вердикты комментариями, поставить метку круга и записать их для конвейера")
    args = parser.parse_args()
    args.dry_run = not args.execute and not args.collect
    if args.collect and (not args.work or args.passes or args.context or args.base or args.reason):
        parser.error(msg("local_collect_args"))
    if not args.collect and not args.reason:
        parser.error(msg("local_reason_required"))
    if args.reason:
        try:
            text_templates.choice("LocalReason", args.reason)
        except ValueError as error:
            parser.error(str(error))

    try:
        return run(args)
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        say_err(str(error))
        return 1
