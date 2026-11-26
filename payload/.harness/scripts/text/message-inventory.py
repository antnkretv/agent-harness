#!/usr/bin/env python3
"""Выгрузка сообщений всех реестров одной таблицей для инвентаризации.

Строка таблицы - одно сообщение: реестр, ключ, объявленный уровень, места
вызова, признаки и предложенная группа необходимости. Группы, уровни и смысл
признаков задает раздел "Необходимость и уровень сообщения" стандарта текста
(docs/platform/standards/text.md); порядок разбора - скилл log-inventory.

Реестры:

- `messages.env` и блоки `MSG_*` скриптов: уровень выводится из определения
  хелпера вызова - завершение с ненулевым кодом, вывод в stderr или в stdout;
- `LogMessages.cs`: уровень `[LoggerMessage]`, блок вызова ищется по скобкам;
- `ErrorMessages.cs`: отказ операции, текст для человека;
- словари интерфейса из `prose/vale.localeDictionaries` манифеста.

Ожидаемый уровень предсказывается признаками статистических моделей выбора
уровня: хелпер вызова, слова шаблона, обработчик ошибки и цикл вокруг вызова.
Признак - кандидат на разбор, а не вердикт: таблица ничего не правит и не
блокирует.

    ./.harness/hooks/python.cmd .harness/scripts/text/message-inventory.py [--path <префикс>...] [--summary]
"""

from __future__ import annotations

import argparse
import ast
import os
import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, msg, payload  # noqa: E402
from code_text import FRONTEND_ROOTS, frontend_code, registry_entries  # noqa: E402
from manifest import setting  # noqa: E402
from shell_text import MSG_LINE  # noqa: E402
from text_primitives import is_shell, line_of  # noqa: E402
from text_scan import read, tracked, uses  # noqa: E402

COLUMNS = ("tier", "surface", "level", "expected", "uses", "signals", "key", "where", "template")

LEVELS = ("verbose", "info", "warning", "error")

CODE_REGISTRIES = ("LogMessages.cs", "ErrorMessages.cs")

PART_HELPERS = ("msg_label", "msg_template", "msg_text", "msg", "row", "wait_for", "wait_labeled")
SHELL_FUNCTION = re.compile(r"^([ \t]*)(?:function[ \t]+)?([A-Za-z_][\w-]*)[ \t]*\(\)[ \t]*\{(.*)$")
PY_LEVELS = {"die": "error", "fail": "error", "SystemExit": "error", "error": "error",
             "warn": "warning", "warning": "warning", "debug": "verbose",
             "say": "info", "print": "info", "log": "info"}
PS_LEVELS = (("error", re.compile(r"\bthrow\b|Write-Error|-Failure\b|\bexit\s+[1-9]")),
             ("warning", re.compile(r"Write-Warning")),
             ("verbose", re.compile(r"Write-Verbose|Write-Debug")))
CS_LEVELS = {"Trace": "verbose", "Debug": "verbose", "Information": "info",
             "Warning": "warning", "Error": "error", "Critical": "error"}

FAILURE = re.compile(r"(?i)не удал|ошибк|отказ|сбой|упал|недоступ|не найден|не задан|не совпада|"
                     r"расход|нарушен|невер|запрещ|прерв|не прош|не ответ|не подтвержд|"
                     r"отклон|предупрежд|не отвеч|таймаут|timeout|failed|error|denied|missing|refused")
SUCCESS = re.compile(r"(?i)^(?!.*\bне\b).*(?:готово|успешно|собран|создан|запущен|начат|"
                     r"заверш|обновлен|применен|поднят|включен|\bok\b|\bdone\b|started|finished)")
PLACEHOLDER = re.compile(r"%s|\{[^{}]*\}|\{\{[^{}]*\}\}")

LOGGER = re.compile(r"\[LoggerMessage\((?P<args>.*?)\)\]\s*(?:(?:internal|public|private|static|partial)\s+)*"
                    r"void\s+(?P<name>\w+)\s*\((?P<params>[^)]*)\)", re.S)
CS_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
ERROR_ENTRY = re.compile(r'\[(?P<codes>\w+)\.(?P<name>\w+)\]\s*=\s*(?P<text>"(?:[^"\\]|\\.)*"(?:\s*\+\s*"(?:[^"\\]|\\.)*")*)')
CS_IDENTIFIER = re.compile(r"\b(\w+(?:\.\w+)?)\b(?!\s*\]\s*=)")
LOOP_OPEN = re.compile(r"^\s*(?:for|while|until)\b.*\bdo\b|^\s*do\s*$")
LOOP_CLOSE = re.compile(r"(?:^|[;\s])done\b")
CS_BLOCK = re.compile(r"\b(catch|foreach|for|while)\b")
UI_LITERAL = re.compile(r"""['"`]([A-Za-z0-9_.:-]+)['"`]""")
UI_PREFIX = re.compile(r"""[`'"]([A-Za-z0-9_.-]+)\.(?:\$\{|['"`]\s*\+)""")
UI_SOURCES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".astro", ".vue", ".html")
PLURAL = re.compile(r"_(?:zero|one|two|few|many|other)$")


@dataclass
class Message:
    surface: str
    registry: str
    line: int
    key: str
    template: str
    level: str = ""
    params: str = ""
    sites: list[tuple[str, int, str, set[str]]] = field(default_factory=list)
    hits: int = 0
    around: set[str] = field(default_factory=set)
    signals: set[str] = field(default_factory=set)
    expected: str = ""
    tier: str = ""


def shell_helpers(files: dict[str, str]) -> dict[str, str]:
    bodies: dict[str, list[str]] = defaultdict(list)
    for path, text in files.items():
        if not is_shell(path, text):
            continue
        lines = text.split("\n")
        for index, line in enumerate(lines):
            m = SHELL_FUNCTION.match(line)
            if not m:
                continue
            indent, name, rest = m.groups()
            body = [rest]
            if not rest.rstrip().endswith("}"):
                for tail in lines[index + 1:]:
                    if tail.rstrip() == indent + "}":
                        break
                    body.append(tail)
            bodies[name].append("\n".join(body))

    def own(body: str) -> str:
        if re.search(r"\b(?:VERBOSE|DEBUG)\b", body):
            return "verbose"
        if re.search(r"\bexit\s+(?:[1-9]|\"?\$)", body):
            return "error"
        if ">&2" in body:
            return "warning"
        return "info" if re.search(r"\b(?:printf|echo)\b", body) else ""

    levels = {name: max((own(b) for b in found), key=lambda x: LEVELS.index(x) if x else -1)
              for name, found in bodies.items()}
    for _ in range(3):
        for name, found in bodies.items():
            if levels[name] == "verbose":
                continue
            called = {levels.get(word, "") for body in found for word in re.findall(r"[A-Za-z_][\w-]*", body)
                      if word != name}
            best = max(called | {levels[name]}, key=lambda x: LEVELS.index(x) if x else -1)
            levels[name] = best
    return {name: level for name, level in levels.items() if level}


def shell_level(line: str, column: int, helpers: dict[str, str]) -> str:
    before = line[:column]
    for word in reversed(re.findall(r"[A-Za-z_][\w-]*", before)):
        if word in PART_HELPERS:
            return "part" if word != "msg" or "$(msg" in before[-8:] else "info"
        if word in helpers:
            return helpers[word]
    return "info"


def shell_loops(text: str) -> set[int]:
    inside, depth = set(), 0
    for number, line in enumerate(text.split("\n"), 1):
        code = line.split("#", 1)[0]
        if LOOP_OPEN.search(code):
            depth += 1
        if depth:
            inside.add(number)
        if LOOP_CLOSE.search(code) and depth:
            depth -= 1
    return inside


def python_sites(text: str) -> list[tuple[str, int, str, set[str]]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        args = [arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
                for arg in node.args[:2]]
        if name == "annotation" and len(args) == 2 and args[1]:
            key, level = args[1], args[0] if args[0] in ("error", "warning") else "info"
        elif name in ("die", "fail", "warn", "say", "msg") and args[0]:
            key, level = args[0], PY_LEVELS.get(name, "")
        else:
            continue
        context, current = set(), node
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.For, ast.While, ast.comprehension)):
                context.add("loop")
            elif isinstance(current, ast.ExceptHandler):
                context.add("handler")
            elif isinstance(current, ast.Raise) and not level:
                level = "error"
            elif isinstance(current, ast.Call) and not level:
                stderr = any(kw.arg == "file" and "stderr" in ast.unparse(kw.value) for kw in current.keywords)
                level = "warning" if stderr else PY_LEVELS.get(
                    getattr(current.func, "id", getattr(current.func, "attr", "")), "")
        found.append((key, node.lineno, level or "part", context))
    return found


def script_sites(path: str, text: str, helpers: dict[str, str]) -> list[tuple[str, int, str, set[str]]]:
    if path.endswith(".py"):
        return python_sites(text)
    found = uses(path, text)
    if not found:
        return []
    lines = text.split("\n")
    loops = shell_loops(text) if not path.endswith(".ps1") else set()
    sites = []
    for key, number in found:
        line = lines[number - 1] if number <= len(lines) else ""
        column = max(line.find("@" + key), line.find(key))
        if path.endswith(".ps1"):
            level = next((name for name, pattern in PS_LEVELS if pattern.search(line[:column])), "info")
        else:
            level = shell_level(line, column, helpers)
        sites.append((key, number, level, {"loop"} if number in loops else set()))
    return sites


def script_inventory(files: dict[str, str], registries: list[str]) -> list[Message]:
    messages = {}
    for path in registries:
        text = files[path]
        for m in MSG_LINE.finditer(text):
            messages[m.group(1)] = Message("script", path, line_of(text, m.start()), m.group(1), m.group(2))
    helpers = shell_helpers(files)
    for path, text in files.items():
        if path.endswith("messages.env"):
            continue
        for key, number, level, context in script_sites(path, text, helpers):
            if key in messages:
                messages[key].sites.append((path, number, level, context))
    for message in messages.values():
        levels = {site[2] for site in message.sites}
        standalone = [level for level in levels if level != "part"]
        message.level = max(standalone, key=LEVELS.index) if standalone else ("part" if levels else "")
    return list(messages.values())


def cs_concat(text: str) -> str:
    return "".join(bytes(part, "utf-8").decode("unicode_escape") if "\\" in part else part
                   for part in CS_STRING.findall(text))


def statement_head(lines: list[str], number: int) -> list[str]:
    head = [lines[number]]
    for back in range(number - 1, max(-1, number - 10), -1):
        if lines[back].rstrip().endswith((";", "{", "}")):
            break
        head.append(lines[back])
    return head


def cs_context(lines: list[str], index: int) -> set[str]:
    depth, context = 0, set()
    for number in range(index, max(-1, index - 200), -1):
        line = lines[number]
        depth += line.count("}") - line.count("{") if number != index else 0
        if depth < 0:
            block = CS_BLOCK.search(" ".join(statement_head(lines, number)))
            if block:
                context.add("handler" if block.group(1) == "catch" else "loop")
            depth = 0
    return context


def backend_inventory(files: dict[str, str], registries: list[str]) -> list[Message]:
    found, by_name = [], defaultdict(list)
    for path in registries:
        text = files[path]
        if posixpath.basename(path) == "LogMessages.cs":
            for m in LOGGER.finditer(text):
                args = m.group("args")
                level = re.search(r"Level\s*=\s*LogLevel\.(\w+)", args)
                event = re.search(r"EventId\s*=\s*(\d+)", args)
                template = cs_concat(args[args.find("Message"):]) if "Message" in args else ""
                key = (event.group(1) + " " if event else "") + m.group("name")
                message = Message("log", path, line_of(text, m.start()), key, template,
                                  CS_LEVELS.get(level.group(1), "info") if level else "info", m.group("params"))
                by_name[m.group("name")].append(message)
                found.append(message)
        else:
            for m in ERROR_ENTRY.finditer(text):
                message = Message("error", path, line_of(text, m.start()),
                                  m.group("codes") + "." + m.group("name"), cs_concat(m.group("text")), "user")
                by_name[message.key].append(message)
                found.append(message)
    for path, text in files.items():
        if not path.endswith(".cs") or posixpath.basename(path) in CODE_REGISTRIES:
            continue
        lines = None
        for m in CS_IDENTIFIER.finditer(text):
            token = m.group(1)
            name = token if token in by_name else token.rsplit(".", 1)[-1]
            if name not in by_name:
                continue
            lines = lines or text.split("\n")
            number = text.count("\n", 0, m.start())
            if lines[number].lstrip().startswith("//"):
                continue
            for message in by_name[name]:
                message.sites.append((path, number + 1, message.level,
                                      cs_context(lines, number) if message.surface == "log" else set()))
    return found


def ui_inventory(files: dict[str, str], registries: list[str]) -> list[Message]:
    roots = re.compile(rf"^(?:{FRONTEND_ROOTS})")

    def root_of(path: str) -> str:
        match = roots.match(path)
        return match.group(0) if match else ""

    owners = {root_of(path) for path in registries} - {""}
    sources: dict[str, list[str]] = defaultdict(list)
    for p, body in files.items():
        if p.endswith(UI_SOURCES):
            root = root_of(p)
            sources[root if root in owners else ""].append(frontend_code(p, body))
    shared = Counter(UI_LITERAL.findall("\n".join(sources[""])))
    shared_prefixes = set(UI_PREFIX.findall("\n".join(sources[""])))
    scanned: dict[str, tuple[Counter, set[str]]] = {}
    found = []
    for path in registries:
        text = files[path]
        app = root_of(path) or posixpath.dirname(path) + "/"
        if app not in scanned:
            code = "\n".join(sources.get(app, []))
            scanned[app] = (Counter(UI_LITERAL.findall(code)) + shared, set(UI_PREFIX.findall(code)) | shared_prefixes)
        literals, built = scanned[app]
        prefixes = {literal.rstrip(".") for literal in literals} | built
        try:
            entries = list(registry_entries(path, text))
        except ValueError:
            continue
        for key, value, offset in entries:
            message = Message("ui", path, line_of(text, offset), key, value, "user")
            message.hits = literals[key] + literals[PLURAL.sub("", key)] + literals[key.split(".", 1)[-1]]
            parts = key.split(".")
            if not message.hits and any(".".join(parts[:n]) in prefixes for n in range(1, len(parts))):
                message.signals.add("dynamic")
            found.append(message)
    return found


def predict(message: Message) -> None:
    template, around = message.template, message.around
    failure, success = bool(FAILURE.search(template)), bool(SUCCESS.search(template))
    if message.level == "user":
        message.expected = "user"
    elif "handler" in around or failure:
        message.expected = "error" if message.level == "error" else "warning"
    elif success:
        message.expected = "info"
    else:
        message.expected = message.level
    if not (message.sites or message.hits) and "dynamic" not in message.signals:
        message.signals.add("unused")
    if message.level == "verbose":
        message.signals.add("verbose")
    if "loop" in around and message.level in ("info", "verbose"):
        message.signals.add("loop")
    if message.level in ("info", "verbose") and message.expected in ("warning", "error"):
        message.signals.add("level-low")
    if message.level == "warning" and success and not failure and "handler" not in around:
        message.signals.add("level-high")
    if message.surface == "log" and message.level == "error" and "Exception" not in message.params \
            and "handler" not in around:
        message.signals.add("no-exception")
    if message.level == "info" and not PLACEHOLDER.search(template):
        message.signals.add("no-data")


def classify(message: Message) -> None:
    signals = message.signals
    if message.level in ("user", "part"):
        message.tier = "-" if message.level == "part" else "U"
        if signals & {"unused", "duplicate"}:
            message.tier += "!"
    elif signals & {"unused", "verbose", "duplicate"}:
        message.tier = "C"
    elif message.level in ("warning", "error") or "handler" in message.around:
        message.tier = "A"
    else:
        message.tier = "B"


def mark_duplicates(messages: list[Message]) -> None:
    groups = defaultdict(list)
    for message in messages:
        if message.surface == "ui":
            continue
        normal = PLACEHOLDER.sub("%", message.template).strip().rstrip(".").lower()
        if len(normal) > 3:
            groups[(message.registry, normal)].append(message)
    for group in groups.values():
        for message in group[1:]:
            message.signals.add("duplicate")


def inventory(prefixes: list[str]) -> list[Message]:
    files = {path: read(path) for path in tracked() if not payload(path)}
    files = {path: text for path, text in files.items() if text}
    dictionaries = set(setting("prose/vale", "localeDictionaries", []) or [])

    def registries(test) -> list[str]:
        return [path for path in files if test(path) and (not prefixes or path.startswith(tuple(prefixes)))]

    messages = [
        *script_inventory(files, registries(lambda p: p.endswith("messages.env") or (
            is_shell(p, files[p]) and MSG_LINE.search(files[p])))),
        *backend_inventory(files, registries(lambda p: posixpath.basename(p) in CODE_REGISTRIES)),
        *ui_inventory(files, registries(lambda p: posixpath.basename(p) in dictionaries)),
    ]
    mark_duplicates(messages)
    for message in messages:
        message.around = set().union(*(site[3] for site in message.sites))
        predict(message)
        classify(message)
    return messages


def row(message: Message) -> list[str]:
    where = f"{message.registry}:{message.line}"
    if message.sites:
        where += f" <- {message.sites[0][0]}:{message.sites[0][1]}"
    return [message.tier, message.surface, message.level, message.expected, str(len(message.sites) or message.hits),
            ",".join(sorted(message.signals)), message.key, where, " ".join(message.template.split())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    messages = inventory([p.rstrip("/") + ("/" if os.path.isdir(os.path.join(ROOT, p)) else "")
                          for p in args.path])
    if args.summary:
        tiers = Counter(m.tier for m in messages)
        signals = Counter(s for m in messages for s in m.signals)
        print(msg("inventory_summary", len(messages), ", ".join(f"{k} {v}" for k, v in sorted(tiers.items())),
                  ", ".join(f"{k} {v}" for k, v in signals.most_common()) or "-"))
        return 0
    print("\t".join(COLUMNS))
    for message in sorted(messages, key=lambda m: (m.tier, m.registry, m.line)):
        print("\t".join(row(message)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
