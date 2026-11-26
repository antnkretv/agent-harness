from __future__ import annotations

import posixpath
import re
from collections.abc import Callable

LINK = re.compile(r"(\(?)(?<!!)\[([^\]\n]+)\]\(([^)\s#]*)(#[^)\s]*)?\)(\)?)")
SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
SKILL = re.compile(r"\.agents/skills/([^/]+)/SKILL\.md")
CODE = re.compile(r"`([^`]+)`")
ENTRY = ("AGENTS.md", ".harness/README.md")
GONE = "\0"


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


def names(label: str, path: str) -> bool:
    text = label.strip("`").rstrip("/")
    base = posixpath.basename(path)
    stem = posixpath.splitext(base)[0]
    return text in (path, base, stem) or path.endswith("/" + text) or text.endswith(("/" + base, "/" + stem))


def mention(label: str, path: str, gates: dict[str, str], line: str, wrapped: bool) -> str:
    gate, skill = gates.get(path), SKILL.fullmatch(path)
    if gate:
        name = f"`{gate}`"
        if name in line.replace(label, ""):
            return ""
        said = f"гейт {name}"
    else:
        said = f"`{skill.group(1)}`" if skill else f"`{path}`"
    if names(label, path) or skill and label.strip("`") == skill.group(1):
        return said
    if not gate and not skill:
        base = posixpath.basename(path)
        stem = posixpath.splitext(base)[0]
        for token in (f"`{base}`", base, *[stem] * stem.isupper()):
            if re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w.-])", label):
                return re.sub(rf"(?<![\w.-]){re.escape(token)}(?![\w.-])", lambda _: said, label, count=1)
        if label.startswith("§"):
            return f"{said} {label}"
    return f"{label}: {said}" if wrapped else f"{label} ({said})"


def unlinked(source: str, text: str, resolve: Callable[[str, str], str | None],
             gates: dict[str, str]) -> tuple[str, list[tuple[int, str, str]]]:
    lines, changes = text.split("\n"), []
    for number, line in prose_lines(text):
        code = code_spans(line)

        def replace(match: re.Match[str]) -> str:
            if in_code(code, match.start(2)):
                return match.group(0)
            target = target_of(source, match.group(3))
            kept = None if target is None or allowed(source, target) else resolve(target, (match.group(4) or "")[1:])
            if kept is None:
                return match.group(0)
            opened, closed = match.group(1), match.group(5)
            said = mention(match.group(2), kept, gates, line, bool(opened and closed))
            if not said:
                return GONE if bool(opened) == bool(closed) else opened + closed
            return opened + said + closed
        new = re.sub(" ?" + GONE, "", LINK.sub(replace, line))
        if new != line:
            changes.append((number, line.strip(), new.strip()))
            lines[number - 1] = new
    return "\n".join(lines), changes
