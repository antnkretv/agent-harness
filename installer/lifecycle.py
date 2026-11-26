from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from .configure import report, settle
from .install import apply
from .render import revision
from .lock_install import archive_build, loader
from .verify import status
from .resources import LOCK, PROJECT, Refusal, git, kit_root, load_json, msg, read_lock, relocated, repository, say, sha256

TASK_WORKTREE = ".harness/scripts/pipeline/task-worktree.py"
UPGRADE_CHANGE = ".harness/scripts/pipeline/kit-upgrade.py"
UPGRADE_KEY = "harness-update"


def init(args: argparse.Namespace) -> int:
    root = repository(args.target)
    if (root / LOCK).is_file():
        raise Refusal(msg("init_installed", LOCK))
    with relocated(root, temporary=True):
        settled = settle(args, root)
    pending, problems, _ = settled
    if pending is None and not problems:
        kit = own_kit()
        archive = Path(args.archive).resolve() if args.archive else released(kit, kit["version"])
        try:
            with contextlib.redirect_stdout(sys.stderr):
                code = apply(argparse.Namespace(target=args.target, dry_run=False, archive=str(archive), source=None))
        finally:
            if not args.archive:
                archive.unlink(missing_ok=True)
        if code:
            return code
    return report(args, settled)


def upgrade(args: argparse.Namespace) -> int:
    root = repository(args.target)
    installed = read_lock(root).get("kit", {})
    if not installed.get("sha256"):
        raise Refusal(msg("upgrade_unpinned", LOCK))
    kit = {**own_kit(), **{key: installed[key] for key in ("name", "repository") if installed.get(key)}}
    given = args.new_archive
    if given:
        archive = Path(given).resolve()
    elif args.commit:
        archive = committed(kit["repository"], args.commit)
    else:
        archive = released(kit, args.version or latest(kit["repository"]))
    try:
        return upgraded(args, root, installed, kit["repository"], archive)
    finally:
        if args.commit:
            shutil.rmtree(archive.parent, ignore_errors=True)
        elif not given:
            archive.unlink(missing_ok=True)


def upgraded(args: argparse.Namespace, root: Path, installed: dict, source: str, archive: Path) -> int:
    sha, shipped, tree = archive_build(archive)
    previous, version = label(installed), label(shipped)
    if tree == read_lock(root)["kit"].get("tree"):
        say("upgrade_current", previous, sha[:12])
        return 0
    ah = loader()
    home = ah.CACHE / sha
    if not ah.genuine(home, tree):
        ah.unpacked(archive, home, tree)
    new = [sys.executable, "-B", str(home / "ah.py")]
    work, branch, base = root, "", ""
    if not args.no_pr:
        base = args.base or load_json(root / PROJECT, msg("no_project", root / PROJECT)).get("git", {}).get("baseBranch", "develop")
        started = run([sys.executable, str(root / TASK_WORKTREE), "start", "--root", str(root), "--base", f"origin/{base}",
                       "--type", "chore", "--key", UPGRADE_KEY], root)
        found = json.loads(started.stdout)
        work, branch = Path(found["path"]), found["branch"]
    if read_lock(work)["kit"].get("tree") != tree:
        asked = json.loads(run(new + ["configure", "--target", str(work), "--next"], work).stdout)
        if not asked.get("done") or asked.get("problems"):
            print(asked.get("show", ""))
            raise Refusal(msg("upgrade_questions", home / "ah.py", work))
        say("upgrade_overwrites")
        status(argparse.Namespace(target=str(work)))
        relinked = relink(work, source, revision(installed), revision(shipped))
        run(new + ["apply", "--target", str(work), "--archive", str(archive), "--source", ""], work, show=True)
        if args.no_pr:
            say("upgrade_done", previous, version, relinked, home / "ah.py")
            return 0
    listed = run(["git", "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all"], work).stdout
    changed = [line[3:] for line in listed.split("\0") if line[1:2] not in ("", " ")]
    title = msg("upgrade_title", version)
    if changed:
        git(work, "add", "--", *changed)
    if git(work, "diff", "--cached", "--name-only"):
        run(new + ["verify", "--target", str(work), "--no-gates"], work, show=True)
        git(work, "commit", "-q", "-m", title)
    if git(work, "rev-parse", "HEAD") != git(work, "rev-parse", f"origin/{branch}", check=False):
        git(work, "push", "-q", "-u", "origin", branch)
    files = git(work, "diff", "--name-only", f"origin/{base}...HEAD").splitlines()
    opened = run([sys.executable, str(work / UPGRADE_CHANGE), "--head", branch, "--base", base, "--title", title,
                  "--previous", previous, "--version", version, "--sha", sha, "--files", str(len(files))], work)
    say("upgrade_opened", opened.stdout.strip(), work)
    return 0


def own_kit() -> dict:
    return load_json(kit_root() / "kit.json", msg("no_kit", kit_root()))


def latest(source: str) -> str:
    address = urllib.parse.urlsplit(source)
    if address.hostname != "github.com":
        raise Refusal(msg("upgrade_version_needed", source))
    request = urllib.request.Request(f"https://api.github.com/repos{address.path.removesuffix('.git')}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)["tag_name"].removeprefix("v")
    except (OSError, ValueError, KeyError) as error:
        raise Refusal(msg("upgrade_latest_failed", source, error)) from None


def released(kit: dict, version: str) -> Path:
    ah, pinned = loader(), {**kit, "version": version}
    url = ah.release(pinned) + ".sha256"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            published = response.read().decode("utf-8").split()[0]
    except (OSError, IndexError) as error:
        raise Refusal(msg("upgrade_download_failed", url, error)) from None
    try:
        archive = ah.downloaded(pinned)
    except OSError as error:
        raise Refusal(msg("upgrade_download_failed", ah.release(pinned), error)) from None
    if sha256(archive.read_bytes()) != published:
        archive.unlink()
        raise Refusal(msg("upgrade_sha_mismatch", ah.release(pinned), published))
    return archive


def label(kit: dict) -> str:
    return f"{kit.get('version', '')}@{kit['commit'][:12]}" if kit.get("commit") else kit.get("version", "")


def committed(repository: str, ref: str) -> Path:
    ah = loader()
    try:
        source = ah.fetched(repository, ref)
    except OSError as error:
        raise Refusal(msg("upgrade_fetch_failed", ref, repository, error)) from None
    try:
        return ah.built(source, True)
    finally:
        shutil.rmtree(source, ignore_errors=True)


def relink(root: Path, source: str, old: str, new: str) -> int:
    if old == new:
        return 0
    before, after = f"{source}/blob/{old}/", f"{source}/blob/{new}/"
    paths = [path for path in git(root, "grep", "-lzF", "-e", before, check=False).split("\0") if path]
    for path in paths:
        with open(root / path, encoding="utf-8", newline="") as file:
            text = file.read()
        with open(root / path, "w", encoding="utf-8", newline="") as file:
            file.write(text.replace(before, after))
    return len(paths)


def run(command: list[str], cwd: Path, show: bool = False) -> subprocess.CompletedProcess:
    done = subprocess.run(command, cwd=cwd, capture_output=not show, text=True, encoding="utf-8",
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if done.returncode:
        raise Refusal(msg("upgrade_step_failed", " ".join(command[1:4]), (done.stderr or done.stdout or "").strip()[-2000:]))
    return done
