from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from .documents import layout, markers
from .links import CODE, LINK, installed_test, prose_lines, unlinked
from .resources import LOCK, METHODOLOGY, PROJECT_BLOCK, git, say

LEGACY_DOCS = {
    "docs/platform/delivery/ci-cd.md": ".harness/docs/ci-cd.md",
    "docs/platform/delivery/sdd.md": ".harness/docs/sdd.md",
    "docs/platform/agents/prompts.md": ".harness/docs/prompts.md",
    "docs/platform/agents/templates.md": ".harness/docs/templates.md",
    "docs/platform/agents/delegated-task.md": ".harness/docs/delegated-task.md",
    "docs/platform/agents/terms.md": ".harness/docs/terms.md",
    "docs/platform/standards/root-entry-docs.md": ".harness/docs/root-entry-docs.md",
    "docs/platform/agents/harness.md": ".harness/docs/harness.md",
    "docs/platform/agents/harness-parameters.md": ".harness/docs/harness-parameters.md",
    "docs/platform/agents/environment.md": ".harness/docs/environment.md",
}
SPLIT = {
    "AGENTS.md": ".harness/AGENTS.md",
    "CLAUDE.md": ".harness/CLAUDE.md",
    "CONTRIBUTING.md": ".harness/CONTRIBUTING.md",
    "REVIEW.md": ".harness/REVIEW.md",
    "docs/README.md": ".harness/docs/README.md",
    "docs/platform/adr/README.md": ".harness/docs/adr.md",
    ".agents/skills/README.md": ".harness/docs/skills.md",
}
ENTRY_LINES = {"AGENTS.md": (".harness/AGENTS.md",), "CLAUDE.md": ("@.harness/AGENTS.md", "@.harness/CLAUDE.md")}
HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
EMPHASIS = re.compile(r"\*")


def slug(title: str) -> str:
    text = CODE.sub(lambda match: match.group(1), LINK.sub(lambda match: match.group(2), title))
    return re.sub(r"[^\w\- ]", "", EMPHASIS.sub("", text).strip().lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    found, seen = set(), {}
    for _, line in prose_lines(text):
        heading = HEADING.match(line)
        if heading:
            base = slug(heading.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def seed_parts(path: str, template: str) -> tuple[str, str, str]:
    found = layout(path, template, "core")
    begin = markers(path, PROJECT_BLOCK)[0]
    preamble = found.outside[0].split(begin, 1)[0].strip() if found.outside else ""
    return found.title, preamble, found.mine


def seeded(path: str, template: str) -> str:
    title, preamble, mine = seed_parts(path, template)
    parts = [part for part in (title, preamble, mine) if part]
    return "\n\n".join(parts) + "\n" if parts else ""


def converted(path: str, current: str, template: str) -> str:
    found = layout(path, current, "core")
    title, preamble, _ = seed_parts(path, template)
    own = [found.mine] + [part.strip() for part in found.outside if part.strip()]
    parts = [found.title or title, preamble, *[part for part in own if part]]
    return "\n\n".join(part for part in parts if part) + "\n"


def unwired(path: str, text: str) -> list[str]:
    return [line for line in ENTRY_LINES.get(path, ()) if line not in text]


def resolver(read: Callable[[str], str | None], installed: Callable[[str], bool]) -> Callable[[str, str], str | None]:
    def resolve(target: str, anchor: str) -> str | None:
        target = LEGACY_DOCS.get(target, target)
        kit = SPLIT.get(target)
        if kit and anchor:
            own, theirs = read(target), read(kit)
            if own is not None and theirs is not None and anchor not in anchors(own) and anchor in anchors(theirs):
                return kit
        return target if installed(target) else None
    return resolve


def project_links(source: str, text: str, resolve: Callable[[str, str], str | None], gates: dict[str, str],
                  repository: str = "") -> tuple[str, list[tuple[int, str, str]]]:
    before = text
    if repository:
        address = re.escape(repository.removesuffix(".git")) + r"/blob/[^/)\s>]+/payload/([^)\s#>]+)"
        up = "/".join([".."] * source.count("/") + [""])
        def known(path: str) -> bool:
            return resolve(path, "") is not None
        text = re.sub(r"(?<=\]\()" + address, lambda match: up + match.group(1) if known(match.group(1)) else match.group(0), text)
        text = re.sub(r"<" + address + r"(#[^>\s]*)?>", lambda match: f"`{LEGACY_DOCS.get(match.group(1), match.group(1))}`"
                      if known(match.group(1)) else match.group(0), text)
    text, _ = unlinked(source, text, resolve, gates)
    changes = [(number, old.strip(), new.strip()) for number, (old, new) in
               enumerate(zip(before.split("\n"), text.split("\n")), 1) if old != new]
    return text, changes


def gates_of(methodology: dict) -> dict[str, str]:
    return {record["gate"]: record["id"] for record in methodology.get("modules", [])
            if isinstance(record.get("gate"), str) and record["gate"].startswith(".harness/")}


def legacy_layout(lock: dict) -> bool:
    return any(entry.get("role") == "block" and path.endswith(".md") for path, entry in lock.get("files", {}).items())


def relinked(root: Path, planned: list) -> list[tuple[str, int, str, str]]:
    tracked = set(filter(None, git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")))
    installed = installed_test(tracked, {item.path for item in planned if item.place == "lock"})

    def read(path: str) -> str | None:
        try:
            return (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    resolve = resolver(read, installed)
    gates = gates_of(json.loads(read(METHODOLOGY) or "{}"))
    repository = json.loads(read(LOCK) or "{}").get("kit", {}).get("repository", "")
    rewritten = []
    for path in sorted(path for path in tracked if path.endswith(".md") and read(path) is not None):
        text, changes = project_links(path, read(path), resolve, gates, repository)
        if changes:
            (root / path).write_text(text, encoding="utf-8", newline="")
            rewritten += [(path, *change) for change in changes]
    if rewritten:
        say("apply_links_rewritten", len({path for path, *_ in rewritten}))
        for path, number, old, new in rewritten:
            print(f"  {path}:{number}\n    - {old}\n    + {new}")
    return rewritten
