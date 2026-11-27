from __future__ import annotations

import argparse
import io
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit

from .documents import json_text
from .resources import BRANCH_CONVENTIONS, PROJECT, Refusal, git as runtime_git, msg, sha256

RUNNER = ".harness/scripts/review/review-local.py"
RECEIPT = ".review-delegation.json"
AUTHORITY_ENV = ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "GITHUB_API_URL", "GITHUB_GRAPHQL_URL", "CI_PROJECT_PATH",
                 "CI_SERVER_URL", "CI_API_V4_URL")


def providers(catalog: dict) -> tuple[str, ...]:
    return tuple(catalog["modules"]["agents"]["extensions"])


def git(root: Path, *args: str) -> str:
    return runtime_git(root, "-c", "core.hooksPath=/dev/null", *args)


def digest(path: Path) -> str:
    return sha256(path.read_bytes())


def inventory(root: Path) -> dict[str, str]:
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        raise Refusal(msg("review_bootstrap_symlink"))
    return {path.relative_to(root).as_posix(): digest(path) for path in sorted(root.rglob("*")) if path.is_file()}


def unpack(archive: Path, expected: str, destination: Path) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise Refusal(msg("review_bootstrap_anchor_required"))
    content = archive.read_bytes()
    if sha256(content) != expected:
        raise Refusal(msg("review_bootstrap_anchor_mismatch"))
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        members = tar.getmembers()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name or ":" in member.name
                    or not path.parts or member.name in names or not (member.isdir() or member.isfile())):
                raise Refusal(msg("review_bootstrap_archive_member"))
            names.add(member.name)
        tar.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not (roots[0] / "payload" / RUNNER).is_file():
        raise Refusal(msg("review_bootstrap_no_engine"))
    return roots[0]


def project(raw: dict, remote: str) -> dict:
    address = urlsplit(remote if "://" in remote else "ssh://" + remote.replace(":", "/", 1))
    if (address.scheme not in ("https", "ssh") or not address.hostname or address.password
            or address.query or address.fragment or address.scheme == "https" and address.username):
        raise Refusal(msg("review_bootstrap_origin"))
    server = "https://" + (address.netloc if address.scheme == "https" else address.hostname)
    repository = address.path.strip("/").removesuffix(".git")
    if not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)+", repository):
        raise Refusal(msg("review_bootstrap_repository"))
    prefix = raw.get("project", {}).get("gitConfigPrefix", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", prefix):
        raise Refusal(msg("review_bootstrap_prefix"))
    declared = raw.get("modules", {}).get("forge", {}).get("extensions", {})
    if len(declared) != 1 or next(iter(declared)) not in ("github", "gitlab"):
        raise Refusal(msg("review_bootstrap_forge"))
    kind, settings = next(iter(declared.items()))
    forge_key = "repo" if kind == "github" else "project"
    declared_url = urlsplit(str(settings.get("url") or ""))
    if (address.scheme == "ssh" and declared_url.scheme == "https" and declared_url.hostname == address.hostname
            and not declared_url.username and declared_url.path.strip("/") == ""):
        server = "https://" + declared_url.netloc
    if settings.get(forge_key) != repository or settings.get("url", server).rstrip("/") != server:
        raise Refusal(msg("review_bootstrap_authority"))
    modules = {"forge": {"extensions": {kind: {forge_key: repository, "url": server}}}}
    cli = raw.get("agents", {}).get("cli", {})
    if not cli or set(cli) - {"codexVersion", "claudeVersion", "cursorVersion", "nodeVersion"} or any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in cli.values()):
        raise Refusal(msg("review_bootstrap_cli"))
    trackers = raw.get("modules", {}).get("tracker", {}).get("extensions", {})
    if "github-issues" in trackers:
        if kind != "github" or trackers["github-issues"].get("repo") != repository:
            raise Refusal(msg("review_bootstrap_tracker"))
        modules["tracker"] = {"extensions": {"github-issues": {"repo": repository}}}
    convention = raw.get("git", {}).get("branchConvention")
    branches = {"baseBranch": "develop", "releaseBranch": "master",
                "branchConvention": convention if convention in BRANCH_CONVENTIONS else BRANCH_CONVENTIONS[0]}
    return {"project": {"gitConfigPrefix": prefix, "name": prefix}, "git": branches,
            "agents": {"cli": cli}, "modules": modules}


def overlay(kit: Path, target: Path, config: dict) -> None:
    platform = target / ".harness"
    intended = target.absolute() / ".harness"
    if platform.resolve() != intended:
        raise Refusal(msg("review_bootstrap_symlink"))
    if platform.exists():
        shutil.rmtree(intended)
    shutil.copytree(kit / "payload/.harness", intended)
    catalog = target / ".harness/state/modules.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(kit / "modules.json", catalog)
    (target / PROJECT).write_text(json_text(config), encoding="utf-8", newline="\n")


POLICY_FILES = ("AGENTS.md", "REVIEW.md", "CONTRIBUTING.md", "specs/README.md", "docs/README.md", ".specify/memory/constitution.md",
                ".harness/AGENTS.md", ".harness/REVIEW.md", ".harness/CONTRIBUTING.md", ".harness/README.md")
POLICY_PREFIXES = ("docs/platform/", ".harness/docs/", ".agents/skills/")


def policy_documents(kit: Path, config: dict, root: Path, base: str) -> dict[str, bytes]:
    sys.dont_write_bytecode = True
    name = "_harness_review_policy_" + sha256(str(kit).encode())[:16]
    package = importlib.util.spec_from_file_location(name, kit / "installer/__init__.py",
                                                    submodule_search_locations=[str(kit / "installer")])
    module = importlib.util.module_from_spec(package)
    sys.modules[name] = module
    package.loader.exec_module(module)
    renderer, blocks_seed = (importlib.import_module(name + "." + child) for child in ("render", "documents"))
    metadata = json.loads((kit / "kit.json").read_text(encoding="utf-8"))
    extra = {**renderer.computed(metadata, config), "harness.installDate": git(root, "show", "-s", "--format=%cs", base)}
    existing = set(git(root, "ls-tree", "-r", "--name-only", base).splitlines())
    files = {}
    for path, entry in metadata["files"].items():
        if not (path in POLICY_FILES or path.startswith(POLICY_PREFIXES)):
            continue
        if entry.get("role") == "seed" and path in existing:
            continue
        content = (kit / "payload" / path).read_bytes()
        if entry.get("render"):
            content = renderer.render(content.decode("utf-8"), config, extra, path).encode("utf-8")
        if entry.get("role") == "seed":
            content = blocks_seed.seeded(path, content.decode("utf-8")).encode("utf-8")
        files[path] = content
    return files


def engine(admin: Path):
    sys.dont_write_bytecode = True
    runpy.run_path(str(admin / ".harness/scripts/script_paths.py"))
    import review_local
    return review_local


def source_state(root: Path, branch: str, base_ref: str, tool) -> dict:
    if git(root, "status", "--porcelain"):
        raise Refusal(msg("review_bootstrap_clean"))
    head = git(root, "rev-parse", "HEAD")
    git(root, "fetch", "origin", branch)
    if git(root, "rev-parse", "FETCH_HEAD") != head:
        raise Refusal(msg("review_bootstrap_pushed"))
    git(root, "fetch", "origin", base_ref)
    return {"source": str(root), "branch": git(root, "branch", "--show-current"),
            "head_sha": head, "base_sha": git(root, "rev-parse", "FETCH_HEAD"),
            "snapshot": tool.snapshot(str(root)) if tool else "",
            "origin": sha256(git(root, "remote", "get-url", "origin").encode())}


def review(args: argparse.Namespace) -> int:
    previous = {name: os.environ.pop(name, None) for name in AUTHORITY_ENV}
    previous_bytecode = os.environ.get("PYTHONDONTWRITEBYTECODE")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return run(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, tarfile.TarError) as error:
        raise Refusal(str(error)) from None
    finally:
        for name, value in {**previous, "PYTHONDONTWRITEBYTECODE": previous_bytecode}.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)


def run(args: argparse.Namespace) -> int:
    root, work, archive = Path(args.target).resolve(), Path(args.work).resolve(), Path(args.archive).resolve()
    if work.is_relative_to(root) or root.is_relative_to(work):
        raise Refusal(msg("review_bootstrap_outside"))
    branch = git(root, "branch", "--show-current")
    if not branch:
        raise Refusal(msg("review_bootstrap_branch"))
    base_ref = args.base.removeprefix("origin/").removeprefix("refs/heads/")
    git(root, "check-ref-format", "refs/heads/" + base_ref)
    initial = source_state(root, branch, base_ref, None)
    context = {str(Path(args.context).resolve()): digest(Path(args.context))} if args.context else {}
    if any(path.startswith(".harness/scripts/") and PurePosixPath(path).name == PurePosixPath(RUNNER).name
           for path in git(root, "ls-tree", "-r", "--name-only", initial["base_sha"]).splitlines()):
        raise Refusal(msg("review_bootstrap_established"))
    if not args.collect and work.exists() and any(work.iterdir()):
        raise Refusal(msg("review_bootstrap_packet_exists"))
    work.mkdir(parents=True, exist_ok=True)
    receipt, control = work / RECEIPT, work / "control"
    admin = control / ".review-admin"
    with tempfile.TemporaryDirectory(prefix="harness-review-kit-") as directory:
        kit = unpack(archive, args.sha256, Path(directory))
        raw = json.loads(git(root, "show", f"HEAD:{PROJECT}"))
        config = project(raw, git(root, "remote", "get-url", "origin"))
        config["git"]["baseBranch"] = base_ref
        documents = policy_documents(kit, config, root, initial["base_sha"])
        expected_files = {".harness/" + path: value for path, value in inventory(kit / "payload/.harness").items()}
        expected_files[".harness/state/modules.json"] = digest(kit / "modules.json")
        expected_files[PROJECT] = sha256(json_text(config).encode())
        policy = work / "head/.review-base"
        anchor = {"archive": str(archive), "sha256": args.sha256, "tools": expected_files,
                  "policy": {**expected_files, **{path: sha256(content)
                                                for path, content in documents.items()}}}
        if args.collect:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            if (saved.get("bootstrap") != anchor or inventory(admin) != expected_files
                    or any(digest(policy / path) != value for path, value in anchor["policy"].items())):
                raise Refusal(msg("review_bootstrap_tools_changed"))
        else:
            git(work, "init", "--quiet", str(control))
            git(control, "fetch", "--no-tags", str(root), initial["head_sha"], initial["base_sha"])
            git(control, "remote", "add", "origin", git(root, "remote", "get-url", "origin"))
            git(control, "checkout", "--quiet", "-b", branch, initial["head_sha"])
            if git(control, "ls-files", ".review-admin", ".review-base"):
                raise Refusal(msg("review_bootstrap_reserved"))
            with (control / ".git/info/exclude").open("a", encoding="utf-8") as handle:
                handle.write("\n.review-admin/\n.review-base/\n")
            overlay(kit, admin, config)
            git(control, "config", f"branch.{branch}.{config['project']['gitConfigPrefix']}-pr", str(args.pr))
        tool = engine(admin)
        state = source_state(root, branch, base_ref, tool)
        if {key: value for key, value in state.items() if key != "snapshot"} != {key: value for key, value in initial.items() if key != "snapshot"}:
            raise Refusal(msg("review_bootstrap_source_changed"))
        change = tool.forge.change(int(args.pr))
        if (not change or change.head != branch or change.head_sha != state["head_sha"]
                or change.base_sha != state["base_sha"] or getattr(change, "cross_repo", False)):
            raise Refusal(msg("review_bootstrap_change"))
        native = argparse.Namespace(pr=str(args.pr), work=str(work), reason=args.reason, passes=args.passes,
                                    context=args.context, provider=args.provider, keep=True, dry_run=True,
                                    collect=args.collect, execute=False, publish=False)
        if args.collect:
            if saved["source"] != state or str(saved["pr"]) != str(args.pr):
                raise Refusal(msg("review_bootstrap_snapshot"))
            sys.modules["review_trust"].verify_packet(saved, str(work))
            plan = json.loads((work / "round.json").read_text(encoding="utf-8"))
            if args.provider != plan["provider"] or tool.review_evidence.task_hash(int(args.pr)) != plan["task_hash"]:
                raise Refusal(msg("review_bootstrap_context"))
            result = tool.collect_native(native)
            sys.modules["review_trust"].verify_packet(saved, str(work))
        else:
            head = work / "head"
            tool.prepare_tree(state["base_sha"], str(head), str(policy))
            overlay(kit, policy, config)
            for path, content in documents.items():
                destination = policy / path
                if destination.resolve() != destination.absolute():
                    raise Refusal(msg("review_bootstrap_symlink"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            result = tool.review(native, state["base_sha"], str(work), str(head), str(policy), [])
            if result == 0:
                plan = json.loads((work / "round.json").read_text(encoding="utf-8"))
                paths = [work / "round.json", control / ".git/config"]
                paths += [Path(item[key]) for item in plan["passes"] for key in ("prompt", "context") if item.get(key)]
                receipt.write_text(json_text({"source": state, "pr": str(args.pr), "runner": RUNNER,
                                              "base_ref": base_ref, "bootstrap": anchor, "context": context,
                                              "packet": {str(path.relative_to(work)): digest(path) for path in paths},
                                              "trees": {str(tree): tool.snapshot(str(tree)) for tree in (head, policy)}}),
                                   encoding="utf-8")
        if (source_state(root, branch, base_ref, tool) != state or inventory(admin) != expected_files
                or digest(archive) != args.sha256
                or any(digest(Path(path)) != value for path, value in
                       (saved["context"] if args.collect else context).items())):
            raise Refusal(msg("review_bootstrap_changed"))
        return result
