from __future__ import annotations

import io
import os
import re
import subprocess
from dataclasses import dataclass, field

from ci_common import kit_owned, msg, record_files
from generated_sources import authored, installed
from manifest import get, get_record

PIPELINE = "methodology.json"


def body_chars() -> int:
    return int(get("processes.review.bodyChars", 0, file=PIPELINE))

DIFF = "pr.diff"
REMOVED = "removed.md"
IMPACT = "impact.md"
FIXES = "fixes.diff"

PLACE = re.compile(r"^\s+- `([^`\n]+?):\d+`", re.M)
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

DEFINITION = re.compile(r"\b(?:def|class|function|func|fn|interface|struct|enum|record|type)\s+([A-Za-z_$][\w$]*)")

METHOD = re.compile(r"^\s*(?:(?:public|private|protected|internal|static|async|override|virtual|sealed|abstract|partial|"
                    r"extern|unsafe|new)\s+)+[\w<>\[\],.?]+(?:\s*<[^>]*>)?\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(")

ARROW = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)")
CONSTANT = re.compile(r"^\s*(?:export\s+|readonly\s+|const\s+)?([A-Z][A-Z0-9_]{2,})\s*[:=]")
SHELL_FUNCTION = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*\(\)\s*\{")
JSON_KEY = re.compile(r'^\s*"([^"\s]+)"\s*:')
YAML_KEY = re.compile(r"^\s*-?\s*([A-Za-z_][\w.-]*)\s*:(?:\s|$)")
INI_KEY = re.compile(r"^\s*([A-Za-z_][\w.-]*)\s*=")

STEP = re.compile(r"^\s*-?\s*name:\s*['\"]?(.+?)['\"]?\s*$")

HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
ROW = re.compile(r"^\s*\|(.+)\|\s*$")
SEPARATOR = re.compile(r"^[\s|:-]+$")
MARKUP = re.compile(r"[`*]")

MESSAGE_PREFIX = "MSG_"

PROSE = (".md", ".mdx")

SHORT = 4
LONG = 80
SHOWN = 120


@dataclass
class Line:
    path: str
    number: int
    text: str
    names: list[str]


@dataclass
class Hits:
    changed: list[tuple[str, int, str]] = field(default_factory=list)
    other: list[tuple[str, int, str]] = field(default_factory=list)


@dataclass
class Change:
    rows: list[list[str]] = field(default_factory=list)
    removed: list[Line] = field(default_factory=list)
    added: list[Line] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)


def limits() -> tuple[int, int, int]:
    found = get_record("processes.review.maps", {}, file=PIPELINE)
    return int(found.get("entries", 0)), int(found.get("places", 0)), int(found.get("frequent", 0))


def git(cwd: str, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, env=env, check=False)
    if check and done.returncode:
        why = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(msg("round_git_failed", " ".join(args[:2]), why[-1] if why else done.returncode))
    return done


def diff(tree: str, base: str, head: str) -> str:
    return git(tree, "diff", "-M", "--no-ext-diff", "--no-textconv", "--no-color", base, head).stdout.decode(
        "utf-8", "replace")


def token(text: str, prose: bool = False) -> str:
    text = MARKUP.sub("", text).strip(" _") if prose else text.strip()
    return text if SHORT <= len(text) <= LONG else ""


def anchors(path: str, line: str) -> list[str]:
    suffix = os.path.splitext(path)[1].lower()
    if suffix in PROSE:
        heading = HEADING.match(line)
        row = ROW.match(line)
        if heading:
            return [token(heading.group(1), True)]
        if row and not SEPARATOR.match(row.group(1)):
            return [token(row.group(1).split("|", 1)[0], True)]
        return []
    if suffix == ".json":
        found = JSON_KEY.findall(line)
    elif suffix in (".yml", ".yaml"):
        step = STEP.match(line)
        found = [step.group(1)] if step else YAML_KEY.findall(line)
    elif suffix in (".toml", ".ini", ".cfg", ".editorconfig", ".env"):
        found = INI_KEY.findall(line)
    else:
        found = (DEFINITION.findall(line) + METHOD.findall(line) + ARROW.findall(line) + CONSTANT.findall(line)
                 + SHELL_FUNCTION.findall(line))
    return [token(name.removeprefix(MESSAGE_PREFIX)) for name in found]


def parse(text: str) -> Change:
    found = Change()
    kind, old, new = "", "", ""
    left = right = 0
    hunk = False

    def close() -> None:
        if kind:
            found.rows.append(["R", old, new] if kind == "R" else [kind, new if kind == "A" else old])
            if kind == "R":
                found.renamed[old] = new

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            close()
            old, _, new = raw[len("diff --git a/"):].partition(" b/")
            kind, hunk = "M", False
        elif (head := HUNK.match(raw)) is not None:
            hunk, left, right = True, int(head.group(1)), int(head.group(2))
        elif not hunk:
            if raw.startswith("deleted file mode"):
                kind = "D"
            elif raw.startswith("new file mode"):
                kind = "A"
            elif raw.startswith("rename from "):
                kind, old = "R", raw[len("rename from "):]
            elif raw.startswith("rename to "):
                new = raw[len("rename to "):]
        elif raw.startswith("-"):
            found.removed.append(Line(old, left, raw[1:], anchors(old, raw[1:])))
            left += 1
        elif raw.startswith("+"):
            found.added.append(Line(new, right, raw[1:], anchors(new, raw[1:])))
            right += 1
        elif raw.startswith(" "):
            left, right = left + 1, right + 1
    close()
    return found


def search(tree: str, tokens: list[str], changed: set[str]) -> dict[str, Hits]:
    found = {name: Hits() for name in tokens}
    if not tokens:
        return found
    pattern = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, sorted(tokens, key=len, reverse=True))) + r")(?!\w)")
    staged = (entry.partition("\t") for entry in git(tree, "ls-files", "--stage", "-z").stdout.decode("utf-8").split("\0") if entry)
    paths = [path for metadata, _, path in staged if metadata.split(" ", 1)[0] in {"100644", "100755"}]
    for path in [*paths, *sorted(record_files(tree))]:
        full = os.path.join(tree, path)
        try:
            with open(full, "rb") as handle:
                if b"\0" in handle.read(8000):
                    continue
                handle.seek(0)
                with io.TextIOWrapper(handle, encoding="utf-8", errors="replace", newline="\n") as lines:
                    for number, text in enumerate(lines, 1):
                        for match in {item.group(1) for item in pattern.finditer(text)}:
                            place = (path, number, text.strip()[:SHOWN])
                            (found[match].changed if path in changed else found[match].other).append(place)
        except FileNotFoundError:
            continue
    return found


def places(rows: list[tuple[str, int, str]], shown: int) -> list[str]:
    lines = [f"  - `{path}:{number}` {text}" for path, number, text in rows[:shown]]
    if len(rows) > shown:
        lines.append("  - " + msg("maps_more", len(rows) - shown))
    return lines


def prose(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PROSE


def removed_map(change: Change, hits: dict[str, Hits], entries: int, shown: int, frequent: int) -> str:
    lines = [msg("maps_removed_title"), "", msg("maps_files_title"), ""]
    gone = [f"- {row[0]} `" + "` -> `".join(row[1:]) + "`" for row in change.rows if row[0] in ("D", "R")]
    lines += gone or [msg("maps_none")]
    lines += ["", msg("maps_lines_title"), ""]
    edited, moved = set(), {}
    for line in change.added:
        for name in line.names:
            edited.add((line.path, name))
            moved.setdefault(name, line.path)
    seen, listed = set(), 0
    for line in change.removed:
        for name in line.names:
            if not name or (change.renamed.get(line.path, line.path), name) in edited or (line.path, name) in seen:
                continue
            seen.add((line.path, name))
            listed += 1
            if listed > entries:
                lines.append(msg("maps_cut", entries))
                return "\n".join(lines) + "\n"
            lines.append(f"- `{line.path}:{line.number}` `{name}`: {line.text.strip()[:SHOWN]}")
            found = hits.get(name, Hits())
            at_head = found.changed + found.other
            if name in moved:
                lines.append("  - " + msg("maps_in_diff", moved[name]))
            if len(at_head) > frequent:
                lines.append("  - " + msg("maps_frequent", len(at_head)))
            elif at_head:
                lines.append("  - " + msg("maps_at_head"))
                lines += places(at_head, shown)
            elif name not in moved:
                lines.append("  - " + msg("maps_absent"))
    if not listed:
        lines.append(msg("maps_none"))
    return "\n".join(lines) + "\n"


def impact_map(subjects: dict[str, str], hits: dict[str, Hits], entries: int, shown: int, frequent: int) -> str:
    lines = [msg("maps_impact_title"), ""]
    listed = 0
    for name, origin in list(subjects.items())[:entries]:
        other = hits.get(name, Hits()).other
        if not other or len(other) > frequent:
            continue
        listed += 1
        lines.append(f"- `{name}` ({origin})")
        lines += places(other, shown)
    if not listed:
        lines.append(msg("maps_none"))
    if len(subjects) > entries:
        lines.append(msg("maps_cut", entries))
    return "\n".join(lines) + "\n"


def write_inputs(tree: str, data: str, base: str, head: str, header: str = "") -> list[str]:
    os.makedirs(data, exist_ok=True)
    text = diff(tree, base, head)
    with open(os.path.join(data, DIFF), "w", encoding="utf-8") as handle:
        handle.write(header + "\n" + text if header else text)
    return write_maps(tree, data, text)


def write_maps(tree: str, data: str, text: str) -> list[str]:
    entries, shown, frequent = limits()
    change = parse(text)
    changed = {path for row in change.rows for path in row[1:]}
    code = [line for line in change.removed + change.added if not prose(line.path)]
    sides = [{(change.renamed.get(line.path, line.path), name) for line in part if not prose(line.path)
              for name in line.names} for part in (change.removed, change.added)]
    subjects: dict[str, str] = {}
    for line in code:
        for name in line.names:
            if name and not all((change.renamed.get(line.path, line.path), name) in side for side in sides):
                subjects.setdefault(name, f"`{line.path}:{line.number}`")
    for row in change.rows:
        if row[0] != "A":
            for name in (row[1], os.path.basename(row[1])):
                if len(name) >= SHORT:
                    subjects.setdefault(name, msg("maps_path_origin", row[0]))
    names = list(dict.fromkeys(name for line in change.removed for name in line.names if name))
    hits = search(tree, sorted(set(names[:entries]) | set(list(subjects)[:entries])), changed)
    os.makedirs(data, exist_ok=True)
    texts = {REMOVED: removed_map(change, hits, entries, shown, frequent),
             IMPACT: impact_map(subjects, hits, entries, shown, frequent)}
    for name, body in texts.items():
        with open(os.path.join(data, name), "w", encoding="utf-8") as handle:
            handle.write(body)
    return list(texts)


def start_tree(tree: str, since: str, base: str) -> str:
    merged = git(tree, "merge-tree", "--write-tree", since, base, check=False) if base else None
    return merged.stdout.decode("utf-8").split()[0] if merged and merged.returncode == 0 else since


def write_fixes(tree: str, data: str, since: str, head: str, base: str) -> str:
    if not since or since == head:
        return ""
    present = git(tree, "cat-file", "-e", f"{since}^{{commit}}", check=False).returncode == 0
    body = diff(tree, start_tree(tree, since, base), head) if present else msg("maps_fixes_missing", since[:12]) + "\n"
    with open(os.path.join(data, FIXES), "w", encoding="utf-8") as handle:
        handle.write(msg("maps_fixes_title", since[:12], head[:12]) + "\n" + body)
    return FIXES


def expected(data: str) -> list[str]:
    with open(os.path.join(data, DIFF), encoding="utf-8") as handle:
        paths = [row[-1] for row in parse(handle.read()).rows if row[0] != "D"]
    try:
        with open(os.path.join(data, IMPACT), encoding="utf-8") as handle:
            paths += PLACE.findall(handle.read())
    except FileNotFoundError:
        pass
    owned, exclude = kit_owned(), get_record("policies.size", {}, file="methodology.json").get("exclude", [])
    return sorted({path for path in paths if authored(path, owned, exclude)})


def unread(read: list[str], wanted: list[str]) -> list[str]:
    seen = set(read) | {installed(path) for path in read}
    return [path for path in wanted if path not in seen and installed(path) not in seen]
