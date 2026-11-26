from __future__ import annotations

import fnmatch
import functools
import json
import posixpath
import re
from pathlib import Path

from .catalog import lookup
from .resources import PROJECT, git, load_json, msg, read_lock

STACK_MARKERS = {"pyproject.toml": "python", "go.mod": "go", "pom.xml": "jvm", "build.gradle": "jvm"}
HISTORICAL_FOLDERS = frozenset({"research", "journal", "journals", "decision-log", "decision-logs", "minutes"})
GENERATED_FOLDERS = frozenset({".design-sync", "dist", "node_modules", "bin", "obj", "coverage", ".next", ".nuxt",
                               "__generated__", "storybook-static", "playwright-report", "test-results"})
PACKAGE = re.compile(r'<Package(?:Reference|Version)\b[^>]*?\bInclude="([^"]+)"')
REMOTE_SERVER = re.compile(r"^(?:(https?)://(?:[^@/]+@)?([^/]+)|\w+://(?:[^@/]+@)?([^/:]+)|(?:[\w.-]+@)?([\w.-]+):)")
REMOTE_REPO = re.compile(r"(?:^\w+://(?:[^@/]+@)?[^/]+/|^(?:[\w.-]+@)?[\w.-]+:)((?:[\w.-]+/)+[\w.-]+?)(?:\.git)?/?$")
def package_match(name: str, wanted_ids: list[str]) -> bool:
    return any(name == known or name.startswith(known + ".") for known in wanted_ids)


DETECT_KEYS = frozenset({"files", "content", "paths", "remote", "server", "packages", "npm"})


class Tree:
    def __init__(self, root: Path | None):
        self.root = root
        self.texts: dict[str, str] = {}

    @functools.cached_property
    def name(self) -> str:
        if self.root is None:
            return "project"
        common = git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        clone = Path(common) if common else None
        return clone.parent.name if clone is not None and clone.name == ".git" else self.root.name

    @functools.cached_property
    def files(self) -> list[str]:
        if self.root is None:
            return []
        owned = {path for path, entry in read_lock(self.root).get("files", {}).items() if entry.get("role") != "seed"}
        manifest = self.root / PROJECT
        project = load_json(manifest, msg("no_project", manifest)) if manifest.is_file() else {}
        ignored = tuple(prefix for key in ("generatedPrefixes", "payloadPrefixes")
                        for prefix in lookup(project, f"docs.{key}", []) if isinstance(prefix, str) and prefix)
        listed_files = git(self.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", check=False).split("\0")
        return sorted(path for path in listed_files if path and path not in owned
                      and not path.startswith(".harness/") and "node_modules/" not in path
                      and not path.startswith(ignored)
                      and (self.root / path).is_file())

    @functools.cached_property
    def named(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for path in self.files:
            found.setdefault(posixpath.basename(path), []).append(path)
        return found

    def text(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8", errors="replace")

    def read(self, path: str) -> str:
        if path not in self.texts:
            self.texts[path] = self.text(path)
        return self.texts[path]

    def glob(self, patterns) -> list[str]:
        plain = [mask for mask in patterns if not any(char in mask for char in "/*?[")]
        found = {path for mask in plain for path in self.named.get(mask, [])}
        masks = [mask for mask in patterns if mask not in plain]
        if masks:
            found.update(path for path in self.files if any(
                fnmatch.fnmatchcase(path if "/" in mask else posixpath.basename(path), mask) for mask in masks))
        return sorted(found)

    @functools.cached_property
    def remote(self) -> str:
        return git(self.root, "remote", "get-url", "origin", check=False) if self.root else ""

    @functools.cached_property
    def branches(self) -> set[str]:
        if self.root is None:
            return set()
        refs = git(self.root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes", check=False)
        return {ref.removeprefix("refs/heads/").removeprefix("refs/remotes/") for ref in refs.splitlines()
                if ref.startswith(("refs/heads/", "refs/remotes/")) and not ref.endswith("/HEAD")}

    def branch(self, *candidates: str) -> str | None:
        return next((name for name in candidates if name in self.branches or f"origin/{name}" in self.branches), None)

    def scan(self, patterns, pattern: re.Pattern) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for path in self.glob(patterns):
            for name in pattern.findall(self.read(path)):
                found.setdefault(name, []).append(path)
        return found

    @functools.cached_property
    def packages(self) -> dict[str, list[str]]:
        return self.scan(("*.csproj", "*.props", "*.targets"), PACKAGE)

    @functools.cached_property
    def npm(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for path in self.glob(("package.json",)):
            data = self.package(path)
            for group in ("dependencies", "devDependencies", "peerDependencies"):
                for name in data.get(group) if isinstance(data.get(group), dict) else {}:
                    found.setdefault(name, []).append(path)
        return found

    def package(self, path: str) -> dict:
        try:
            data = json.loads(self.read(path))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def matched(self, detect: dict) -> list[str]:
        paths = self.glob(detect.get("files", []))
        if "content" not in detect:
            return paths
        pattern = re.compile(detect["content"])
        return [path for path in paths if pattern.search(self.read(path))]

    def evidence(self, detect: dict) -> list[str]:
        found = []
        for mask in detect.get("paths", []):
            hit = next((path for path in self.files if path == mask or mask.endswith("/") and path.startswith(mask)), None)
            found += [msg("why_file", hit)] if hit else []
        if detect.get("remote") and self.remote and re.search(detect["remote"], self.remote):
            found.append(msg("why_remote", self.remote))
        found += [msg("why_package", name, places[0]) for name, places in sorted(self.packages.items())
                  if package_match(name, detect.get("packages", []))]
        found += [msg("why_npm", name, self.npm[name][0]) for name in detect.get("npm", []) if name in self.npm]
        if found and "content" in detect:
            return found
        return [msg("why_file", path) for path in self.matched(detect)] + found

    def candidates(self, detect: dict) -> tuple[list[str], list[str]]:
        if "files" in detect:
            values = self.matched(detect)
            return values, [msg("why_file", value) for value in values]
        if "packages" in detect:
            hits = {known: hit for known in detect["packages"]
                    if (hit := next(((name, places[0]) for name, places in sorted(self.packages.items())
                                     if package_match(name, [known])), None))}
            return list(hits), [msg("why_package", *hit) for hit in hits.values()]

        if "remote" in detect:
            matched = bool(self.remote and re.search(detect["remote"], self.remote))
            found = (remote_server(self.remote) if detect.get("server") else
                     (REMOTE_REPO.search(self.remote) or [None, ""])[1]) if matched else ""
            return ([found], [msg("why_remote", self.remote)]) if found else ([], [])
        return [], []


def remote_server(remote: str) -> str:
    found = REMOTE_SERVER.match(remote)
    if not found:
        return ""
    scheme, host = (found.group(1), found.group(2)) if found.group(1) else ("https", found.group(3) or found.group(4))
    return f"{scheme}://{host}"
