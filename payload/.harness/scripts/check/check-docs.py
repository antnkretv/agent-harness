#!/usr/bin/env python3
"""Проверки областей `docs` и `agents` и проходы по всему дереву.

    ./.harness/hooks/python.cmd .harness/scripts/check/check-docs.py --area docs
    ./.harness/hooks/python.cmd .harness/scripts/check/check-docs.py --area agents
    ./.harness/hooks/python.cmd .harness/scripts/check/check-docs.py --anchors

Контракты: docs/platform/architecture/area-map.md и
.harness/docs/root-entry-docs.md.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
import agent_links  # noqa: E402
from ci_common import (  # noqa: E402
    LIVE_DOC_PREFIXES,
    MACHINE_TEXT_PREFIXES,
    ROOT,
    CODE_SPAN,
    app_prefix,
    generated_path,
    get,
    get_list,
    get_records,
    heading_anchor,
    kit_owned,
    msg,
    rules,
    tracked as listed,
)

SKILLS_DIR = ".agents/skills"
SKILLS_INDEX = SKILLS_DIR + "/README.md"
PROJECT_SKILLS_HEADING = re.compile(r"^## Проектные скиллы$", re.M)
RULES_REGISTRY = ".harness/methodology.json"
HOOK_IMPLEMENTATIONS_DIR = ".harness/hooks"
AGENT_DIRS = (".claude/", ".codex/", ".cursor/")
HOOK_ADAPTER_DIRS = tuple(directory + "hooks" for directory in AGENT_DIRS)
RULE_ADAPTER_DIRS = tuple(directory + "rules" for directory in AGENT_DIRS)
ADAPTER_MAX_BYTES = 1024
MATCHER_KEYS = ("paths", "globs")
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
LIST_ITEM = re.compile(r"^\s*-\s+(.*)$")

MAX_BYTES = 1024 * 1024
INDEX = get("docs.index", "docs/README.md")
DOCS_DIR = INDEX.rsplit("/", 1)[0] + "/" if "/" in INDEX else ""
SIZE_EXEMPT_PREFIXES = (DOCS_DIR + "assets/", DOCS_DIR + "legal/")
TOOLING_PREFIXES = (".github/", ".claude/", ".codex/", ".cursor/", ".agents/", ".specify/")

VENDORED_SKILLS = (
    "speckit-analyze", "speckit-checklist",
    "speckit-constitution", "speckit-implement", "speckit-plan",
    "speckit-specify", "speckit-tasks",
    "speckit-converge",
) + tuple(get_list("docs.vendoredSkills", []))

SECRET_PATTERNS = (
    r"\.kdbx$", r"\.env$", r"(^|/)\.env\.", r"creds", r"recovery",
    r"учетки", r"password",
)

INSTRUCTION_NAMES = ("README.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "REVIEW.md")
CENTRAL_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "REVIEW.md")
KIT_INSTRUCTIONS = tuple(".harness/" + name for name in CENTRAL_INSTRUCTION_NAMES)
INSTRUCTION_ROOTS = tuple(get_list("docs.instructionRoots", ("",)))

LIMITS_ROOT = {
    "README.md": {"lines": 25, "words": 350},
    "AGENTS.md": {"lines": 60, "words": 900},
    "CLAUDE.md": {"lines": 30, "words": 350},
    "REVIEW.md": {"lines": 120, "words": 1300},
}
MAX_INSTRUCTION_LINE = 300

STATUS_LEXICON = (
    (r"ЗАКРЫТ[АО]?\b", msg("docs_status_closed")),
    (r"\b\d{1,3}/\d{1,3}\b(?!\s*(строк|символ|МБ|Мб))", msg("docs_status_count")),
    (r"смержен[оа]\s+в\s+`?develop", msg("docs_status_merged")),
    (r"[Оо]статок:\s*T\d+", msg("docs_status_remainder")),
)

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FENCE = re.compile(r"```.*?```", re.S)
EXTERNAL = re.compile(r"^(https?:|mailto:|#)")


def tracked() -> list[str]:
    return [p for p in listed() if os.path.exists(p)]


@functools.cache
def readable(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def prose(text: str) -> str:
    return CODE_SPAN.sub("", FENCE.sub("", text))


def is_instruction(path: str) -> bool:
    for root in INSTRUCTION_ROOTS:
        if path.startswith(root) and path[len(root):] in INSTRUCTION_NAMES:
            return True
    return False


def is_vendored(path: str) -> bool:
    parts = path.split("/")
    return any(p in VENDORED_SKILLS for p in parts)


APP_ROOTS = tuple(prefix for prefix in map(app_prefix, get_list("apps[].path", [])) if prefix)


def in_docs_area(path: str) -> bool:
    if path.startswith(("docs/", "specs/") + LIVE_DOC_PREFIXES):
        return True
    if "/" not in path and path.endswith(".md"):
        return True
    for root in (root for root in APP_ROOTS if path.startswith(root)):
        rest = path[len(root):]
        if rest.startswith("docs/") or ("/" not in rest and rest.endswith(".md")):
            return True
    return False


def platform_document(path: str) -> bool:
    return path.startswith(".harness/") and path.endswith(".md")


def in_agents_area(path: str) -> bool:
    return path.startswith((".claude/", ".codex/", ".specify/", ".cursor/", ".agents/", ".harness/hooks/"))


def rooted(base: str, target: str) -> str:
    return target[1:] if target.startswith("/") else os.path.join(base, target)


def resolved_links(path: str) -> list[tuple[str, str, str]]:
    base = os.path.dirname(path)
    links = []
    for target in LINK.findall(prose(readable(os.path.join(ROOT, path)))):
        target = target.strip()
        if EXTERNAL.match(target):
            continue
        relative, _, anchor = target.partition("#")
        if not relative:
            continue
        resolved = os.path.normpath(rooted(base, relative)).replace(os.sep, "/")
        links.append((target, resolved, anchor))
    return links


def check_links(files: list[str]) -> list[str]:
    problems = []
    for path in (f for f in files if f.endswith((".md", ".mdc"))):
        vendored = is_vendored(path)
        for target, resolved, _ in resolved_links(path):
            if not (vendored and resolved.startswith(".agents/")) and not os.path.exists(resolved):
                problems.append(f"{path} => {target}")
    return problems


RETIRED_SPEC = re.compile(r"specs/((?:\d{3}|\d{8}-\d{6})-[a-z0-9-]+)")
SCANNED_SUFFIXES = (".md", ".py", ".sh", ".json", ".yml", ".yaml", ".cs", ".ts", ".tsx", ".js", ".mjs", ".txt")


def check_retired_specs(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        if is_vendored(path) or not path.endswith(SCANNED_SUFFIXES):
            continue
        try:
            text = readable(path)
        except (UnicodeDecodeError, OSError):
            continue
        seen: dict[str, int] = {}
        for match in RETIRED_SPEC.finditer(text):
            seen.setdefault(match.group(1), text[: match.start()].count("\n") + 1)
        for slug, line in seen.items():
            if not os.path.isdir(os.path.join(ROOT, "specs", slug)):
                problems.append(f"{path}:{line} => specs/{slug}")
    return problems


ROOT_ENTRY_DOCS = ("README.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "llms.txt")
MERMAID_DIAGRAM_TYPES = (
    "graph", "flowchart", "sequenceDiagram", "classDiagram", "stateDiagram-v2", "stateDiagram",
    "erDiagram", "journey", "gantt", "pie", "mindmap", "timeline",
    "C4Context", "C4Container", "C4Component", "gitGraph", "quadrantChart",
)
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
FENCE_LINE = re.compile(r"^[ \t]*(```|~~~)")


def mermaid_documents(every: list[str]) -> list[str]:
    files = [f for f in every if f.startswith(DOCS_DIR) and f.endswith(".md")]
    files += [f for f in ROOT_ENTRY_DOCS if os.path.exists(os.path.join(ROOT, f))]
    return sorted(set(files))


def anchor_documents(every: list[str], kit: bool = False) -> list[str]:
    owned = set() if kit else kit_owned()
    return [f for f in every if (f.endswith((".md", ".mdc")) or f in ROOT_ENTRY_DOCS)
            and not is_vendored(f) and not generated_path(f) and f not in owned]


def strip_fences(text: str) -> list[str]:
    out, inside = [], False
    for line in text.splitlines():
        if FENCE_LINE.match(line):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else line)
    return out


@functools.cache
def heading_slugs(path: str) -> set[str]:
    slugs, seen = set(), {}
    for match in HEADING.finditer("\n".join(strip_fences(readable(path)))):
        base = heading_anchor(match.group(2))
        if not base:
            continue
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")
    return slugs


def check_anchors(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        base = os.path.dirname(path)
        for target in LINK.findall(prose(readable(path))):
            target = target.strip()
            if target.startswith(("http:", "https:", "mailto:", "tel:")):
                continue
            head, _, anchor = target.partition("#")
            anchor = unquote(anchor.split("?")[0])
            if not anchor:
                continue
            if head:
                resolved = os.path.normpath(rooted(base, unquote(head.split("?")[0])))
                if not resolved.endswith(".md") or not os.path.exists(resolved):
                    continue
            else:
                resolved = path
            if anchor.lower() not in heading_slugs(resolved):
                where = msg("docs_same_file") if resolved == path else resolved
                problems.append(msg("docs_anchor_missing", path, target, anchor, where))
    return problems


def check_mermaid(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        lines, inside, start, body = readable(path).splitlines(), False, 0, []
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not inside and stripped.startswith("```") and stripped[3:].strip().lower() == "mermaid":
                inside, start, body = True, number, []
                continue
            if inside and stripped.startswith("```"):
                inside = False
                first = next((b for b in body if b.strip()), None)
                if first is None:
                    problems.append(msg("docs_mermaid_empty", path, start))
                elif not starts_with_diagram_type(first.strip()):
                    problems.append(
                        msg("docs_mermaid_no_type", path, start, first.strip()))
                continue
            if inside:
                body.append(line)
    return problems


def starts_with_diagram_type(first: str) -> bool:
    for kind in MERMAID_DIAGRAM_TYPES:
        if first.startswith(kind) and (len(first) == len(kind) or not first[len(kind)].isalnum()):
            return True
    return False


def check_index(files: list[str]) -> list[str]:
    documents = {
        f for f in files
        if f.startswith("docs/") and f.endswith((".md", ".txt"))
        and not f.startswith(tuple("docs/" + p for p in TOOLING_PREFIXES))
    }
    reachable: set[str] = set()
    queue = [INDEX]
    while queue:
        current = queue.pop()
        if current in reachable or not os.path.exists(current):
            continue
        reachable.add(current)
        if not current.endswith(".md"):
            continue
        queue += [resolved for _, resolved, _ in resolved_links(current) if resolved.startswith("docs/")]
    return sorted(documents - reachable)


DOC_TYPE_WORDS = frozenset((
    "adr", "analysis", "brief", "catalog", "checklist", "comparison", "criteria", "criterion",
    "glossary", "guide", "howto", "matrix", "overview", "reference", "research", "runbook",
    "spec", "specification", "standard", "standards", "summary", "tutorial", "workflow",
))
KEBAB_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ADR_NAME = re.compile(r"^\d{4}-")
PLATFORM_DIR = DOCS_DIR + "platform/"
ADR_DIR = PLATFORM_DIR + "adr/"
TYPED_PREFIXES = tuple(get_list("docs.typedPrefixes", (DOCS_DIR,)))
UNTYPED_PREFIXES = (ADR_DIR, *MACHINE_TEXT_PREFIXES, *get_list("docs.historicalPrefixes", ()))
DOC_TYPE = re.compile(r"Обучение|Практическое руководство|Справочник|Объяснение")
TYPE_LINE = re.compile(r"^> \*\*Тип \(Diataxis\):\*\* (.+?)(?:\s+-\s.*)?\s*$")
DATED_NAME = re.compile(r"-\d{4}-\d{2}(?:-\d{2})?\.md$")
ADR_HEADER_FIELD = re.compile(r"^- ([^:]+):")
ADR_HEADER_FIELDS = re.compile(r"Статус|Дата|Область")


def check_names(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        if not (path.startswith(DOCS_DIR) or "/docs/" in path):
            continue
        folder, name = os.path.split(path)
        if name.startswith(".") or name in INSTRUCTION_NAMES:
            continue
        stem = name.split(".", 1)[0]
        if not KEBAB_NAME.match(stem):
            problems.append(msg("docs_name_kebab", path))
            continue
        if not name.endswith(".md"):
            continue
        if folder + "/" == PLATFORM_DIR:
            problems.append(msg("docs_name_topic", path, PLATFORM_DIR))
        if path.startswith(ADR_DIR) and not ADR_NAME.match(stem):
            problems.append(msg("docs_name_adr", path))
        words = sorted(DOC_TYPE_WORDS.intersection(stem.split("-")))
        if words:
            problems.append(msg("docs_name_type", path, ", ".join(words)))
        parent = os.path.basename(folder)
        if (stem + "-").startswith(parent + "-"):
            problems.append(msg("docs_name_folder", path, parent))
    return problems


def check_adr_headers(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        if not (path.startswith(ADR_DIR) and ADR_NAME.match(os.path.basename(path))):
            continue
        for line in readable(path).splitlines():
            if line.startswith("## "):
                break
            field = ADR_HEADER_FIELD.match(line)
            if field and not ADR_HEADER_FIELDS.fullmatch(field.group(1)):
                problems.append(msg("docs_adr_header_field", path, field.group(1)))
    return problems


def declared_type(path: str) -> str | None:
    lines = iter(readable(path).splitlines())
    for line in lines:
        if line.startswith("# "):
            break
    for line in lines:
        if line.strip():
            match = TYPE_LINE.match(line)
            return match.group(1) if match else None
    return None


def check_types(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        if not path.endswith(".md") or not path.startswith(TYPED_PREFIXES):
            continue
        declared = declared_type(path)
        if declared is None:
            exempt = (path.startswith(UNTYPED_PREFIXES) or os.path.basename(path) == "README.md"
                      or DATED_NAME.search(path))
            if not exempt:
                problems.append(msg("docs_type_missing", path))
        elif not DOC_TYPE.fullmatch(declared):
            problems.append(msg("docs_type_unknown", path, declared))
    return problems


def check_size(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        if path.startswith(SIZE_EXEMPT_PREFIXES):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > MAX_BYTES:
            problems.append(msg("docs_file_too_big", path, format(size / MAX_BYTES, ".2f")))
    return problems


def check_secrets(files: list[str]) -> list[str]:
    return [f for f in files if any(re.search(p, f, re.I) for p in SECRET_PATTERNS)]


SPEC_SUFFIXES = (".md", ".txt", ".json")


def check_spec_formats(files: list[str]) -> list[str]:
    return [f for f in files if f.startswith("specs/") and not f.endswith(SPEC_SUFFIXES)]


def check_instruction_limits(files: list[str]) -> list[str]:
    problems = []
    for path in (f for f in files if is_instruction(f)):
        name = os.path.basename(path)
        limits = LIMITS_ROOT.get(name)
        if not limits:
            continue
        text = readable(path)
        lines = text.splitlines()
        words = len(re.findall(r"[\w-]+", text, re.UNICODE))
        if len(lines) > limits["lines"]:
            problems.append(msg("docs_lines_over", path, len(lines), limits['lines']))
        if words > limits["words"]:
            problems.append(msg("docs_words_over", path, words, limits['words']))
        for number, line in enumerate(lines, 1):
            if len(line) > MAX_INSTRUCTION_LINE:
                problems.append(msg("docs_line_too_long", path, number, len(line), MAX_INSTRUCTION_LINE))
    return problems


def check_instruction_locations(files: list[str]) -> list[str]:
    return [
        path for path in files
        if "/" in path and os.path.basename(path) in CENTRAL_INSTRUCTION_NAMES and path not in KIT_INSTRUCTIONS
    ]


def check_status_lexicon(files: list[str]) -> list[str]:
    problems = []
    for path in (f for f in files if is_instruction(f)):
        text = prose(readable(path))
        for pattern, why in STATUS_LEXICON:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{path}:{line} - {why}: {match.group(0)!r}")
    return problems


def check_asset_duplicates(files: list[str]) -> list[str]:
    seen: dict[str, list[str]] = {}
    for path in files:
        match = re.search(r"(?:^|/)skills/([^/]+)/SKILL\.md$", path)
        if match:
            seen.setdefault(match.group(1), []).append(path)
    return [f"{name}: {', '.join(paths)}" for name, paths in sorted(seen.items()) if len(paths) > 1]


def check_hook_adapters() -> list[str]:
    problems = []
    implementations = os.path.join(ROOT, HOOK_IMPLEMENTATIONS_DIR)
    if not os.path.isdir(implementations):
        return problems

    names = set(os.listdir(implementations))
    names_by_stem = {os.path.splitext(name)[0]: name for name in names}
    directories = list(HOOK_ADAPTER_DIRS)
    for app in get_list("apps[].path", []):
        directories.extend(app_prefix(app) + directory for directory in HOOK_ADAPTER_DIRS if app_prefix(app))

    for directory in directories:
        root = os.path.join(ROOT, directory)
        if not os.path.isdir(root):
            continue
        for adapter_name in os.listdir(root):
            path = os.path.join(root, adapter_name)
            if not os.path.isfile(path):
                continue
            name = adapter_name if adapter_name in names else names_by_stem.get(os.path.splitext(adapter_name)[0])
            if not name:
                continue
            relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if os.path.getsize(path) > ADAPTER_MAX_BYTES:
                problems.append(msg("docs_adapter_too_big", relative, ADAPTER_MAX_BYTES))
                continue
            text = readable(path)
            if ".harness" not in text or "hooks" not in text or name not in text:
                problems.append(msg("docs_adapter_no_call", relative, HOOK_IMPLEMENTATIONS_DIR, name))
    return problems


def split_globs(value: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in value:
        depth += (char == "{") - (char == "}")
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip().strip("\"'") for part in parts if part.strip()]


def rule_matchers(relative: str) -> dict[str, set[str]]:
    matchers: dict[str, set[str]] = {key: set() for key in MATCHER_KEYS}
    block = FRONT_MATTER.match(readable(os.path.join(ROOT, relative)))
    key = ""
    for line in block.group(1).splitlines() if block else []:
        item = LIST_ITEM.match(line)
        if item:
            if key in matchers:
                matchers[key].update(split_globs(item.group(1)))
            continue
        name, separator, value = line.partition(":")
        if separator:
            key = name.strip()
            if key in matchers:
                matchers[key].update(split_globs(value))
    return matchers


def rule_targets(relative: str) -> set[str]:
    return {
        f"{resolved}#{anchor}" if anchor else resolved
        for _, resolved, anchor in resolved_links(relative)
    }


def check_rules() -> list[str]:
    problems = []
    for directory in RULE_ADAPTER_DIRS:
        root = os.path.join(ROOT, directory)
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            if name.endswith((".md", ".mdc")) and not name.startswith(agent_links.PREFIX):
                problems.append(msg("docs_rule_outside", f"{directory}/{name}", agent_links.RULES))
    root = os.path.join(ROOT, agent_links.RULES)
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        path = f"{agent_links.RULES}/{name}"
        if not name.endswith(".md") or not os.path.isfile(os.path.join(ROOT, path)):
            continue
        if os.path.getsize(os.path.join(ROOT, path)) > ADAPTER_MAX_BYTES:
            problems.append(msg("docs_adapter_too_big", path, ADAPTER_MAX_BYTES))
        matchers = rule_matchers(path)
        if not all(matchers.values()):
            problems.append(msg("docs_rule_no_matcher", path, ", ".join(MATCHER_KEYS)))
        elif len({frozenset(value) for value in matchers.values()}) > 1:
            problems.append(msg("docs_rule_matchers_differ", path, "; ".join(
                f"{key} => {', '.join(sorted(value))}" for key, value in matchers.items())))
        targets = rule_targets(path)
        if not targets:
            problems.append(msg("docs_rule_no_source", path))
        named = sorted(target for target in targets if target.startswith(AGENT_DIRS))
        if named:
            problems.append(msg("docs_rule_agent_dir", path, ", ".join(named)))
    return problems


def check_skills_registered() -> list[str]:
    index = os.path.join(ROOT, SKILLS_INDEX)
    registry = os.path.join(ROOT, RULES_REGISTRY)
    if not os.path.exists(index) or not os.path.exists(registry):
        return []

    with open(index, encoding="utf-8") as handle:
        parts = PROJECT_SKILLS_HEADING.split(handle.read(), 1)
    if len(parts) < 2:
        return []
    section = re.split(r"^## ", parts[1], maxsplit=1, flags=re.M)[0]

    listed = {
        os.path.normpath(os.path.join(SKILLS_DIR, link)).replace(os.sep, "/")
        for link in re.findall(r"\[[^\]]+\]\(([^)]+SKILL\.md)\)", section)
    }

    modules = rules()
    if not modules:
        return [msg("docs_registry_empty", RULES_REGISTRY)]
    declared = {module.get("doc") for module in modules if module.get("doc")}

    return sorted(path for path in listed if path not in declared)


def report(title: str, problems: list[str], hint: str) -> bool:
    if not problems:
        print(f"  OK   {title}")
        return True
    print(f"  FAIL {title}")
    for item in problems:
        print(f"         {item}")
    print(f"         => {hint}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", choices=["docs", "agents"])
    parser.add_argument("--retired-specs", action="store_true",
                        help="только пути снятых каталогов спек; областью не отбирается")
    parser.add_argument("--anchors", action="store_true",
                        help="только ссылки и их якоря во всех отслеживаемых Markdown; областью не отбирается")
    parser.add_argument("--kit-files", action="store_true",
                        help="якоря и в файлах комплекта: их сверяет kit.py check в репозитории комплекта")
    args = parser.parse_args()
    if not args.area and not args.retired_specs and not args.anchors:
        parser.error(msg("docs_need_area"))

    every = tracked()
    passed = []
    if args.retired_specs:
        passed.append(report(msg("docs_title_retired"), check_retired_specs(every),
                             msg("docs_hint_retired")))
    if args.anchors:
        documents = anchor_documents(every, args.kit_files)
        passed.append(report(msg("docs_title_links"), check_links(documents), msg("docs_hint_links")))
        passed.append(report(msg("docs_title_anchors"), check_anchors(documents), msg("docs_hint_anchors")))
    if args.area == "docs":
        files = [f for f in every if in_docs_area(f)]
        print(msg("docs_area_docs", len(files)) + "\n")
        conventions = {
            "index": lambda: report(msg("docs_title_index"), check_index(files), msg("docs_hint_index", INDEX)),
            "names": lambda: report(msg("docs_title_names"), check_names(files), msg("docs_hint_names")),
            "adr-headers": lambda: report(msg("docs_title_adr_header"), check_adr_headers(files),
                                          msg("docs_hint_adr_header")),
            "types": lambda: report(msg("docs_title_types"), check_types(every), msg("docs_hint_types")),
            "status-lexicon": lambda: report(msg("docs_title_status"), check_status_lexicon(files),
                                             msg("docs_hint_status")),
            "size": lambda: report(msg("docs_title_size"), check_size(files), msg("docs_hint_size")),
        }
        chosen = get_list("docs.checks", [])
        unknown = sorted(set(chosen) - set(conventions))
        if unknown:
            print(msg("docs_checks_unknown", ", ".join(unknown), ", ".join(conventions)))
            passed.append(False)
        passed += [conventions[name]() for name in chosen if name in conventions]
        passed += [
            report(msg("docs_title_links"), check_links(files + [f for f in every if platform_document(f)]),
                   msg("docs_hint_links")),
            report(msg("docs_title_limits"), check_instruction_limits(files),
                   msg("docs_hint_limits")),
            report(msg("docs_title_root_only"), check_instruction_locations(every),
                   msg("docs_hint_root_only")),
            report(msg("docs_title_secrets"), check_secrets(files),
                   msg("docs_hint_secrets")),
            report(msg("docs_title_spec_formats"), check_spec_formats(files),
                   msg("docs_hint_spec_formats")),
            report(msg("docs_title_mermaid"), check_mermaid(mermaid_documents(every)),
                   msg("docs_hint_mermaid")),
        ]
    elif args.area == "agents":
        files = [f for f in every if in_agents_area(f)]
        print(msg("docs_area_agents", len(files)) + "\n")
        passed += [
            report(msg("docs_title_assets"), check_asset_duplicates(every),
                   msg("docs_hint_assets")),
            report(msg("docs_title_adapters"), check_hook_adapters(),
                   msg("docs_hint_adapters")),
            report(msg("docs_title_links"), check_links(files),
                   msg("docs_hint_agent_links")),
            report(msg("docs_title_rules"), check_rules(),
                   msg("docs_hint_rules")),
            report(msg("docs_title_skills"), check_skills_registered(),
                   msg("docs_hint_skills")),
        ]

    print()
    if all(passed):
        print(msg("docs_passed"))
        return 0
    print(msg("docs_failed"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
