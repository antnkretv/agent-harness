from __future__ import annotations

import os
import re

ENTRY = ".gitlab-ci.yml"
KIT_FILE = ".gitlab/ci/harness.yml"
BLOCK = re.compile(r"# harness:begin core\n.*?# harness:end core\n?", re.S)
LOCAL = re.compile(r"^\s*-?\s*local:\s*['\"]?([^'\"\s#]+)", re.M)
ITEM = re.compile(r"^(\s*)-\s")
ALWAYS = "- when: always"


def read(root: str, path: str) -> str | None:
    try:
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            return handle.read().replace("\r\n", "\n")
    except OSError:
        return None


def section(text: str, key: str) -> list[str]:
    lines, found = [], False
    for line in text.splitlines():
        if found and line[:1] not in ("", " ", "\t", "#"):
            break
        if found:
            lines.append(line)
        elif line.rstrip() == key + ":":
            found = True
    return lines if found else []


def rules(text: str) -> list[list[str]]:
    body = section(text, "workflow")
    start = next((index for index, line in enumerate(body) if line.strip() == "rules:"), None)
    if start is None:
        return []
    level = len(body[start]) - len(body[start].lstrip())
    items: list[list[str]] = []
    for line in body[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= level and not line.lstrip().startswith("-"):
            break
        found = ITEM.match(line)
        if found and (not items or len(found.group(1)) <= len(ITEM.match(items[0][0]).group(1))):
            items.append([line])
        elif items:
            items[-1].append(line)
    return items


def plain(item: list[str]) -> str:
    return " ".join(" ".join(item).split())


def shown(item: list[str]) -> str:
    cut = len(item[0]) - len(item[0].lstrip())
    return "\n".join(line[cut:] for line in item)


def project_files(root: str) -> list[str]:
    text = read(root, ENTRY) or ""
    return [path for path in LOCAL.findall(BLOCK.sub("", text)) if path.lstrip("/") != KIT_FILE]


def missing(root: str) -> list[tuple[str, list[str]]]:
    kit = [item for item in rules(read(root, KIT_FILE) or "") if plain(item) != ALWAYS]
    found = []
    for path in project_files(root):
        own = rules(read(root, path.lstrip("/")) or "")
        if not section(read(root, path.lstrip("/")) or "", "workflow"):
            continue
        if [plain(item) for item in own[:len(kit)]] != [plain(item) for item in kit]:
            found.append((path.lstrip("/"), [shown(item) for item in kit]))
    return found
