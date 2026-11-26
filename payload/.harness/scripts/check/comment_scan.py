"""Слой комментариев: история - в git, трассировка - в спеке, длинный разбор - в runbook.

Стандарт - docs/platform/standards/code-comments.md.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-comments.py                  # изменения против базы
    ./.harness/hooks/python.cmd .harness/scripts/check/check-comments.py --all            # все отслеживаемое дерево
    ./.harness/hooks/python.cmd .harness/scripts/check/check-comments.py --files a.sh b.cs
"""

from __future__ import annotations
import argparse
import functools
import os
import posixpath
import re
import sys
from ci_common import ROOT, TEST_PATH, base_ref, changed_files, generated, get_record, git, listed, machine_text, modules, msg, payload, tracked

STANDARD = modules().get("comment-layer", {}).get("doc") or "docs/platform/standards/code-comments.md"

RULES = get_record("docs.comments", {})
RULE_KEYS = ("plain", "doc", "density", "constantFloor", "traceability", "moodGuard", "history", "failure")
NESTED_KEYS = {"plain": ("lines", "words", "chars"), "doc": ("lines", "words", "chars"), "density": ("max", "floor")}
MISSING = [key for key in RULE_KEYS if key not in RULES] + [
    f"{key}.{field}" for key, fields in NESTED_KEYS.items() if isinstance(RULES.get(key), dict)
    for field in fields if field not in RULES[key]] if RULES else []
SETTINGS = {} if MISSING else RULES

HASH_SUFFIXES = (".sh", ".bash", ".ps1", ".psm1", ".yml", ".yaml", ".py", ".toml")
CONSTANT_SUFFIXES = (".cs", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
SLASH_SUFFIXES = CONSTANT_SUFFIXES + (".vue", ".css", ".scss")
JSON_SUFFIXES = (".json",)

JSON_COMMENT = re.compile(r'^"_"\s*:\s*(.*)$')

LINTED_SUFFIXES = HASH_SUFFIXES + SLASH_SUFFIXES + JSON_SUFFIXES

PRAGMA = re.compile(r"comments-check:\s*(off|on)\b")

DIRECTIVE = re.compile(r"^(?:shellcheck\s|noqa\b|pylint:|type:\s*ignore|eslint-|@ts-|<reference\s)")

SHEBANG = re.compile(r"^#!.*\b(?:ba|z|k)?sh\b|^#!.*\bpython3?\b|^#!.*\bpwsh\b")

HELP_RANGE = re.compile(r"""sed\s+-n\s+["']?(\d+),(\d+)p["']?\s+["']?\$0""")
HELP_SCAN = re.compile(r"""awk\s+'NR==1\{next\}\s*/\^#/|print_header_help\s+["']?\$0""")

TRACEABILITY = re.compile(SETTINGS.get("traceability", r"(?!)"))
DOC_REFERENCE = re.compile(r"""<(?:see|seealso|paramref|typeparamref)\s+(?:cref|langword|name|href)="([^"]*)"\s*/>|\{@link(?:code|plain)?\s+([^}|\s]+)[^}]*\}""")
DOC_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s+[\w:-]+=\"[^\"]*\")*\s*/?>|(?<!\S)@\w+|\{[^{}]*\}")

CS_CONSTANT = re.compile(
    r"\b(?:const|static\s+readonly)\s+\w+\s+(\w+)\s*=\s*(-?\d[\d_]*(?:\.\d+)?)[mMdDfFlLuU]*\s*;"
)
SCRIPT_CONSTANT = re.compile(r"\bconst\s+([A-Z][A-Z0-9_]*)\s*(?::\s*number\s*)?=\s*(-?\d[\d_]*(?:\.\d+)?)\s*[;\n]")
COMMENT_NUMBER = re.compile(r"(?<![\w.,:/-])([1-9][\d_]*(?:[.,]\d+)?|0[.,]\d+)(?![\d_%:/-]|[.,]\d)")
PROJECT_FILE = ".csproj"

MOOD_GUARD = re.compile(SETTINGS.get("moodGuard", r"(?!)"), re.IGNORECASE)

HISTORY = tuple(re.compile(pattern, re.IGNORECASE) for pattern in SETTINGS.get("history", []))
FAILURE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in SETTINGS.get("failure", []))


def tracked_files() -> list[str]:
    return [
        p for p in listed()
        if p.endswith(LINTED_SUFFIXES) or "." not in os.path.basename(p)
    ]


def family(path: str, text: str) -> str | None:
    if path.endswith(HASH_SUFFIXES):
        return "hash"
    if path.endswith(SLASH_SUFFIXES):
        return "slash"
    if path.endswith(JSON_SUFFIXES):
        return "json" if '"_"' in text else None
    head = text.splitlines()[0] if text else ""
    if "." not in os.path.basename(path) and SHEBANG.match(head):
        return "hash"
    return None


def selectable(paths: list[str]) -> list[str]:
    picked = []
    for path in paths:
        if machine_text(path) or payload(path):
            continue
        if not path.endswith(LINTED_SUFFIXES) and "." in os.path.basename(path):
            continue
        if not os.path.isfile(os.path.join(ROOT, path)):
            continue
        picked.append(path)
    return picked


def comment_lines(path: str, text: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    kind = family(path, text)
    if kind == "json":
        return manifest_comment_lines(text)
    hashed = kind == "hash"
    enabled = True
    block: str | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()

        switch = PRAGMA.search(line)
        if switch:
            enabled = switch.group(1) == "on"
            continue
        if not enabled:
            continue

        if hashed:
            if number == 1 and line.startswith("#!"):
                continue
            if line.startswith("#") and not DIRECTIVE.match(line.lstrip("#").strip()):
                found.append((number, line.lstrip("#").strip(), "plain"))
            continue

        if block:
            found.append((number, line.lstrip("*/ ").strip(), block))
            if "*/" in line:
                block = None
            continue
        if line.startswith("//") and DIRECTIVE.match(line.lstrip("/").strip()):
            continue
        if line.startswith("///"):
            found.append((number, line[3:].strip(), "doc"))
        elif line.startswith("//"):
            found.append((number, line[2:].strip(), "plain"))
        elif line.startswith("/*"):
            kind = "doc" if line.startswith("/**") else "plain"
            found.append((number, line.lstrip("/*").strip(), kind))
            if "*/" not in line[2:]:
                block = kind
    return found


def manifest_comment_lines(text: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    inside = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if inside:
            if line.startswith("]"):
                inside = False
                continue
            found.append((number, manifest_text(line), "plain"))
            continue
        match = JSON_COMMENT.match(line)
        if not match:
            continue
        rest = match.group(1).strip()
        if rest.startswith("["):
            inside = not rest.endswith(("]", "],"))
            rest = rest[1:].strip()
        body = manifest_text(rest)
        if body:
            found.append((number, body, "plain"))
    return found


def manifest_text(line: str) -> str:
    return line.strip().rstrip(",").strip().strip('"')


def trailing_comments(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")):
            continue
        quote: str | None = None
        index = 0
        while index < len(raw):
            char = raw[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in "\"'`":
                quote = char
            elif char == "/" and raw[index + 1:index + 2] == "/":
                found.append((number, raw[index + 2:].strip()))
                break
            elif char == "#" and index and raw[index - 1].isspace():
                found.append((number, raw[index + 1:].strip()))
                break
            index += 1
    return found


def pragma_lines(text: str) -> set[int]:
    closed: set[int] = set()
    enabled = True
    for number, raw in enumerate(text.splitlines(), start=1):
        switch = PRAGMA.search(raw)
        if switch:
            enabled = switch.group(1) == "on"
            closed.add(number)
            continue
        if not enabled:
            closed.add(number)
    return closed


def help_lines(text: str) -> set[int]:
    printed: set[int] = set()
    for match in HELP_RANGE.finditer(text):
        start, end = int(match.group(1)), int(match.group(2))
        if 0 < start <= end:
            printed.update(range(start, end + 1))

    if HELP_SCAN.search(text):
        for number, line in enumerate(text.splitlines()[1:], start=2):
            if not line.startswith("#"):
                break
            printed.add(number)
    return printed


def blocks(found: list[tuple[int, str, str]]) -> list[list[tuple[int, str, str]]]:
    grouped: list[list[tuple[int, str, str]]] = []
    for item in found:
        if grouped and item[0] == grouped[-1][-1][0] + 1:
            grouped[-1].append(item)
        else:
            grouped.append([item])
    return grouped


def inspect(path: str) -> list[tuple[int, str, str]]:
    with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if not family(path, text) or generated(path, text):
        return []

    found = comment_lines(path, text)
    problems: list[tuple[int, str, str]] = []
    grouped = blocks(found)

    for group in grouped:
        for index, (number, body, _kind) in enumerate(group):
            window = " ".join(item[1] for item in group[max(index - 1, 0):index + 2])
            rules = HISTORY if MOOD_GUARD.search(window) else HISTORY + FAILURE
            for pattern in rules:
                match = pattern.search(body)
                if match:
                    problems.append((number, "history", msg("comments_history_found", repr(match.group(0).strip()))))
                    break

    marked = [(number, body) for number, body, _kind in found]
    marked += trailing_comments(text)
    for number, body in marked:
        match = TRACEABILITY.search(body)
        if match:
            problems.append(
                (number, "traceability", msg("comments_traceability_found", repr(match.group(0))))
            )

    printed = help_lines(text) | pragma_lines(text)
    counted = 0
    for group in grouped:
        if sum(1 for item in group if item[0] in printed) * 2 >= len(group):
            continue
        docs = documenting(group)
        if not docs:
            counted += len(group)
        problems.extend(volume(group, docs, SETTINGS["doc" if docs else "plain"]))

    marked = {number for number, _b, _k in found}
    problems.extend(density(text, marked | printed, counted))
    problems.extend(constants(path, text, found, marked))
    problems.sort()
    return problems


def documenting(group: list[tuple[int, str, str]]) -> bool:
    return sum(1 for item in group if item[2] == "doc") * 2 >= len(group)


def volume(group: list[tuple[int, str, str]], docs: bool, limits: dict) -> list[tuple[int, str, str]]:
    parts = [doc_text(item[1]) if docs else item[1] for item in group]
    body = " ".join(part for part in parts if part)
    lines = sum(1 for part in parts if part) if docs else len(group)
    measured = (("lines", lines, msg("comments_unit_lines")), ("words", len(body.split()), msg("comments_unit_words")),
                ("chars", len(body), msg("comments_unit_chars")))
    over = [(value, name, limits[key]) for key, value, name in measured if value > limits[key]]
    if not over:
        return []
    kind = msg("comments_kind_doc") if docs else msg("comments_kind_plain")
    said = ", ".join(msg("comments_volume_item", name, value, limit) for value, name, limit in over)
    return [(group[0][0], "volume", f"{kind}: {said}")]


def doc_text(body: str) -> str:
    named = DOC_REFERENCE.sub(lambda match: (match.group(1) or match.group(2)).rpartition(".")[2], body)
    return " ".join(DOC_TAG.sub(" ", named).split())


def number(value: str) -> float:
    return float(value.replace("_", "").replace(",", "."))


def declared(text: str, pattern: re.Pattern[str]) -> dict[str, float]:
    return {name: number(value) for name, value in pattern.findall(text)}


@functools.cache
def project_folders() -> frozenset[str]:
    return frozenset(posixpath.dirname(path) for path in tracked() if path.endswith(PROJECT_FILE))


def relative(path: str) -> str:
    return os.path.relpath(os.path.join(ROOT, path), ROOT).replace(os.sep, "/")


def project_root(path: str) -> str | None:
    folder = posixpath.dirname(relative(path))
    while folder not in project_folders():
        if not folder or folder.startswith(".."):
            return None
        folder = posixpath.dirname(folder)
    return folder


@functools.cache
def project_constants(folder: str | None) -> dict[str, float]:
    if folder is None:
        return {}
    values: dict[str, float] = {}
    ambiguous: set[str] = set()
    for source in git("ls-files", "--", posixpath.join(folder, "*.cs")).splitlines():
        with open(os.path.join(ROOT, source), encoding="utf-8", errors="replace") as handle:
            for name, value in declared(handle.read(), CS_CONSTANT).items():
                if values.setdefault(name, value) != value:
                    ambiguous.add(name)
    return {name: value for name, value in values.items() if name not in ambiguous}


def constants(path: str, text: str, found: list[tuple[int, str, str]], marked: set[int]) -> list[tuple[int, str, str]]:
    if not path.endswith(CONSTANT_SUFFIXES) or TEST_PATH.search(relative(path)):
        return []
    mentioned = [
        (line, match.group(1), number(match.group(1)))
        for line, body, _kind in found
        for match in COMMENT_NUMBER.finditer(doc_text(body))
    ]
    values = {value for _line, _raw, value in mentioned if abs(value) >= SETTINGS["constantFloor"] or not value.is_integer()}
    if not values:
        return []
    code = "\n".join(line for index, line in enumerate(text.splitlines(), start=1) if index not in marked)
    if path.endswith(".cs"):
        known = {**project_constants(project_root(path)), **declared(text, CS_CONSTANT)}
        visible = {
            name: value for name, value in known.items()
            if value in values and re.search(rf"\b{re.escape(name)}\b", code)
        }
    else:
        visible = {name: value for name, value in declared(code, SCRIPT_CONSTANT).items() if value in values}
    by_value: dict[float, list[str]] = {}
    for name, value in visible.items():
        by_value.setdefault(value, []).append(name)
    problems: list[tuple[int, str, str]] = []
    reported: set[int] = set()
    for line, raw, value in mentioned:
        names = by_value.get(value)
        if names and line not in reported:
            reported.add(line)
            problems.append((line, "constant", msg("comments_constant_found", raw, ", ".join(sorted(names)))))
    return problems


def density(text: str, marked: set[int], counted: int) -> list[tuple[int, str, str]]:
    code = sum(1 for number, line in enumerate(text.splitlines(), start=1)
               if line.strip() and number not in marked)
    if not code or counted < SETTINGS["density"]["floor"] or counted <= code * SETTINGS["density"]["max"]:
        return []
    return [(1, "density", msg("comments_density", counted, code, int(SETTINGS["density"]["max"] * 100)))]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="все отслеживаемые файлы под стандартом")
    scope.add_argument("--files", nargs="+", metavar="PATH", help="явный список файлов")
    parser.add_argument("--base", help="база сравнения для режима изменений")
    parser.add_argument("--worktree", action="store_true", help="добавить незакоммиченные изменения")
    args = parser.parse_args()

    if not RULES:
        say(msg("comments_unset"))
        return 0
    if MISSING:
        say(msg("comments_rules_incomplete", ", ".join(MISSING)))
        return 1
    if args.all:
        candidates = tracked_files()
    elif args.files:
        candidates = args.files
    else:
        base = base_ref(args.base)
        if not base:
            say(msg("gate_no_base"))
            return 0
        candidates = changed_files(base, args.worktree)

    files = selectable(candidates)
    if not files:
        return 0

    findings = {path: problems for path in files if (problems := inspect(path))}
    for path in sorted(findings):
        for number, rule, message in findings[path]:
            print(f"{path}:{number}: {rule}: {message}")

    if not findings:
        say(msg("gate_clean", len(files)))
        return 0

    say(msg("comments_failed", len(findings), STANDARD))
    return 1


def say(message: str) -> None:
    print(f"check-comments: {message}", file=sys.stderr)
