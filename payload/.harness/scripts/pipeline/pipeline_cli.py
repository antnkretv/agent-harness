from __future__ import annotations

import process_validation
import review_round
from contextlib import nullcontext
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import shutil

from ci_common import BASE_BRANCH, ROOT, forge, tracker, msg
from pipeline_execution import Pipeline
from pipeline_state import Blocked, Pending, locked, stages
from task_workspace import Workspace


def main() -> int:
    if sys.argv[1:2] == ["next"]:
        import pipeline_next
        return pipeline_next.cli(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Execute and verify a declared pipeline using snapshot-bound runtime evidence.")
    parser.add_argument("command", choices=("plan", "run", "verify", "audit", "check-ready", "ready"))
    parser.add_argument("pipeline")
    parser.add_argument("--task", default="", help="task context file")
    parser.add_argument("--workdir", default="", help="source tree; policy and executable runner remain trusted")
    parser.add_argument("--report", default="", help="current-run CI audit artifact")
    parser.add_argument("--wait-minutes", type=float, default=0)
    parser.add_argument("--publish-context", default="")
    parser.add_argument("--issue", default="")
    parser.add_argument("--task-key", default="", help="branch slug of the task")
    parser.add_argument("--task-type", default="", help="Conventional Commits type of the future PR title")
    parser.add_argument("--change", type=int, default=0)
    parser.add_argument("--base", default="")
    parser.add_argument("--until", default="", help="process ID or process/step ID; change defaults to Ready, other compositions require an explicit stop")
    parser.add_argument("--ignore-check", action="append", default=[], help="active orchestrator context; cannot exclude a required check")
    parser.add_argument("--reviewer", default="", help="protected CI publisher login expected on review evidence")
    parser.add_argument("--required-check", action="append", default=[])
    parser.add_argument("--timeout-minutes", type=float, default=25)
    args = parser.parse_args()
    worktree = ROOT
    temporary = ""
    try:
        if args.command == "plan":
            problems = []
            process_validation.check_contract(problems)
            if problems:
                raise Blocked("\n".join(problems))
            print(json.dumps({"pipeline": args.pipeline, "steps": stages(args.pipeline)}, ensure_ascii=False,
                             indent=2))
            return 0
        if args.workdir:
            worktree = str(Path(args.workdir).resolve())
        elif args.command == "audit":
            change = forge.change(args.change)
            if not change:
                raise Blocked(msg('runtime_current_pr_is_unavailable'))
            temporary = tempfile.mkdtemp(prefix="pipeline-audit-")
            worktree = os.path.join(temporary, "head")
            review_round.checkout(worktree, {"change": change.number, "head": change.head_sha,
                "base": change.base, "base_sha": change.base_sha, "fetch": change.fetch_ref})
        data = None
        if args.command == "run" and args.pipeline == "change":
            workspace = Workspace(worktree)
            data = workspace.prepare(issue=args.issue, key=args.task_key, kind=args.task_type, base=args.base, change=args.change,
                                     resume_merged=True)
            if data:
                worktree = data["path"]
                args.base = data["base"]
        args.unallocated = data is None
        if not args.base:
            current = forge.change(args.change) if args.change else None
            if args.change and current is None:
                raise Blocked(msg("runtime_current_pr_is_unavailable"))
            args.base = "origin/" + current.base if current else BASE_BRANCH
        pipeline = Pipeline(args, worktree)
        if args.command == "audit":
            return pipeline.readiness.audit()
        if args.command == "check-ready":
            return pipeline.readiness.check_ready()
        if args.command == "ready":
            return pipeline.readiness.ready()
        guard = Workspace(worktree).pipeline_session() if data else nullcontext()
        with guard, locked(pipeline.work / "runner.lock"):
            pipeline.state = pipeline.journal.load()
            pipeline.records = pipeline.state["records"]
            result = pipeline.run()
        if args.command == "run" and args.change and forge.change(args.change).state == "merged":
            for outcome in Workspace(ROOT).reap():
                print(outcome, file=sys.stderr)
        return result
    except Pending as error:
        print(msg("pipeline_run_pending", error), file=sys.stderr)
        return 2
    except (RuntimeError, OSError, ValueError, KeyError, forge.ForgeError, tracker.TrackerError) as error:
        print(msg("pipeline_run_blocked", error), file=sys.stderr)
        return 1
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)
