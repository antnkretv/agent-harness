from __future__ import annotations

from contextlib import contextmanager, suppress
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from ci_common import forge, get, install_kit, msg, record_files, release_flow, tracker
from authorship import agent_commit, session_agent
from manifest import extensions
from tracker import COMMIT_TYPES, branch_name, slug_of, valid_slug
from pipeline_state import Blocked, digest, locked, write_json

SESSIONS = ".sessions"
SETTINGS = ".harness/settings.env"

def zero(sha: str) -> bool:
    return set(sha) == {"0"}


def dirty(path: Path) -> bool:
    return bool(git(path, "status", "--porcelain", "--untracked-files=all"))


def kit_only(path: Path, entry: Path, placed: set[str]) -> bool:
    name = entry.as_posix().rstrip("/")
    if name in placed:
        return True
    files = [Path(top, file).relative_to(path).as_posix() for top, _, names in os.walk(path / name)
             if "__pycache__" not in Path(top).parts for file in names]
    return bool(files) and all(file in placed for file in files)


def hooked() -> bool:
    return "commit-msg" in extensions("githooks")


REMOTE = frozenset({"fetch", "push", "ls-remote", "worktree", "merge", "merge-tree", "bundle"})
MOUNT_POINT = 0xA0000003


@lru_cache(maxsize=1)
def authorized() -> dict | None:
    return forge.git_env() if forge.name and any(os.environ.get(name) for name in forge.token_names) else None


def git(root: Path, *args: str, optional: bool = False) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, encoding="utf-8", check=False,
                            env=authorized() if args[0] in REMOTE else None)
    if result.returncode and not optional:
        raise Blocked(msg("workspace_git_failed", " ".join(args), result.stderr.strip()))
    return "" if result.returncode else result.stdout.rstrip("\n")


def branch_for(commit_type: str, slug: str, issue: str = "", base: str = "") -> str:
    if commit_type not in COMMIT_TYPES:
        raise Blocked(msg("workspace_type_invalid", commit_type or msg("value_empty"), ", ".join(COMMIT_TYPES)))
    if not valid_slug(slug):
        raise Blocked(msg("workspace_key_invalid", slug))
    if issue:
        return branch_name(commit_type, slug, issue, base)
    name = tracker.issueless_branch(commit_type, slug, base)
    if not name:
        raise Blocked(msg("workspace_key_ambiguous", slug))
    return name


def only(branches: set[str]) -> str:
    if len(branches) > 1:
        raise Blocked(msg("workspace_change_mismatch", ", ".join(sorted(branches))))
    return next(iter(branches), "")


def commits(root: Path, revisions: str) -> list[tuple[str, int, str, list[str]]]:
    found = []
    fields = git(root, "log", "--no-merges",
                 "--format=%H%x00%ct%x00%(trailers:key=Task-Branch,valueonly,unfold)%x00%B%x00", revisions).split("\0")
    for offset in range(0, len(fields) - 1, 4):
        sha, timestamp, trailers, body = fields[offset:offset + 4]
        found.append((sha.lstrip("\n"), int(timestamp), body, trailers.splitlines()))
    return found


def canonical(branch: str) -> bool:
    return tracker.branch_parts(branch) is not None


def named(branch: str, issue: str = "", key: str = "", kind: str = "", base: str = "") -> bool:
    if issue:
        return tracker.branch_issue(branch) == issue
    if not key:
        return True
    return branch == tracker.issueless_branch(kind, key, base) if kind else tracker.branch_parts(branch) == ("", key)


def issue_type(issue) -> str:
    found = [get(f"policies.issues.types.{label}.commit", "", "methodology.json") for label in issue.labels]
    return next((value for value in found if value), "fix")


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.common = Path(git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        self.directory = self.common / "harness" / "tasks"
        self.reviews = self.common.parent / SESSIONS / "review"
        self.directory.mkdir(parents=True, exist_ok=True)

    def review(self, change: object) -> Path:
        return self.reviews / str(change)

    def paths(self, branch: str) -> tuple[Path, Path]:
        git(self.root, "check-ref-format", "refs/heads/" + branch)
        name = digest(branch)
        return (self.directory / (name + ".json"),
                self.common.parent.parent / ".harness-worktrees" / digest(os.path.normcase(str(self.common)))[:16] / name[:16])

    def location(self, data: dict) -> Path:
        branch = data["branch"]
        _, destination = self.paths(branch)
        path = Path(data.get("path", ""))
        if path.resolve() == path:
            if path == destination or self.registered(path):
                return path
            if not path.exists():
                if (data.get("phase") == "relocating" and data.get("relocationHead")
                        and self.attached(destination, branch, data["relocationHead"])):
                    return path
                bundle = Path(data.get("bundle", ""))
                if (data.get("phase") in ("retiring", "retired") and data.get("head") and data.get("merge")
                        and bundle.is_file() and bundle.resolve() == bundle and bundle.is_relative_to(self.directory)):
                    git(self.root, "bundle", "verify", str(bundle))
                    if data["head"] + " refs/heads/" + branch in git(self.root, "bundle", "list-heads", str(bundle)).splitlines():
                        return path
        raise Blocked(msg("workspace_record_invalid", branch))

    def primary(self, path: Path) -> bool:
        directory = git(path, "rev-parse", "--path-format=absolute", "--git-dir", optional=True) if path.exists() else ""
        return bool(directory) and Path(directory).resolve() == self.common

    def handoff(self) -> dict:
        branch = git(self.root, "branch", "--show-current")
        if not branch:
            raise Blocked(msg("workspace_detached"))
        record, _ = self.paths(branch)
        with (locked(self.directory / "allocation.lock"), locked(record.with_suffix(".pipeline.lock")),
              locked(record.with_suffix(".lock"))):
            data = self.read(branch)
            previous = self.location(data)
            head = git(self.root, "rev-parse", "HEAD")
            if (data.get("phase") != "active" or not data.get("allocationHead")
                    or not self.attached(self.root, branch, head)):
                raise Blocked(msg("workspace_record_invalid", branch))
            if previous != self.root and (git(previous, "branch", "--show-current") or dirty(previous)
                                          or Workspace(previous).common != self.common
                                          or git(previous, "rev-parse", "HEAD") != head):
                raise Blocked(msg("workspace_record_invalid", branch))
            data["path"] = str(self.root)
            self.save(data)
            return self.validate(self.root)

    def relocate(self, branch: str) -> dict:
        record, destination = self.paths(branch)
        with (locked(self.directory / "allocation.lock"), locked(record.with_suffix(".pipeline.lock")),
              locked(record.with_suffix(".lock"))):
            data = self.read(branch)
            path = self.location(data)
            if self.primary(path):
                raise Blocked(msg("workspace_current", path))
            if path == destination and data.get("phase") == "active":
                if self.validate(path)["branch"] != branch:
                    raise Blocked(msg("workspace_record_invalid", branch))
                return data
            if (os.environ.get("HARNESS_TASK_BRANCH") or self.root.is_relative_to(path)
                    or Path.cwd().resolve().is_relative_to(path)):
                raise Blocked(msg("workspace_current", path))
            if data.get("phase") not in ("active", "relocating"):
                raise Blocked(msg("workspace_record_invalid", branch))
            if destination.resolve() != destination:
                raise Blocked(msg("workspace_collision", branch, destination))
            if data.get("phase") == "active":
                self.validate(path)
                head = git(path, "rev-parse", "HEAD")
                if not self.attached(path, branch, head):
                    raise Blocked(msg("workspace_record_invalid", branch))
                if destination.exists() or destination.is_symlink() or self.registered(destination):
                    raise Blocked(msg("workspace_collision", branch, destination))
                data.update(phase="relocating", relocationHead=head)
                self.save(data)
            head = data["relocationHead"]
            if path.exists():
                if (not self.attached(path, branch, head) or destination.exists()
                        or destination.is_symlink() or self.registered(destination)):
                    raise Blocked(msg("workspace_collision", branch, destination))
                destination.parent.mkdir(parents=True, exist_ok=True)
                git(self.root, "worktree", "move", str(path), str(destination))
            if not self.attached(destination, branch, head) or Workspace(destination).common != self.common:
                raise Blocked(msg("workspace_record_invalid", branch))
            data.update(path=str(destination), phase="active")
            del data["relocationHead"]
            self.save(data)
            return data

    def worktrees(self) -> list[dict]:
        return [dict(field.partition(" ")[::2] for field in record.split("\0") if field)
                for record in git(self.root, "worktree", "list", "--porcelain", "-z").split("\0\0")]

    def attached(self, path: Path, branch: str, head: str) -> bool:
        registered = self.registered(path)
        return ("locked" not in registered and registered.get("branch") == "refs/heads/" + branch
                and registered.get("HEAD") == head)

    def gone(self, path: Path) -> bool:
        return not path.exists() and not path.is_symlink() and not self.registered(path)

    def registered(self, path: Path) -> dict:
        return next((fields for fields in self.worktrees()
                     if fields.get("worktree") and Path(fields["worktree"]).resolve() == path.resolve()), {})

    def follow(self, data: dict, pull) -> None:
        base = "origin/" + pull.base
        if data["base"] != base and data.get("change") in (0, pull.number):
            base_sha = data["baseSha"]
            if data["base"] != "refs/heads/" + pull.base:
                base_sha = git(self.root, "rev-parse", "--verify", "--quiet", base + "^{commit}",
                               optional=True) or base_sha
            data.update(base=base, baseSha=base_sha)
            self.save(data)

    def unbound(self, data: dict, change: int, *, release: bool = True) -> bool:
        number = data.get("change")
        if number in (0, change):
            return True
        pull = forge.change(number) if forge.name else None
        if release:
            return self.released(data, pull)
        return bool(pull and pull.state == "closed" and pull.head == data["branch"])

    def released(self, data: dict, pull) -> bool:
        if not pull or pull.state != "closed" or pull.head != data["branch"]:
            return False
        data["change"] = 0
        self.save(data)
        return True

    def owned(self, branch: str, base_sha: str, head: str) -> bool:
        return all(marks == [branch] for _, _, _, marks in commits(self.root, base_sha + ".." + head))

    def recover(self, data: dict, path: Path) -> None:
        branch, expected = data["branch"], data["allocationHead"]
        if path != self.location(data) or path.is_symlink():
            raise Blocked(msg("workspace_record_invalid", branch))
        if not path.exists():
            if self.registered(path):
                if not self.attached(path, branch, expected):
                    raise Blocked(msg("workspace_record_invalid", branch))
                git(self.root, "worktree", "remove", "--force", str(path))
            current = git(self.root, "rev-parse", "--verify", "refs/heads/" + branch, optional=True)
            if current and current != expected:
                raise Blocked(msg("workspace_head_changed", branch))
            if current:
                git(self.root, "worktree", "add", str(path), branch)
            else:
                git(self.root, "worktree", "add", "-b", branch, str(path), expected)
        if not self.attached(path, branch, expected) or git(path, "rev-parse", "HEAD") != expected or dirty(path):
            raise Blocked(msg("workspace_record_invalid", branch))
        self.placed(path)
        self.hooks()
        self.settings(path)
        data["phase"] = "active"
        self.save(data)

    def settings(self, path: Path) -> None:
        source, target = self.common.parent / SETTINGS, path / SETTINGS
        if (not source.is_file() or not target.parent.is_dir() or target.exists() or target.is_symlink()
                or not git(path, "check-ignore", "--", SETTINGS, optional=True)):
            return
        try:
            target.symlink_to(source)
        except OSError as error:
            shutil.copyfile(source, target)
            print(msg("workspace_settings_copied", target, error), file=sys.stderr)

    def hooks(self) -> None:
        if hooked() and not git(self.root, "config", "--get", "core.hooksPath", optional=True):
            git(self.root, "config", "core.hooksPath", ".githooks")

    def records(self) -> list[dict]:
        rows = []
        for path in sorted(self.directory.glob("*.json")):
            with suppress(FileNotFoundError):
                rows.append(json.loads(path.read_text(encoding="utf-8")))
        return rows

    def load(self, branch: str) -> dict:
        record, _ = self.paths(branch)
        if not record.is_file():
            raise Blocked(msg("workspace_record_invalid", branch))
        data = json.loads(record.read_text(encoding="utf-8"))
        if data.get("branch") != branch:
            raise Blocked(msg("workspace_record_invalid", branch))
        return data

    def read(self, branch: str) -> dict:
        data = self.load(branch)
        self.location(data)
        return data

    def save(self, data: dict) -> None:
        write_json(self.paths(data["branch"])[0], data)
        forge.remember_change(data["branch"], data.get("change") or 0, data.get("issue") or "")

    def validate(self, path: Path, *, phases: tuple = ("active",)) -> dict:
        branch = git(path, "branch", "--show-current")
        if not branch:
            raise Blocked(msg("workspace_detached"))
        data = self.read(branch)
        if (data.get("phase", "active") not in phases
                or Path(data["path"]).resolve() != path.resolve()):
            raise Blocked(msg("workspace_record_invalid", branch))
        if path.resolve() != self.root and Workspace(path).common != self.common:
            raise Blocked(msg("workspace_record_invalid", branch))
        return data

    def open_changes(self, branch: str) -> list:
        if not forge.name:
            return []
        found = [forge.change(number) for number in forge.open_changes(branch)]
        if any(pull is None for pull in found):
            raise Blocked(msg("workspace_change_mismatch", branch))
        return [pull for pull in found if pull.head == branch and pull.state == "open"]

    def finish(self, change: int) -> None:
        branch = git(self.root, "branch", "--show-current")
        if not branch or not self.paths(branch)[0].is_file():
            return
        with locked(self.directory / "allocation.lock"):
            data = self.validate(self.root)
            if data.get("change") != change:
                raise Blocked(msg("workspace_change_mismatch", data["branch"]))
            data["afterMerge"] = True
            self.save(data)

    def find(self, issue: str = "", key: str = "", kind: str = "", base: str = "") -> str:
        if not (issue or key):
            return ""
        return only({data["branch"] for data in self.records()
                     if data.get("phase") != "retired" and named(data["branch"], issue, key, kind, base or data["base"])})

    def prepare(self, *, issue: str = "", key: str = "", kind: str = "", base: str = "", change: int = 0,
                resume_merged: bool = False) -> dict | None:
        pull = forge.change(change) if change and forge.name else None
        with locked(self.directory / "allocation.lock"):
            if pull and pull.state == "merged" and not self.paths(pull.head)[0].is_file():
                return None
            if resume_merged and pull and pull.state == "merged":
                data = self.read(pull.head)
                if (data.get("change") != change or base and base != "origin/" + pull.base
                        or not named(pull.head, issue, key, kind, "origin/" + pull.base)):
                    raise Blocked(msg("workspace_change_mismatch", pull.head))
                self.validate(Path(data["path"]))
                self.follow(data, pull)
                return data
            branch = git(self.root, "branch", "--show-current")
            record = self.paths(branch)[0] if branch else None
            if record and record.is_file():
                data = self.validate(self.root)
                if (pull and (pull.state != "open" or pull.head != branch or getattr(pull, "cross_repo", False))
                        or base and base != ("origin/" + pull.base if pull else data["base"])
                        or not named(branch, issue, key, kind, base or data["base"])
                        or change and (not pull or not self.unbound(data, change, release=False))):
                    raise Blocked(msg("workspace_change_mismatch", branch))
                with locked(record.with_suffix(".lock")):
                    if git(self.root, "remote", "get-url", "origin", optional=True):
                        git(self.root, "fetch", "--prune", "origin")
                        self.sync_head(branch, self.root, pull)
                    if pull:
                        self.unbound(data, change)
                        self.follow(data, pull)
                    return data
        return self.start(issue=issue, key=key, kind=kind, base=base, change=change)

    def bind(self, change: int) -> None:
        branch = git(self.root, "branch", "--show-current")
        with locked(self.directory / "allocation.lock"):
            data = self.validate(self.root)
            pull = forge.change(change)
            if not pull or pull.state != "open" or pull.head != branch or not self.unbound(data, change):
                raise Blocked(msg("workspace_change_mismatch", branch))
            self.attach(data, pull)

    def adopt(self, pull) -> None:
        if pull.state != "open":
            return
        with locked(self.directory / "allocation.lock"):
            if not self.paths(pull.head)[0].is_file():
                return
            data = self.load(pull.head)
            if data.get("phase", "active") == "active" and not data.get("change"):
                self.attach(data, pull)

    def attach(self, data: dict, pull) -> None:
        data["change"] = pull.number
        self.follow(data, pull)
        self.save(data)

    @contextmanager
    def pipeline_session(self):
        data = self.validate(self.root)
        with locked(self.paths(data["branch"])[0].with_suffix(".pipeline.lock")):
            with locked(self.directory / "allocation.lock"):
                data = self.validate(self.root)
                data["pipeline"] = True
                self.save(data)
            yield

    def start(self, *, issue: str = "", key: str = "", kind: str = "", base: str = "", change: int = 0) -> dict:
        for result in self.reap():
            print(result, file=sys.stderr)
        pull = forge.change(change) if change else None
        if change and (not pull or pull.state != "open"):
            raise Blocked(msg("workspace_change_mismatch", str(change)))
        if not (pull or issue or kind and key):
            raise Blocked(msg("workspace_identity_required"))
        remote_origin = git(self.root, "remote", "get-url", "origin", optional=True)
        base = base or ("origin/" if remote_origin else "refs/heads/") + (pull.base if pull else get("git.baseBranch", "develop"))
        if base.startswith("origin/") and remote_origin:
            git(self.root, "check-ref-format", "refs/heads/" + base[7:])
            git(self.root, "fetch", "--prune", "origin")
        elif not forge.name and base.startswith("refs/heads/"):
            git(self.root, "check-ref-format", base)
        else:
            raise Blocked(msg("workspace_base_invalid", base))
        base_sha = git(self.root, "rev-parse", "--verify", base + "^{commit}")
        if issue:
            if not tracker.valid_key(issue):
                raise Blocked(msg("workspace_issue_invalid", issue))
            found = tracker.issue(issue)
            if not found:
                raise Blocked(msg("workspace_issue_unavailable", issue))
            if not found.open:
                raise Blocked(msg("workspace_issue_closed", issue))
            if any(person != tracker.login() for person in found.assignees):
                raise Blocked(msg("workspace_issue_assigned", issue))
        if pull and not named(pull.head, issue, key, kind, base):
            raise Blocked(msg("workspace_change_mismatch", pull.head))
        branch = pull.head if pull else self.find(issue, key, kind, base) or self.issue_branch(issue)
        if not branch:
            slug = key or slug_of(found.title)
            if not slug:
                raise Blocked(msg("workspace_title_slug_empty", issue))
            branch = branch_for(kind or issue_type(found), slug, issue, base)
        record, path = self.paths(branch)
        try:
            opened = self.open_changes(branch)
        except forge.ForgeUnavailable as error:
            if change:
                raise
            print(msg("workspace_forge_offline", error), file=sys.stderr)
            opened = []
        if len(opened) > 1 or change and [pull.number for pull in opened] != [change]:
            raise Blocked(msg("workspace_change_mismatch", branch))
        if opened:
            pull = opened[0]
            remote = git(self.root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + branch, optional=True)
            if (pull.head != branch or pull.head_sha != remote or "origin/" + pull.base != base
                    or getattr(pull, "cross_repo", False)):
                raise Blocked(msg("workspace_change_mismatch", branch))
        with locked(self.directory / "allocation.lock"):
            if record.exists():
                data = self.read(branch)
                if data.get("phase") == "retired":
                    record.unlink(missing_ok=True)
                else:
                    expected_base = "origin/" + opened[0].base if opened else data["base"]
                    if expected_base != base:
                        raise Blocked(msg("workspace_record_invalid", branch))
                    if opened and not self.unbound(data, opened[0].number, release=False):
                        raise Blocked(msg("workspace_change_mismatch", branch))
                    path = self.location(data)
                    with locked(record.with_suffix(".lock")):
                        if data.get("phase") == "allocating":
                            self.recover(data, path)
                        self.validate(path)
                        self.sync_head(branch, path, opened[0] if opened else None)
                        if opened:
                            self.unbound(data, opened[0].number)
                            self.follow(data, opened[0])
                        data["change"] = opened[0].number if opened else data.get("change", 0)
                        self.save(data)
                        return data
            if path.exists() or path.is_symlink() or path.resolve() != path:
                raise Blocked(msg("workspace_collision", branch, path))
            local = git(self.root, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch, optional=True)
            remote = git(self.root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + branch, optional=True)
            if not opened and (local or remote) and not (canonical(branch) and self.owned(branch, base_sha, local or remote)):
                raise Blocked(msg("workspace_collision", branch, path))
            local = self.sync_head(branch, pull=opened[0] if opened else None)
            data = {"branch": branch, "path": str(path), "base": base, "baseSha": base_sha,
                    "issue": issue, "key": key, "change": opened[0].number if opened else 0,
                    "phase": "allocating", "allocationHead": local or remote or base_sha}
            self.save(data)
            self.recover(data, path)
            head = local or remote
            if head and not opened and self.absorbed(base_sha, head):
                git(path, "merge", "--quiet", "--no-edit", base_sha)
            return data

    def issue_branch(self, issue: str) -> str:
        if not issue:
            return ""
        refs = git(self.root, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").splitlines()
        return only({branch for branch in (ref.removeprefix("origin/") for ref in refs) if tracker.branch_issue(branch) == issue})

    def sync_head(self, branch: str, path: Path | None = None, pull=None) -> str:
        local = git(self.root, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch, optional=True)
        remote = git(self.root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + branch, optional=True)
        if pull and (pull.head != branch or pull.head_sha != remote or getattr(pull, "cross_repo", False)):
            raise Blocked(msg("workspace_change_mismatch", branch))
        holder = self.holder(branch)
        if holder and (path is None or "locked" in holder
                       or Path(holder["worktree"]).resolve() != path.resolve()):
            raise Blocked(msg("workspace_branch_busy", branch, holder["worktree"]))
        if path is not None and (not holder or holder.get("HEAD") != local):
            raise Blocked(msg("workspace_record_invalid", branch))
        if local and remote and local != remote:
            fork = git(self.root, "merge-base", local, remote, optional=True)
            if fork == local:
                if path is None:
                    git(self.root, "update-ref", "refs/heads/" + branch, remote, local)
                else:
                    self.fast_forward(path, local, remote)
                local = remote
            elif fork != remote:
                raise Blocked(msg("workspace_branch_diverged", branch))
        return local or remote

    def fast_forward(self, path: Path, local: str, target: str) -> None:
        if dirty(path):
            raise Blocked(msg("workspace_dirty", path))
        added = [Path(name) for name in git(path, "diff", "--no-renames", "--name-only",
                                           "--diff-filter=A", "-z", local, target).split("\0") if name]
        if added:
            roots = sorted({":(literal)" + new.parts[0] for new in added})
            ignored = [Path(name) for name in git(path, "ls-files", "--others", "--ignored",
                                                 "--exclude-standard", "--directory", "-z", "--", *roots).split("\0") if name]
            placed = set(record_files(str(path)))
            if any((new == old or new in old.parents or old in new.parents) and not kit_only(path, old, placed)
                   for new in added for old in ignored):
                raise Blocked(msg("workspace_dirty", path))
        git(path, "reset", "--keep", target)
        self.placed(path)

    @staticmethod
    def placed(path: Path) -> None:
        installed = install_kit(str(path))
        if installed is not None and installed.returncode:
            raise Blocked(msg("workspace_install_failed", path, (installed.stderr or installed.stdout).strip()))

    def absorbed(self, base_sha: str, head: str) -> bool:
        merged = git(self.root, "merge-tree", "--write-tree", base_sha, head, optional=True)
        return bool(merged) and merged.splitlines()[0] == git(self.root, "rev-parse", base_sha + "^{tree}")

    def contains(self, head: str, sha: str) -> bool:
        return git(self.root, "rev-list", "--count", head + ".." + sha, optional=True) == "0"

    def landed(self, pull, chain: tuple[int, ...] = ()) -> None:
        base = "origin/" + pull.base
        if git(self.root, "rev-parse", "--verify", "--quiet", base + "^{commit}", optional=True):
            git(self.root, "merge-base", "--is-ancestor", pull.merge_sha, base)
            return
        for number in forge.merged_changes(pull.base):
            parent = forge.change(number) if number not in (*chain, pull.number) else None
            if parent and parent.state == "merged" and parent.merge_sha and parent.head == pull.base:
                git(self.root, "fetch", "origin", parent.fetch_ref)
                if self.contains(parent.head_sha, pull.merge_sha):
                    return self.landed(parent, (*chain, pull.number))
        raise Blocked(msg("workspace_base_gone", pull.base, pull.number))

    def caught_up(self, pull) -> str:
        current = git(self.root, "rev-parse", "--verify", "--quiet", "refs/heads/" + pull.head, optional=True)
        if current != pull.head_sha and not git(self.root, "rev-parse", "--verify", "--quiet",
                                                pull.head_sha + "^{commit}", optional=True):
            git(self.root, "fetch", "origin", pull.fetch_ref)
        if current and current != pull.head_sha:
            if not self.contains(pull.head_sha, current):
                raise Blocked(msg("workspace_behind_diverged", pull.head, pull.number))
        return current

    @contextmanager
    def session(self, path: Path):
        branch = git(path, "branch", "--show-current")
        with locked(self.paths(branch)[0].with_suffix(".lock")):
            yield self.validate(path)
            self.validate(path)

    def cleanup(self, change: int, *, observed=None, fetched: bool = False) -> dict:
        if os.environ.get("HARNESS_TASK_BRANCH"):
            raise Blocked(msg("workspace_cleanup_author"))
        pull = observed or forge.change(change)
        if not pull or pull.state != "merged" or not pull.merge_sha:
            raise Blocked(msg("workspace_merge_required", change))
        record, path = self.paths(pull.head)
        with locked(record.with_suffix(".pipeline.lock")), locked(record.with_suffix(".lock")):
            if (not record.is_file() and not git(self.root, "rev-parse", "--verify", "--quiet",
                                                 "refs/heads/" + pull.head, optional=True)
                    and not self.holder(pull.head)):
                return {"branch": pull.head, "change": change, "phase": "retired"}
            data = self.load(pull.head)
            path = Path(data.get("path", ""))
            vanished = (data.get("phase") == "active" and path.resolve() == path and path.parent.is_dir()
                        and self.gone(path))
            if not vanished:
                path = self.location(data)
            if self.primary(path):
                raise Blocked(msg("workspace_current", path))
            if data.get("phase") == "retired":
                return data
            if data.get("pipeline") and not data.get("afterMerge"):
                raise Blocked(msg("workspace_pipeline_incomplete", change))
            if data.get("change") not in (0, change):
                raise Blocked(msg("workspace_change_mismatch", pull.head))
            self.follow(data, pull)
            if path.resolve() == self.root or self.root.is_relative_to(path.resolve()):
                raise Blocked(msg("workspace_current", path))
            if not fetched:
                git(self.root, "fetch", "--prune", "origin")
            self.landed(pull)
            current = self.caught_up(pull)
            if vanished:
                holder = self.holder(pull.head)
                if holder:
                    raise Blocked(msg("workspace_branch_busy", pull.head, holder["worktree"]))
                if not self.absorbed(pull.merge_sha, pull.head_sha):
                    raise Blocked(msg("workspace_unpreserved", change))
                git(self.root, "update-ref", "refs/heads/" + pull.head, pull.head_sha, current or "0" * len(pull.head_sha))
                bundle = self.directory / (str(change) + "-" + pull.head_sha + ".bundle")
                git(self.root, "bundle", "create", str(bundle), "refs/heads/" + pull.head, "--not", data["baseSha"])
                git(self.root, "bundle", "verify", str(bundle))
                data.update(phase="retiring", head=pull.head_sha, change=change, merge=pull.merge_sha, bundle=str(bundle))
                self.save(data)
                with locked(self.directory / "allocation.lock"):
                    return self.retire(data, path)
            if current and current != pull.head_sha and self.attached(path, pull.head, current):
                self.fast_forward(path, current, pull.head_sha)
            if data.get("phase") == "retiring" and not path.exists():
                if data.get("head") != pull.head_sha or data.get("merge") != pull.merge_sha or path.is_symlink():
                    raise Blocked(msg("workspace_record_invalid", pull.head))
                git(self.root, "bundle", "verify", data["bundle"])
                with locked(self.directory / "allocation.lock"):
                    return self.retire(data, path)
            self.validate(path, phases=("active", "retiring"))
            if not self.attached(path, pull.head, pull.head_sha):
                raise Blocked(msg("workspace_record_invalid", pull.head))
            if dirty(path):
                raise Blocked(msg("workspace_dirty", path))
            if git(path, "rev-parse", "HEAD") != pull.head_sha:
                raise Blocked(msg("workspace_head_changed", pull.head))
            if not self.absorbed(pull.merge_sha, pull.head_sha):
                raise Blocked(msg("workspace_unpreserved", change))
            bundle = self.directory / (str(change) + "-" + pull.head_sha + ".bundle")
            git(self.root, "bundle", "create", str(bundle), "refs/heads/" + pull.head, "--not", data["baseSha"])
            git(self.root, "bundle", "verify", str(bundle))
            ignored = [name for name in git(path, "ls-files", "--others", "--ignored", "--exclude-standard",
                                            "--directory", "-z").split("\0") if name]
            listed = {Path(name.rstrip("/")) for name in ignored}
            for name in ignored:
                relative = Path(name.rstrip("/"))
                source = path / relative
                if (relative.is_absolute() or ".." in relative.parts or relative.parts[0] == ".git"
                        or not source.parent.resolve().is_relative_to(path.resolve())
                        or git(path, "ls-files", "--", name)):
                    raise Blocked(msg("workspace_record_invalid", pull.head))
                if listed.intersection(relative.parents):
                    continue
                if getattr(source.lstat(), "st_reparse_tag", 0) == MOUNT_POINT:
                    source.rmdir()
                elif source.is_symlink() or not source.is_dir():
                    source.unlink()
                else:
                    shutil.rmtree(source)
            if dirty(path) or git(path, "rev-parse", "HEAD") != pull.head_sha:
                raise Blocked(msg("workspace_dirty", path))
            data.update(phase="retiring", head=pull.head_sha, change=change, merge=pull.merge_sha, bundle=str(bundle))
            self.save(data)
            with locked(self.directory / "allocation.lock"):
                return self.retire(data, path)

    def retire(self, data: dict, path: Path) -> dict:
        branch, head = data["branch"], data["head"]
        if path != self.location(data):
            raise Blocked(msg("workspace_record_invalid", branch))
        if self.primary(path):
            raise Blocked(msg("workspace_current", path))
        current = git(self.root, "rev-parse", "--verify", "refs/heads/" + branch, optional=True)
        if current and current != head:
            raise Blocked(msg("workspace_head_changed", branch))
        if path.exists() and (dirty(path) or git(path, "ls-files", "--others", "--ignored", "--exclude-standard")
                              or git(path, "rev-parse", "HEAD") != head):
            raise Blocked(msg("workspace_dirty", path))
        if self.registered(path):
            if not self.attached(path, branch, head):
                raise Blocked(msg("workspace_record_invalid", branch))
            git(self.root, "worktree", "remove", *([] if path.exists() else ["--force"]), str(path))
        elif path.exists():
            raise Blocked(msg("workspace_record_invalid", branch))
        if current:
            git(self.root, "update-ref", "-d", "refs/heads/" + branch, head)
        data["phase"] = "retired"
        self.save(data)
        self.purge(data)
        return data

    def purge(self, data: dict) -> None:
        record = self.paths(data["branch"])[0]
        reviews = self.review(data.get("change"))
        if data.get("change") and reviews.is_dir():
            trees = [fields["worktree"] for fields in self.worktrees()
                     if fields.get("worktree") and Path(fields["worktree"]).resolve().is_relative_to(reviews.resolve())]
            for tree in sorted(trees, reverse=True):
                git(self.root, "worktree", "remove", "--force", tree)
            shutil.rmtree(reviews)
        bundle = Path(data.get("bundle") or "")
        if bundle.parent == self.directory:
            bundle.unlink(missing_ok=True)
        record.unlink(missing_ok=True)

    def holder(self, branch: str) -> dict:
        return next((fields for fields in self.worktrees() if fields.get("branch") == "refs/heads/" + branch), {})

    def vacated(self, path: Path, branch: str) -> bool:
        return self.primary(path) and not self.holder(branch)

    def abandoned(self, data: dict) -> bool:
        path = Path(data.get("path", ""))
        return (data.get("phase") == "active" and not data.get("change")
                and (self.gone(path) or self.vacated(path, data["branch"]))
                and not any(git(self.root, "rev-parse", "--verify", "--quiet", ref + data["branch"], optional=True)
                            for ref in ("refs/heads/", "refs/remotes/origin/")))

    def reap_record(self, data: dict, results: list[str], fetched: bool) -> bool:
        record = self.paths(data["branch"])[0]
        if data.get("phase") == "retired":
            with locked(self.directory / "allocation.lock"):
                current = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
                if all(current.get(name) == data.get(name) for name in ("phase", "head", "merge")):
                    self.purge(current)
            return fetched
        path = Path(data.get("path", ""))
        if not self.gone(path) and self.primary(self.location(data)) and self.holder(data["branch"]):
            return fetched
        number = data.get("change")
        if not number:
            if self.abandoned(data):
                with locked(self.directory / "allocation.lock"):
                    if (record.is_file() and self.abandoned(self.load(data["branch"]))
                            and not git(self.root, "ls-remote", "--heads", "origin", "refs/heads/" + data["branch"])):
                        record.unlink(missing_ok=True)
                        results.append(msg("workspace_record_dropped", data["branch"]))
                return fetched
            if git(self.root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + data["branch"], optional=True):
                opened = self.open_changes(data["branch"])
                if len(opened) == 1:
                    with locked(self.directory / "allocation.lock"):
                        if record.is_file():
                            current = self.load(data["branch"])
                            current["change"] = opened[0].number
                            self.save(current)
            return fetched
        pull = forge.change(number)
        with locked(self.directory / "allocation.lock"):
            if not record.is_file():
                return fetched
            current = self.load(data["branch"])
            released = self.released(current, pull)
        if released:
            return self.reap_record(current, results, fetched)
        if pull and pull.state == "merged":
            if self.vacated(path, data["branch"]):
                with locked(self.directory / "allocation.lock"):
                    current = self.load(data["branch"]) if record.is_file() else {}
                    if current and not (current.get("pipeline") and not current.get("afterMerge")):
                        self.purge(current)
                        results.append(msg("workspace_record_merged", number, data["branch"]))
                return fetched
            if not fetched:
                git(self.root, "fetch", "--prune", "origin")
            self.cleanup(number, observed=pull, fetched=True)
            results.append(msg("workspace_removed", data["branch"]))
            return True
        return fetched

    def reap(self) -> list[str]:
        if not forge.name:
            return []
        results, fetched = [], False
        for data in self.records():
            try:
                fetched = self.reap_record(data, results, fetched)
            except forge.ForgeUnavailable as error:
                results.append(msg("workspace_forge_offline", error))
                break
            except (Blocked, OSError, ValueError, forge.ForgeError) as error:
                if self.paths(data["branch"])[0].is_file():
                    results.append(str(error))
        return results


def check_local(root: Path, message: str = "") -> dict | None:
    runner = os.environ.get("HARNESS_TASK_BRANCH")
    if os.environ.get("HARNESS_TASK_DETACHED") and not runner:
        return None
    branch = git(root, "branch", "--show-current")
    if not (session_agent() or runner):
        return
    workspace = Workspace(root)
    if branch and not workspace.paths(branch)[0].is_file():
        if runner or canonical(branch):
            raise Blocked(msg("workspace_unassigned", branch))
        if release_flow(branch):
            raise Blocked(msg("workspace_shared_branch", branch))
        return
    data = workspace.validate(root)
    if os.environ.get("HARNESS_TASK_BRANCH", branch) != branch:
        raise Blocked(msg("workspace_branch_invalid", branch))
    if message:
        git(root, "interpret-trailers", "--in-place", "--if-exists", "replace", "--trailer", "Task-Branch: " + data["branch"], message)
    return data


def inherited_commits(root: Path, base: str, branches: set[str]) -> set[str]:
    heads = set()
    for branch in sorted(branches):
        for number in forge.merged_changes(branch):
            pull = forge.change(number)
            if (not pull or pull.state != "merged" or pull.cross_repo or pull.head != branch
                    or not pull.merge_sha or not pull.head_sha
                    or git(root, "merge-base", pull.merge_sha, base, optional=True) != pull.merge_sha):
                continue
            if not git(root, "rev-parse", "--verify", "--quiet", pull.head_sha + "^{commit}", optional=True):
                git(root, "fetch", "--quiet", "--no-tags", "origin", pull.fetch_ref)
                if not git(root, "rev-parse", "--verify", "--quiet", pull.head_sha + "^{commit}", optional=True):
                    continue
            heads.add(pull.head_sha)
    return set(git(root, "log", "--no-merges", "--format=%H", *sorted(heads), "--not", base).splitlines()) if heads else set()


def first_change(root: Path, base: str, pick: tuple[str, ...], *paths: str) -> int:
    stamps = git(root, "log", "--first-parent", "-m", *pick, "--format=%ct", base, "--", *paths).splitlines()
    return min(map(int, stamps), default=0)


def check_ci(root: Path, base: str, branch: str) -> None:
    if not branch or branch.startswith("release/") or not hooked():
        return
    entries = commits(root, base + "..HEAD")
    cutoff, agent, legacy = 0, set(), ""
    convention = canonical(branch)
    if convention:
        introduced = first_change(root, base, ("--diff-filter=A",), ".harness/scripts/task_workspace.py",
                                  ".harness/scripts/pipeline/task_workspace.py")
        adopted = first_change(root, base, ("-S", '"branchConvention"'), ".harness/project.json",
                               ".harness/project/manifest.json")
        cutoff = introduced and max(introduced, adopted)
        people = dict(line.split("\0", 1) for line in git(root, "log", "--no-merges", "--format=%H%x00%ae%x00%ce",
                                                          base + "..HEAD").splitlines() if "\0" in line)
        agent = {sha for sha, _, body, _ in entries if agent_commit(body, tuple(people.get(sha, "").split("\0")))}
    if cutoff and any(not marks and sha in agent for sha, _, _, marks in entries):
        number = os.environ.get("PR_NUMBER")
        if not number:
            prefix = get("project.gitConfigPrefix", "")
            bindings = git(root, "config", "--get-all", f"branch.{branch}.{prefix}-pr", optional=True).splitlines() if prefix else []
            common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            if (common / "harness" / "tasks").is_dir():
                workspace = Workspace(root)
                if workspace.paths(branch)[0].is_file():
                    change = workspace.read(branch).get("change")
                    if change:
                        bindings.append(str(change))
            number = bindings[0] if bindings and len(set(bindings)) == 1 else None
        pull = forge.change(int(number)) if number and number.isdecimal() and int(number) > 0 else None
        if (pull and pull.state == "open" and not pull.cross_repo and pull.created
                and pull.head == branch and "origin/" + pull.base == base):
            created = datetime.fromisoformat(pull.created.replace("Z", "+00:00"))
            if created.tzinfo is not None and created.timestamp() < cutoff:
                legacy = pull.head_sha
    foreign = {marks[0] for _, _, _, marks in entries if len(marks) == 1 and marks[0] != branch}
    inherited = inherited_commits(root, base, foreign) if foreign and forge.name else set()
    legacy_commits = set()
    if legacy and any(not marks for _, _, _, marks in entries):
        heads = forge.historical_heads(pull, cutoff)
        if heads:
            legacy_commits = set(git(root, "log", "--ignore-missing", "--no-merges", "--format=%H", *heads,
                                     "--not", base, optional=True).splitlines())
            legacy_commits.intersection_update(git(root, "log", "--no-merges", "--format=%H", legacy,
                                                   "--not", base, optional=True).splitlines())
    for sha, _, body, marks in entries:
        if not marks and convention and forge.name:
            number = forge.change_in_message(body)
            merged = forge.change(number) if number else None
            if (merged and merged.state == "merged" and not merged.cross_repo and merged.base == branch
                    and sha in (merged.merge_sha, merged.squash_sha)):
                continue
        if branch not in marks and sha in inherited:
            continue
        authored = sha in agent
        if legacy and not marks and sha in legacy_commits:
            continue
        if (marks or cutoff and authored) and marks != [branch]:
            raise Blocked(msg("workspace_ci_invalid", branch))
