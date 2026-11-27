from __future__ import annotations

import json
import os
import re
from typing import NamedTuple

from .resources import BLOCK_MARKERS, HASH_MARKERS, PROJECT_BLOCK


def merge_json(base, extra):
    if isinstance(base, dict) and isinstance(extra, dict):
        merged = dict(base)
        for key, value in extra.items():
            merged[key] = merge_json(merged[key], value) if key in merged else value
        return merged
    if isinstance(base, list) and isinstance(extra, list):
        merged = list(base)
        for value in extra:
            key = value["id"] if keyed(value) else None
            same = next((index for index, item in enumerate(merged) if key is not None and keyed(item) and item["id"] == key), None)
            if same is not None:
                merged[same] = merge_json(merged[same], value)
            elif value not in merged:
                merged.append(value)
        return merged
    return base


def scalar_list(value) -> bool:
    return isinstance(value, list) and not any(isinstance(item, (dict, list)) for item in value)


def overlay_json(kit, mine, shipped: dict | None = None, path: str = ""):
    if isinstance(kit, dict) and isinstance(mine, dict):
        return {**{key: overlay_json(kit[key], value, shipped, f"{path}/{key}") if key in kit else value
                   for key, value in mine.items()},
                **{key: value for key, value in kit.items() if key not in mine}}
    if shipped is not None and scalar_list(kit) and scalar_list(mine):
        retired = shipped.get(path, [])
        return kit + [item for item in mine if item not in kit and item not in retired]
    return kit


def kit_lists(data, path: str = "") -> dict[str, list]:
    if isinstance(data, dict):
        return {found: items for key, value in data.items() for found, items in kit_lists(value, f"{path}/{key}").items()}
    return {path: data} if scalar_list(data) and data else {}


def keyed(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("id"), str)


def json_indent(data: bytes) -> str:
    return "\t" if data.startswith(b"{\n\t") else "  "


def json_text(data, indent: str = "  ") -> str:
    return json.dumps(data, ensure_ascii=False, indent="\t" if indent == "\t" else 2) + "\n"


def markers(path: str, block: str) -> tuple[str, str]:
    begin, end = BLOCK_MARKERS.get(os.path.splitext(path)[1], HASH_MARKERS)
    if block == PROJECT_BLOCK:
        return begin.replace("harness:begin {id}", "project:begin"), end.replace("harness:end {id}", "project:end")
    return begin.format(id=block), end.format(id=block)


def entry_markers(path: str, entry: dict) -> tuple[str, str]:
    return markers(path, entry.get("block", "core"))


def block_of(text: str, begin: str, end: str) -> tuple[int, int] | None:
    start = text.find(begin)
    stop = text.find(end, start + 1) if start >= 0 else -1
    if start < 0 or stop < 0:
        return None
    return start, stop + len(end)


def block_text(data: bytes, begin: str, end: str) -> bytes | None:
    text = data.decode("utf-8", "replace")
    span = block_of(text, begin, end)
    return None if span is None else text[span[0]:span[1]].encode("utf-8")


class Layout(NamedTuple):
    title: str
    head: str
    mine: str
    outside: list[str]
    tail: str
    project_first: bool


def project_wrapper(path: str, text: str) -> tuple[str, str, str]:
    if not path.endswith((".props", ".targets")):
        return "", text, ""
    text = re.sub(r"<Project\b([^>]*?)\s*/>", r"<Project\1>\n</Project>", text, count=1)
    opening, closing = re.search(r"<Project\b[^>]*>", text), text.rfind("</Project>")
    if opening is None or closing < opening.end():
        return "", text, ""
    return text[:opening.end()], text[opening.end():closing], text[closing:]


def layout(path: str, text: str, kit: str) -> Layout:
    lines = text.lstrip("\n").split("\n", 1)
    title = lines[0] if path.endswith(".md") and lines[0].startswith("# ") else ""
    head, body, tail = project_wrapper(path, (lines[1] if len(lines) > 1 else "") if title else text)
    pair = {name: markers(path, name) for name in (kit, PROJECT_BLOCK)}
    spans = {name: block_of(body, *pair[name]) for name in pair}
    mine = ""
    if spans[PROJECT_BLOCK]:
        start, stop = spans[PROJECT_BLOCK]
        mine = body[start + len(pair[PROJECT_BLOCK][0]):stop - len(pair[PROJECT_BLOCK][1])].lstrip("\n").rstrip()
    outside, cursor = [], 0
    for start, stop in sorted(span for span in spans.values() if span):
        outside.append(body[cursor:start])
        cursor = stop
    outside.append(body[cursor:])
    first = bool(spans[PROJECT_BLOCK] and spans[kit] and spans[PROJECT_BLOCK][0] < spans[kit][0])
    return Layout(title, head, mine, outside, tail, first)


def outside_text(path: str, text: str, kit: str) -> str:
    return "".join(layout(path, text, kit).outside).strip()


def compose(path: str, current: str, template: str, kit: str) -> str:
    block = template[slice(*block_of(template, *markers(path, kit)))]
    found = layout(path, current, kit)
    outside = found.outside
    lines, preamble = outside[0].lstrip("\n").split("\n"), block.split("\n\n", 1)[0].split("\n")[1:]
    if lines[:len(preamble)] == preamble:
        outside[0] = "\n".join(lines[len(preamble):])
    content = "\n\n".join([found.mine] * bool(found.mine) + [part.lstrip("\n").rstrip() for part in outside if part.strip()])
    begin, end = markers(path, PROJECT_BLOCK)
    own = begin + "\n" + (content + "\n" if content else "") + ("  " if found.head else "") + end
    first, second = (own, block) if layout(path, template, kit).project_first else (block, own)
    if found.head or found.tail:
        return found.head + "\n  " + first + "\n  " + second + "\n" + found.tail
    return (found.title + "\n\n" if found.title else "") + first + "\n\n" + second + "\n"


ENTRY_LINES = {"AGENTS.md": (".harness/AGENTS.md",), "CLAUDE.md": ("@.harness/AGENTS.md", "@.harness/CLAUDE.md")}


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
    given = {line for line in preamble.split("\n") if line.startswith("@")}
    own = ["\n".join(line for line in part.split("\n") if line not in given).strip()
           for part in [found.mine] + [part.strip() for part in found.outside if part.strip()]]
    parts = [found.title or title, preamble, *[part for part in own if part]]
    return "\n\n".join(part for part in parts if part) + "\n"


def unwired(path: str, text: str) -> list[str]:
    return [line for line in ENTRY_LINES.get(path, ()) if line not in text]
