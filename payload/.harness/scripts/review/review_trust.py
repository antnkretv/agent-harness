from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from ci_common import ROOT, SCRIPT_TREE as SCRIPTS, base_paths, base_read, base_tree, install_kit as installed_into, msg, named_scripts, placed_digest
from manifest import layer_manifest

BASE_CONFIG = ".review-base"
DELEGATION = ".review-delegation.json"

RUNNER = SCRIPTS + "review/review-local.py"

FETCH_TIMEOUT = 120


def say(message: str, stream=None) -> None:
    print(f"{os.path.splitext(os.path.basename(RUNNER))[0]}: {message}", file=stream or sys.stdout, flush=True)


def say_err(message: str) -> None:
    say(message, sys.stderr)


def git(*args: str, repo: str = ROOT, check: bool = True) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", repo, *args], capture_output=True, text=True,
                            encoding="utf-8", check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def base_runner(base: str) -> str:
    found = named_scripts(base_paths(base), PurePosixPath(RUNNER).name)
    if len(found) != 1:
        raise RuntimeError(msg("local_no_base_reader", RUNNER))
    return found[0]


def install_kit(tree: str | Path) -> None:
    done = installed_into(str(tree))
    if done is not None and done.returncode:
        raise RuntimeError(msg("workspace_install_failed", tree, (done.stderr or done.stdout).strip()))


def current_base(base: str) -> bool:
    raw = (base_read(base, ".harness/methodology.json") or b"").decode("utf-8", "replace")
    return bool(raw) and isinstance(json.loads(raw).get("processes"), dict)


def trusted_tools(base: str, runner: str) -> dict[str, bytes]:
    layer = runner.split("/", 1)[0] + "/"
    directory = layer + SCRIPTS.split("/", 1)[1]
    paths = [path for path in base_paths(base)
             if path.startswith((directory, *(layer + part for part in ("lib/", "vendor/", "templates/"))))
             or path in (layer + "messages.env", layer + "project/manifest.json", layer + "project/messages.env")
             or path.startswith(layer) and path.count("/") == 2 and path.endswith("/modules.json")
             or path.startswith(layer) and path.count("/") == 1 and path.endswith(".json")]
    if runner not in paths:
        raise RuntimeError(msg("local_no_base_reader", runner))
    return {path: base_read(base, path) for path in paths} if base_tree(base) else blobs(base, paths)


def blobs(base: str, paths: list[str]) -> dict[str, bytes]:
    selected = {}
    for row in git("ls-tree", "-rz", base, "--", *paths).split("\0"):
        if not row:
            continue
        meta, path = row.split("\t", 1)
        mode, kind, oid = meta.split()
        if mode not in ("100644", "100755") or kind != "blob":
            raise RuntimeError(msg("local_no_base_reader", path))
        selected[path] = oid
    result = subprocess.run(["git", "-C", ROOT, "cat-file", "--batch"],
                            input="".join(oid + "\n" for oid in selected.values()).encode("ascii"),
                            capture_output=True, check=True)
    stream = io.BytesIO(result.stdout)
    files = {}
    for path, oid in selected.items():
        actual, kind, size = stream.readline().split()
        content = stream.read(int(size))
        if (actual.decode("ascii") != oid or kind != b"blob" or len(content) != int(size)
                or stream.read(1) != b"\n"):
            raise RuntimeError(msg("local_no_base_reader", path))
        files[path] = content
    return files


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_packet(saved: dict, work: str) -> None:
    if (any(file_hash(Path(work, path)) != digest for path, digest in saved["packet"].items())
            or any(snapshot(tree) != digest for tree, digest in saved["trees"].items())):
        raise RuntimeError(msg("runtime_review_source_base_or_policy_changed_during_analysis"))


def source_state(pr: str, base: str, base_ref: str) -> dict:
    problem = pushed()
    if (problem or git("status", "--porcelain") or not fetch(base_ref.split("/", 1)[-1])
            or git("rev-parse", "FETCH_HEAD") != base):
        raise RuntimeError(problem or msg("runtime_review_source_base_or_policy_changed_during_analysis"))
    return {"source": os.path.realpath(ROOT), "pr": str(pr), "head_sha": git("rev-parse", "HEAD"),
            "base_sha": base, "snapshot": snapshot(ROOT),
            "origin": hashlib.sha256(git("remote", "get-url", "origin").encode()).hexdigest()}


def admin_of(work: str) -> Path:
    return Path(work, "control", ".review-admin")


def delegate(args: argparse.Namespace, work: str, base: str, runner: str, base_ref: str) -> int:
    work = os.path.abspath(work)
    receipt = Path(work, DELEGATION)
    admin = admin_of(work)
    control = admin.parent
    state = source_state(args.pr, base, base_ref)
    tools = trusted_tools(base, runner)
    if args.collect:
        saved = json.loads(receipt.read_text())
        if saved["source"] != state or saved["runner"] != runner:
            raise RuntimeError(msg("runtime_review_plan_is_stale_or_its_base_policy_is_missing"))
        verify_packet(saved, work)
        context = saved["context"]
        actual = {str(path.relative_to(admin)) for path in admin.rglob("*") if path.is_file()}
        if actual != tools.keys() or any(path.is_symlink() for path in admin.rglob("*")):
            raise RuntimeError(msg("runtime_review_source_base_or_policy_changed_during_analysis"))
        for path, content in tools.items():
            if (admin / path).is_symlink() or (admin / path).read_bytes() != content:
                raise RuntimeError(msg("runtime_review_source_base_or_policy_changed_during_analysis"))
    else:
        if control.exists() or control.is_symlink() or receipt.exists() or receipt.is_symlink():
            raise RuntimeError(msg("local_diverged", work))
        control.mkdir()
        git("init", "--quiet", repo=str(control))
        git("config", "core.hooksPath", "/dev/null", repo=str(control))
        git("fetch", "--no-tags", ROOT, state["head_sha"], base, repo=str(control))
        git("remote", "add", "origin", ROOT, repo=str(control))
        branch = git("branch", "--show-current")
        git("checkout", "--quiet", "--no-guess", "-b", branch, state["head_sha"], repo=str(control))
        if git("ls-files", ".review-admin", BASE_CONFIG, repo=str(control)):
            raise RuntimeError(msg("local_diverged", str(control)))
        with (control / ".git/info/exclude").open("a") as handle:
            handle.write("\n.review-admin/\n" + BASE_CONFIG + "/\n")
        for path, content in tools.items():
            target = admin / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        layer = admin / runner.split("/", 1)[0]
        prefix = json.loads((layer / layer_manifest(lambda name: (layer / name).is_file())).read_text())["project"]["gitConfigPrefix"]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", prefix):
            raise RuntimeError(msg("local_no_identity"))
        git("config", f"branch.{branch}.{prefix}-pr", str(args.pr), repo=str(control))
        context = {os.path.abspath(args.context): file_hash(args.context)} if args.context else {}
    if any(file_hash(path) != digest for path, digest in context.items()):
        raise RuntimeError(msg("runtime_review_source_base_or_policy_changed_during_analysis"))
    if git("status", "--porcelain", repo=str(control)) or git("rev-parse", "HEAD", repo=str(control)) != state["head_sha"]:
        raise RuntimeError(msg("runtime_a_committed_clean_source_snapshot_is_required"))
    if source_state(args.pr, base, base_ref) != state:
        raise RuntimeError(msg("runtime_review_source_base_or_policy_changed_during_analysis"))
    if args.execute:
        help_text = subprocess.run([sys.executable, str(admin / runner), "--help"], cwd=control,
                                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                                   capture_output=True, text=True, check=False)
        if help_text.returncode or not re.search(r"--execute(?=[,\s\]])", help_text.stdout):
            raise RuntimeError(msg("local_base_mode", "--execute"))
    command = [sys.executable, str(admin / runner), "--pr", str(args.pr), "--work", work,
               "--provider", args.provider]
    if args.collect:
        command += ["--collect", "--keep"]
    else:
        command += ["--base", base, "--reason", args.reason, "--execute" if args.execute else "--dry-run"]
        if args.keep:
            command.append("--keep")
        if args.context:
            command += ["--context", os.path.abspath(args.context)]
        for name in args.passes or []:
            command += ["--pass", name]
    result = subprocess.run(command, cwd=control, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), check=False)
    if (source_state(args.pr, base, base_ref) != state
            or any(file_hash(path) != digest for path, digest in context.items())):
        raise RuntimeError(msg("runtime_review_source_base_or_policy_changed_during_analysis"))
    if args.collect:
        verify_packet(saved, work)
    if result.returncode == 0 and args.dry_run:
        plan = json.loads(Path(work, "round.json").read_text())
        head, policy = Path(work, "head"), Path(work, "head", BASE_CONFIG)
        if (str(plan["pr"]) != str(args.pr) or plan["head_sha"] != state["head_sha"]
                or plan["base_sha"] != base or Path(plan["head"]) != head or Path(plan["base"]) != policy
                or git("rev-parse", "HEAD", repo=str(head)) != state["head_sha"]
                or git("rev-parse", "HEAD", repo=str(policy)) != base):
            raise RuntimeError(msg("runtime_review_plan_is_stale_or_its_base_policy_is_missing"))
        packet = {path: file_hash(Path(work, path)) for path in ("round.json", "control/.git/config")}
        for entry in plan["passes"]:
            for key in ("prompt", "context"):
                if entry.get(key):
                    path = Path(entry[key])
                    if not path.is_relative_to(work):
                        raise RuntimeError(msg("local_diverged", str(path)))
                    packet[str(path.relative_to(work))] = file_hash(path)
        receipt.write_text(json.dumps({"source": state, "runner": runner, "base_ref": base_ref,
                                       "packet": packet, "context": context,
                                       "trees": {str(tree): snapshot(str(tree)) for tree in (head, policy)}},
                                      ensure_ascii=False, indent=2) + "\n")
    return result.returncode


def fetch(ref: str) -> bool:
    try:
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", ROOT, "fetch", "origin", ref],
                              capture_output=True, text=True, encoding="utf-8",
                              check=False, timeout=FETCH_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def pushed() -> str:
    branch = git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    if not branch or branch == "HEAD":
        return msg("local_not_on_branch")
    if not fetch(branch):
        return msg("local_origin_silent", branch, FETCH_TIMEOUT)
    if git("rev-parse", "HEAD") != git("rev-parse", "FETCH_HEAD", check=False):
        return msg("local_diverged", branch)
    return ""


def snapshot(repo: str) -> str:
    digest = hashlib.sha256()
    digest.update(git("rev-parse", "HEAD", repo=repo).encode())
    digest.update(git("diff", "--binary", "HEAD", repo=repo).encode())
    for path in git("ls-files", "--others", "--exclude-standard", repo=repo).splitlines():
        if path.startswith(BASE_CONFIG + "/"):
            continue
        full = os.path.join(repo, path)
        digest.update(path.encode())
        if os.path.islink(full):
            digest.update(os.readlink(full).encode())
        elif os.path.isfile(full):
            with open(full, "rb") as handle:
                digest.update(handle.read())
    digest.update(placed_digest(repo))
    return digest.hexdigest()
