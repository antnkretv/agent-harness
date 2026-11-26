from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from platform_messages import msg

SKILLS = ".agents/skills"
RULES = ".agents/rules"
PREFIX = "harness-"
ADAPTERS = ((".claude", ".md", True), (".cursor", ".mdc", False))


def wanted(root: Path) -> dict[Path, Path]:
    rules = sorted((root / RULES).glob("*.md")) if (root / RULES).is_dir() else []
    links = {}
    for folder, suffix, skills in ADAPTERS:
        if not (root / folder).is_dir():
            continue
        if skills and (root / SKILLS).is_dir():
            links[root / folder / "skills"] = root / SKILLS
        for rule in rules:
            links[root / folder / "rules" / (PREFIX + rule.stem + suffix)] = rule
    return links


def current(link: Path, target: Path) -> bool:
    return link.exists() and os.path.samefile(link, target)


def place(link: Path, target: Path) -> None:
    try:
        os.symlink(os.path.relpath(target, link.parent), link, target_is_directory=target.is_dir())
    except OSError:
        if os.name != "nt":
            raise
        if not target.is_dir():
            os.link(target, link)
            return
        done = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        if done.returncode:
            raise OSError(done.stderr.strip() or done.stdout.strip()) from None


def make(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        place(link, target)
    except OSError:
        if not current(link, target):
            raise


def stale(root: Path, keep: set[Path]) -> list[Path]:
    found = []
    for folder, suffix, _ in ADAPTERS:
        rules = root / folder / "rules"
        if rules.is_dir():
            found += [path for path in rules.glob(PREFIX + "*" + suffix) if path not in keep]
    return found


def ensure(root: Path) -> None:
    links = wanted(root)
    for path in stale(root, set(links)):
        path.unlink(missing_ok=True)
    for link, target in links.items():
        if link.exists() or link.is_symlink():
            if current(link, target):
                continue
            if link.is_dir() and not link.is_symlink():
                raise OSError(msg("agent_links_occupied", link.relative_to(root)))
            link.unlink(missing_ok=True)
        make(link, target)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[2])
    try:
        ensure(root)
    except OSError as error:
        print(msg("agent_links_failed", error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
