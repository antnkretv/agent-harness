"""Сессия агента CI одним входом для claude-code, codex и cursor.

    ./.harness/hooks/python.cmd .harness/scripts/agent/agent-run.py --provider {claude-code,codex,cursor} --mode {review,agent,reply}
        --model M [--effort E] --prompt-file F --log L [--workdir D] [--add-dir D]...
        [--review-purpose {review,process-audit}] [--inputs D] [--allowed-tools T] [--disallowed-tools T]
        [--mcp-config F] [--timeout-minutes N]

review - только чтение: окружение сессии собирается из белого списка, итог - JSON по
review.schema.json рядом; --review-purpose process-audit требует тело аудита процесса. Прочитанное
review берет из журнала инструментов сессии: сессия без чтения файла каталога --workdir - отказ,
кроме --review-purpose process-audit.
--inputs - каталог входов круга (pr.diff, impact.md): с ним прочитанное сверяется с файлами диффа и
местами impact.md.
agent - правка, коммит и push: окружение задания CI целиком и forge.git_env().
--disallowed-tools вида Bash(<команда>:*) и Bash(<команда>) codex получает правилами execpolicy,
cursor - запретами Shell(<команда>) своей конфигурации; все три исполнителя - общим хуком запретов
.harness/hooks/agent-deny.py, который дописывает отказы в файл AGENT_DENIALS.

Лог --log - родной JSONL исполнителя, последней строкой - итоговая запись
{"type": "result", "subtype": "success"|"error", "is_error", "result", "comments", "coverage",
"duration_ms", "usage", "denials", "provider", "model", "effort"}; denials - отказы хука запретов, null, если хук ни разу не вызывался;
provider, model и effort - параметры запуска, по ним вердикт печатает строку модели. В review поле result - тело вердикта и строка `VERDICT: approve|changes`,
comments - построчные находки, coverage - журнал, прочитанное, ожидаемое и непрочитанное
({"journal", "read", "expected", "unread"}). Код возврата 0 только у успешной записи.

--mcp-config - {"mcpServers": {"<имя>": {"url": "https://...", "bearerTokenEnv": "<переменная>"}}}:
сессия получает серверы в форме своего исполнителя. Токен - переменная окружения, иначе слот CI с этим
именем (manifest.environ).
"""

from __future__ import annotations
from contextlib import nullcontext
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from ci_common import annotation, msg
from manifest import get_records
from agent_lifecycle import execute, on_signal
from agent_hook import DENIALS_ENV
import agent_links
from agent_models import Outcome, Session, SessionError, Stopped
from agent_provider import BINARIES, KINDS, SCHEMA, claude_command, codex_command, cursor_command, environment, identity, read_prompt, read_servers
from agent_results import codex_outcome, coverage, coverage_report, events, stream_outcome, verdict
from permission_policy import WRITE_TOOLS, declared_denies, deny_entries
from pipeline_state import Blocked
from task_workspace import Workspace
import session_cost
import text_templates

COMMANDS: dict[str, Callable[[Session], list[str]]] = {
    "claude-code": claude_command, "codex": codex_command, "cursor": cursor_command}

OUTCOMES: dict[str, Callable[[Session, Path, int], Outcome]] = {
    "claude-code": stream_outcome, "codex": codex_outcome, "cursor": stream_outcome}


def denials(ledger: Path) -> list[dict] | None:
    try:
        return events(ledger)
    except OSError:
        return None


def run(args: argparse.Namespace) -> int:
    log = Path(args.log).resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    started = time.monotonic()
    temp = Path(tempfile.mkdtemp(prefix="agent-run-"))
    outcome, text, comments, covered, ok = Outcome(), "", [], None, False
    session = None
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), on_signal)
    try:
        declared = get_records("policies.permissions.deny", [], file="methodology.json")
        if not declared:
            raise SessionError(msg("perms_policy_missing"))
        servers = read_servers(args.mcp_config)
        prompt = read_prompt(args.prompt_file)
        if args.mode == "review":
            contract = text_templates.prompt("stage/audit-output" if args.review_purpose == "process-audit"
                                             else "review/output")
            prompt += "\n\n" + contract + "\n\n" + SCHEMA.read_text(encoding="utf-8")
        env = environment(args, servers)
        if not shutil.which(BINARIES[args.provider], path=env.get("PATH")):
            raise SessionError(msg("agent_run_no_cli", BINARIES[args.provider], args.provider))
        workdir = Path(args.workdir or os.getcwd()).resolve()
        if args.detached:
            env["HARNESS_TASK_DETACHED"] = "1"
        workspace = Workspace(workdir) if args.mode == "agent" and not args.detached else None
        data = None
        if workspace:
            try:
                data = workspace.prepare(issue=args.issue, key=args.task_key, kind=args.task_type,
                                         base=args.base, change=args.change)
            except Blocked as error:
                if not args.read_only_fallback:
                    raise
                print(annotation("warning", "agent_run_read_only", error), flush=True)
                workspace.hooks()
                prompt += "\n\n" + text_templates.prompt("agent/read-only", {"ReadOnlyReason": error})
                args.mode = "reply"
                env = environment(args, servers)
        if data:
            workdir = Path(data["path"])
        if args.mode == "reply":
            args.disallowed_tools = ",".join(filter(None, [args.disallowed_tools, *WRITE_TOOLS]))
        args.disallowed_tools = ",".join(dict.fromkeys([*declared_denies(declared), *KINDS[args.provider]["denies"],
                                                        *deny_entries(args.disallowed_tools)]))
        session = Session(args, prompt, env, workdir, servers, temp)
        env[DENIALS_ENV] = str(session.denials)
        with (workspace.session(workdir) if data else nullcontext()) as held:
            if data:
                env["HARNESS_TASK_BRANCH"] = held["branch"]
            if not session.review:
                identity(env, session.workdir)
                try:
                    agent_links.ensure(session.workdir)
                except OSError as error:
                    raise SessionError(msg("agent_run_links_failed", error)) from None
            code = execute(session, COMMANDS[args.provider](session), log)
        outcome = OUTCOMES[args.provider](session, log, code)
        if not outcome.ok:
            raise SessionError(outcome.text.strip() or msg("agent_run_failed", args.provider, code))
        text, comments = (verdict(args.provider, outcome, purpose=args.review_purpose)
                          if args.mode == "review" else (outcome.text, []))
        if args.mode == "review":
            covered = coverage(log, session.workdir, args.inputs, f"{args.provider} {log}",
                               required=args.review_purpose != "process-audit")
            print("\n".join(coverage_report(covered)), flush=True)
        ok = True
    except Stopped as stopped:
        text = msg("agent_run_stopped", args.provider, stopped.signum)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        text = str(error)
    finally:
        refused = denials(session.denials) if session else None
        kept = False
        for undo in reversed(session.undo if session else []):
            try:
                undo()
            except OSError as error:
                kept = True
                print(annotation("warning", "agent_run_restore_failed", error, temp))
        if not kept:
            shutil.rmtree(temp, ignore_errors=True)
    if not ok:
        comments, covered = [], None
        print(text, file=sys.stderr)
    row = {"type": "result", "subtype": "success" if ok else "error", "is_error": not ok, "result": text,
           "comments": comments, "coverage": covered, "duration_ms": (time.monotonic() - started) * 1000,
           "usage": outcome.usage, "tiers": session_cost.session(str(log))["tiers"], "denials": refused,
           "provider": args.provider, "model": args.model, "effort": args.effort}
    with log.open("a", encoding="utf-8") as output:
        output.write("\n" + json.dumps(row, ensure_ascii=False) + "\n")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=tuple(BINARIES), required=True)
    parser.add_argument("--mode", choices=("review", "agent", "reply"), required=True)
    parser.add_argument("--review-purpose", choices=("review", "process-audit"), default="review")
    parser.add_argument("--inputs", default="", help="только review: каталог pr.diff и impact.md круга")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--issue", default="")
    parser.add_argument("--task-key", default="")
    parser.add_argument("--task-type", default="")
    parser.add_argument("--change", type=int, default=0)
    parser.add_argument("--base", default="")
    parser.add_argument("--detached", action="store_true", help="без выделения копии задачи: стадии вне run change")
    parser.add_argument("--read-only-fallback", action="store_true",
                        help="при отказе выделения копии вести сессию без правок вместо ошибки")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--allowed-tools", default="", help="только claude-code")
    parser.add_argument("--disallowed-tools", default="",
                        help="claude-code целиком; codex и cursor - записи Bash(...), cursor еще Edit и Write")
    parser.add_argument("--mcp-config", default="")
    parser.add_argument("--timeout-minutes", type=float, default=0)
    args = parser.parse_args()
    if args.mode != "review" and args.review_purpose != "review":
        parser.error(msg("agent_run_review_purpose_mode"))
    return run(args)
