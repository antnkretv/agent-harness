from __future__ import annotations

import posixpath
import re
from collections.abc import Callable

LINK = re.compile(r"(\(?)(?<!!)\[([^\]\n]+)\]\(([^)\s#]*)(#[^)\s]*)?\)(\)?)")
SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
ENTRY = ("AGENTS.md", ".harness/README.md")


def target_of(source: str, target: str) -> str | None:
    if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("//"):
        return None
    found = posixpath.normpath(target[1:] if target.startswith("/") else posixpath.join(posixpath.dirname(source), target))
    return None if found.startswith("..") else found


def prose_lines(text: str):
    inside = False
    for number, line in enumerate(text.split("\n"), 1):
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            yield number, line


def code_spans(line: str) -> list[tuple[int, int]]:
    return [match.span() for match in SPAN.finditer(line) if "](" in match.group(0)]


def in_code(spans: list[tuple[int, int]], position: int) -> bool:
    return any(start <= position < stop for start, stop in spans)


def folders(paths: set[str]) -> set[str]:
    return {path.rsplit("/", depth)[0] for path in paths for depth in range(1, path.count("/") + 1)}


def installed_test(tracked: set[str], placed: set[str]) -> Callable[[str], bool]:
    held, kit = folders(tracked), folders(placed)
    return lambda target: target not in tracked and target not in held and (target in placed or target in kit)


def allowed(source: str, target: str) -> bool:
    return (source, target) == ENTRY


def installed_links(source: str, text: str, installed: Callable[[str], bool]) -> list[tuple[int, str]]:
    found = []
    for number, line in prose_lines(text):
        code = code_spans(line)
        for match in LINK.finditer(line):
            if in_code(code, match.start(2)):
                continue
            target = target_of(source, match.group(3))
            if target and not allowed(source, target) and installed(target):
                found.append((number, target))
    return found

