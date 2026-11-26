#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401


def main() -> int:
    import manifest

    parser = argparse.ArgumentParser(description="Allocate, verify and retire task worktrees before authoring sessions.")
    parser.add_argument("command", choices=("start", "check", "ci", "cleanup", "await-merge", "refs", "relocate",
                                                "handoff"))
    parser.add_argument("--root", default=manifest.root())
    parser.add_argument("--issue", default="")
    parser.add_argument("--key", default="", help="branch slug")
    parser.add_argument("--type", default="", help="Conventional Commits type of the future PR title")
    parser.add_argument("--branch", default="", help="existing registered branch for relocate")
    parser.add_argument("--base", default="")
    parser.add_argument("--change", type=int, default=0)
    parser.add_argument("--message", default="")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--print-base", action="store_true")
    parser.add_argument("--phase", default="")
    parser.add_argument("--session", default="")
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    manifest.set_root(str(root))
    from ci_common import BASE_BRANCH, msg
    from pipeline_state import Blocked
    from authorship import session_agent
    from task_workspace import Workspace, canonical, check_ci, check_local, git, zero

    try:
        if args.command == "start":
            workspace = Workspace(root)
            if args.session:
                from pipeline_next import attach, entry_base
                args.base = entry_base(root, args.session, args.base, workspace.find(args.issue, args.key, args.type, args.base))
            data = workspace.start(issue=args.issue, key=args.key, kind=args.type, base=args.base, change=args.change)
            if args.session:
                attach(root, args.session, data)
            print(json.dumps(data, ensure_ascii=False))
        elif args.command == "relocate":
            if args.branch and (args.issue or args.key):
                raise Blocked(msg("workspace_identity_required"))
            workspace = Workspace(root)
            branch = args.branch or workspace.find(args.issue, args.key, args.type)
            if not branch:
                raise Blocked(msg("workspace_identity_required"))
            print(json.dumps(workspace.relocate(branch), ensure_ascii=False))
        elif args.command == "handoff":
            print(json.dumps(Workspace(root).handoff(), ensure_ascii=False))
        elif args.command == "check":
            lines = sys.stdin.read().split("\n") if args.push else []
            lines = [line.split() for line in lines if line.strip()]
            data = check_local(root, args.message)
            if args.print_base and data:
                print(data["base"])
            current = git(root, "branch", "--show-current") if lines else ""
            held = data or os.environ.get("HARNESS_TASK_DETACHED")
            if lines and (held and (canonical(current) or os.environ.get("HARNESS_TASK_BRANCH")) or session_agent()):
                target, sha = current, git(root, "rev-parse", "HEAD")
                for local, local_sha, remote, remote_sha in lines:
                    if remote != "refs/heads/" + target or local_sha != sha:
                        raise Blocked(msg("workspace_branch_invalid", remote) if data
                                      else msg("workspace_push_foreign", target, remote))
        elif args.command == "ci":
            check_ci(root, args.base or BASE_BRANCH, os.environ.get("PR_BRANCH") or git(root, "branch", "--show-current"))
        elif args.command == "await-merge":
            from pipeline_next import await_merge
            return await_merge(root, args.change, args.detach)
        elif args.command == "cleanup":
            workspace = Workspace(root)
            print(json.dumps(workspace.cleanup(args.change), ensure_ascii=False) if args.change else "\n".join(workspace.reap()))
        elif args.command == "refs" and args.phase == "prepared" and os.environ.get("HARNESS_TASK_BRANCH"):
            allowed = "refs/heads/" + os.environ["HARNESS_TASK_BRANCH"]
            for line in sys.stdin:
                old, new, ref = line.split()
                if (ref == allowed and zero(new) or ref.startswith("refs/")
                        and not ref.startswith(("refs/remotes/", "refs/tags/")) and ref != allowed):
                    raise Blocked(msg("workspace_branch_invalid", ref))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
