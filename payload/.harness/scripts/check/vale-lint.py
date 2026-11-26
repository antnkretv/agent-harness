#!/usr/bin/env python3
"""Prose-линт репозитория с общими исключениями и порогом Vale.

    ./.harness/hooks/python.cmd .harness/scripts/check/vale-lint.py                 # изменения против базы
    ./.harness/hooks/python.cmd .harness/scripts/check/vale-lint.py --all           # все отслеживаемые файлы
    ./.harness/hooks/python.cmd .harness/scripts/check/vale-lint.py --docs          # все отслеживаемые документы Markdown
    ./.harness/hooks/python.cmd .harness/scripts/check/vale-lint.py --files a.md b.cs
    echo "текст" | ./.harness/hooks/python.cmd .harness/scripts/check/vale-lint.py --stdin

По умолчанию линтуется дифф, а дифф, задевший вход гейта, линтует все файлы: вход - сам
гейт, глоссарий и поле `inputs` записи `prose` реестра. `--scope` читает пути со stdin и
печатает `run`, если гейт нужен этим путям, иначе пусто. Машинный и
вендорный текст исключен, поскольку HumanizedText адресован человеку. Профили
расширений задает `.vale.ini`; `LINTED_SUFFIXES` не дает звать Vale на файлы вне
этих профилей.
"""

from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
import comment_scan  # noqa: E402
import code_text  # noqa: E402

import argparse  # noqa: E402
import fnmatch  # noqa: E402
import functools  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

from ci_common import (  # noqa: E402
    GLOSSARY,
    LIVE_DOC_PREFIXES,
    ROOT,
    base_ref,
    changed_files,
    generated,
    git,
    listed,
    glossary_patterns,
    get_list,
    machine_text,
    modules,
    msg,
    payload,
    setting,
)

PROSE_SUFFIXES = (".md",)
NATIVE_CODE_SUFFIXES = (".cs", ".ps1", ".py", ".ts", ".tsx")

EXTRACTED_CODE_SUFFIXES = (
    ".sh", ".bash", ".psm1", ".yml", ".yaml", ".toml",
    ".js", ".jsx", ".mjs", ".cjs", ".vue", ".css", ".scss",
)

CODE_SUFFIXES = NATIVE_CODE_SUFFIXES + EXTRACTED_CODE_SUFFIXES
LINTED_SUFFIXES = PROSE_SUFFIXES + CODE_SUFFIXES + (".env", ".json")

BLOCKING_LEVEL = {"prose": "error", "code": "warning", "published": "warning"}
PUBLICATION = ".publish.md"
TEMPLATES = ".harness/templates/"

COORDINATE_SUFFIXES = (
    "cs|ts|tsx|js|jsx|mjs|cjs|vue|json|yml|yaml|sh|bash|ps1|psm1|py|toml|ini"
    "|csproj|sln|sql|razor|http|css|scss|md|html|txt"
)
LINE_NUMBERS = r":\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*"
COORDINATE = re.compile(
    "(?P<coord>"
    rf"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:{COORDINATE_SUFFIXES}){LINE_NUMBERS}"
    rf"|[A-Za-z0-9_$/-]+\.[A-Z][A-Za-z0-9_]*{LINE_NUMBERS}"
    rf"|(?P<bare>`{LINE_NUMBERS}`)"
    r")(?![\d\w])"
)


GLOSSARY_RULES = {
    "Glossary": (True, setting("prose/vale", "glossaryLevel", "suggestion"), "~registry"),
    "RegistryGlossary": (True, "warning", "registry"),
    "UserGlossary": (False, "warning", "registry"),
}
REGISTRY_FILES = ("messages.env", *setting("prose/vale", "localeDictionaries", []))


def quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def glossary_rule(swaps: dict[str, str], level: str, scope: str) -> str | None:
    if not swaps:
        return None
    lines = [
        "extends: substitution",
        "message: " + quoted(msg("vale_glossary_rule", "%s", "%s")),
        "level: " + level,
        "scope: " + scope,
        "ignorecase: true",
        "nonword: true",
        "swap:",
        *(f"  {quoted(pattern)}: {quoted(term)}" for pattern, term in swaps.items()),
    ]
    return "\n".join(lines) + "\n"


def glossary_rules() -> dict[str, str | None]:
    path = os.path.join(ROOT, GLOSSARY)
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    swaps = {
        everywhere: {pattern: term for _, pattern, term in glossary_patterns(text, everywhere=everywhere)}
        for everywhere in (True, False)
    }
    return {
        ".vale/styles/HumanizedText/" + name + ".yml": glossary_rule(swaps[everywhere], level, scope)
        for name, (everywhere, level, scope) in GLOSSARY_RULES.items()
    }


def glossary_rule_current() -> bool:
    for relative, expected in glossary_rules().items():
        path = os.path.join(ROOT, relative)
        if not os.path.exists(path):
            if expected is not None:
                return False
        else:
            with open(path, encoding="utf-8") as handle:
                if handle.read() != expected:
                    return False
    return True


def write_glossary_rule() -> None:
    for relative, expected in glossary_rules().items():
        path = os.path.join(ROOT, relative)
        if expected is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(expected)
        say(msg("vale_glossary_written", relative, expected.count("\n  ") if expected else 0))


def say(message: str) -> None:
    print(f"vale-lint: {message}", file=sys.stderr)


def find_vale() -> str | None:
    explicit = os.environ.get("VALE_BIN")
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("vale")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/vale")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def gate_inputs() -> tuple[str, ...]:
    record = modules().get("prose", {})
    return tuple(item for item in (record.get("gate"), GLOSSARY, *record.get("inputs", [])) if item)


def touches_inputs(paths: list[str]) -> bool:
    return any(path == item or item.endswith("/") and path.startswith(item) for path in paths for item in gate_inputs())


def gate_needed(paths: list[str]) -> bool:
    return touches_inputs(paths) or any(path.endswith(LINTED_SUFFIXES) for path in paths)


def tracked_files() -> list[str]:
    return [p for p in listed() if p.endswith(LINTED_SUFFIXES)]


def live_doc(path: str) -> bool:
    return path.startswith(LIVE_DOC_PREFIXES)


def lint_coordinates(paths: list[str]) -> list[dict]:
    alerts: list[dict] = []
    for path in paths:
        if not live_doc(path):
            continue
        with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        fenced = False
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            anchored = False
            for found in COORDINATE.finditer(line):
                if found.group("bare") and not anchored:
                    continue
                anchored = True
                alerts.append({
                    "File": path,
                    "Line": number,
                    "Span": [found.start("coord") + 1],
                    "Severity": "error",
                    "Message": (msg("vale_line_coordinate", found.group('coord'))),
                })
    return alerts


def registry_path(path: str) -> bool:
    return os.path.basename(path) in REGISTRY_FILES


def selectable(paths: list[str]) -> list[str]:
    picked = []
    vendored = tuple(".agents/skills/" + name + "/" for name in get_list("docs.vendoredSkills", []))
    for path in paths:
        if not path.endswith(LINTED_SUFFIXES):
            continue
        if path.startswith(vendored) or (machine_text(path) and not registry_path(path)) or generated(path) or payload(path):
            continue
        if path.endswith((".env", ".json")) and not registry_path(path):
            continue
        if not os.path.isfile(os.path.join(ROOT, path)):
            continue
        picked.append(path)
    return picked


def run_vale(vale: str, args: list[str], stdin_text: str | None = None) -> str:
    result = subprocess.run(
        [vale, *args],
        cwd=ROOT,
        capture_output=True,
        input=None if stdin_text is None else stdin_text.encode("utf-8"),
        check=False,
    )
    stdout, stderr = result.stdout.decode("utf-8"), result.stderr.decode("utf-8", "replace")
    if result.returncode not in (0, 1):
        print(msg("vale_runtime_failed", stdout or stderr), file=sys.stderr)
        raise SystemExit(2)
    return stdout


def alerts_from(output: str) -> list[dict]:
    decoder = json.JSONDecoder()
    merged: dict[str, list] = {}
    index = 0
    while index < len(output):
        while index < len(output) and output[index].isspace():
            index += 1
        if index >= len(output):
            break
        obj, index = decoder.raw_decode(output, index)
        for name, items in obj.items():
            merged.setdefault(name, []).extend(items)
    flat = []
    for name, items in merged.items():
        for item in items:
            item["File"] = name.replace(os.sep, "/")
            flat.append(item)
    return flat


LEVEL_ORDER = {"suggestion": 0, "warning": 1, "error": 2}
SCAN_LEVEL = "--minAlertLevel=suggestion"
VALE_COMMAND_LIMIT = 24_000 if os.name == "nt" else 100_000


def file_class(path: str) -> str:
    if path.endswith(PUBLICATION) or path.endswith(".md") and path.replace(os.sep, "/").startswith(TEMPLATES):
        return "published"
    return "prose" if path.endswith(PROSE_SUFFIXES) else "code"


def blocking(alert: dict, kind: str) -> bool:
    level = LEVEL_ORDER.get(alert["Severity"], 2)
    if kind == "published" and alert["Check"].rpartition(".")[2] in GLOSSARY_RULES:
        return level >= LEVEL_ORDER["error"]
    return level >= LEVEL_ORDER[BLOCKING_LEVEL[kind]]


def vale_batches(vale: str, options: list[str], paths: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    batch: list[str] = []
    for path in paths:
        candidate = [*batch, path]
        command = subprocess.list2cmdline([vale, *options, *candidate])
        if batch and len(command) > VALE_COMMAND_LIMIT:
            batches.append(batch)
            batch = [path]
        else:
            batch = candidate
    if batch:
        batches.append(batch)
    return batches


def lint_extracted(vale: str, paths: list[str]) -> list[dict]:
    if not paths:
        return []
    parser = comment_scan
    alerts: list[dict] = []
    for path in paths:
        full = os.path.join(ROOT, path)
        with open(full, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if generated(path, text):
            continue
        found = parser.comment_lines(path, text)
        if not found:
            continue
        numbers = [item[0] for item in found]
        payload = "\n".join(item[1] for item in found)
        if not payload.strip():
            continue
        output = run_vale(
            vale, ["--ext", ".txt", SCAN_LEVEL, "--output=JSON"], stdin_text=payload
        )
        for alert in alerts_from(output):
            index = alert["Line"] - 1
            alert["File"] = path
            alert["Line"] = numbers[index] if 0 <= index < len(numbers) else alert["Line"]
            alerts.append(alert)
    return alerts


def lint_registries(vale: str, paths: list[str]) -> list[dict]:
    if not paths:
        return []
    lines, locations = [], {}
    machine = get_list("policies.text.machineMessages", [], file="methodology.json")
    glossary_exceptions = {
        (entry["path"], entry["key"], entry["value"])
        for entry in setting("prose/vale", "glossaryValueExceptions", [])
    }
    for path in paths:
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            text = handle.read()
        line, previous = 1, 0
        try:
            for key, value, offset in code_text.registry_entries(path, text):
                line += text.count("\n", previous, offset)
                previous = offset
                if path.endswith(".env") and key in machine:
                    continue
                locations[len(lines) + 1] = (path, key, line, value)
                lines.extend((re.sub(r"\s+", " ", value), ""))
        except ValueError as error:
            return [{"File": path, "Line": 1, "Span": [1], "Severity": "error",
                     "Message": msg("vale_registry_unread", error)}]
    if not lines:
        return []
    output = run_vale(vale, ["--ext", ".messages.txt", SCAN_LEVEL, "--output=JSON"],
                         stdin_text="\n".join(lines))
    alerts = []
    for alert in alerts_from(output):
        line = alert["Line"]
        path, key, source_line, value = locations.get(line, locations.get(line - 1, next(iter(locations.values()))))
        if alert.get("Check") == "HumanizedText.Glossary" and (path, key, value) in glossary_exceptions:
            continue
        alert.update(File=path, Line=source_line, Span=[1],
                     Message=msg("value_pair", key, alert["Message"]))
        alerts.append(alert)
    return alerts


def report(alerts: list[dict], files: list[str]) -> int:
    if not alerts:
        say(msg("gate_clean", len(files)))
        return 0

    alerts.sort(key=lambda a: (a["File"], a["Line"]))
    blocked = [a for a in alerts if blocking(a, file_class(a["File"]))]
    advised = [a for a in alerts if a not in blocked]

    def show(items: list[dict]) -> None:
        for alert in items:
            print(f"{alert['File']}:{alert['Line']}:{alert['Span'][0]}: {alert['Severity']}: {alert['Message']}")

    if advised:
        print(msg("vale_advice_title"))
        show(advised)
        if blocked:
            print()

    if not blocked:
        say(msg("vale_advice_only", len(advised)))
        return 0

    show(blocked)

    hint = msg("vale_hint")
    if any(live_doc(a["File"]) for a in blocked):
        hint += msg("vale_hint_live_doc")
    files_hit = len({a["File"] for a in blocked})
    print("\n" + msg("vale_failed", len(blocked), files_hit, hint), file=sys.stderr)
    return 1


@functools.cache
def view_globs() -> tuple[str, ...]:
    try:
        with open(os.path.join(ROOT, ".vale.ini"), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ()
    found, section = [], None
    for line in text.splitlines():
        header = re.fullmatch(r"\[(.+)\]", line.strip())
        if header:
            section = header.group(1)
        elif section and re.match(r"View\s*=", line.strip()):
            found += expand(section)
    return tuple(found)


def viewed_by(path: str, glob: str) -> bool:
    if glob.startswith("**/"):
        return fnmatch.fnmatch(path, glob[3:]) or fnmatch.fnmatch(path, "*/" + glob[3:])
    return fnmatch.fnmatch(path, glob)


def expand(glob: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", glob)
    if not match:
        return [glob]
    return [item for part in match.group(1).split(",")
            for item in expand(glob[:match.start()] + part + glob[match.end():])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="все отслеживаемые .md и .cs")
    scope.add_argument("--docs", action="store_true", help="все отслеживаемые документы .md: этап 2 установки harness")
    scope.add_argument("--files", nargs="+", metavar="PATH", help="явный список файлов")
    scope.add_argument("--stdin", action="store_true", help="текст из stdin (сообщение коммита, тело issue)")
    parser.add_argument("--base", help="база сравнения для режима изменений")
    parser.add_argument("--worktree", action="store_true", help="добавить незакоммиченные изменения")
    parser.add_argument("--ext", default=PUBLICATION, help="расширение для --stdin: профиль .vale.ini и порог; по умолчанию публикация, сообщение коммита - .commit.md, документ - .md")
    parser.add_argument("--label", default="", help="как называть источник текста в выводе")
    parser.add_argument("--glossary", action="store_true", help="собрать правило Vale из колонки проверки глоссария")
    parser.add_argument(
        "--scope",
        action="store_true",
        help="run, если гейт нужен путям из stdin, иначе пусто - для отбора гейта в хуках и конвейере",
    )
    args = parser.parse_args()

    if args.scope:
        print("run" if gate_needed([line.strip() for line in sys.stdin if line.strip()]) else "")
        return 0

    if args.glossary:
        write_glossary_rule()
        return 0

    if not args.stdin and not glossary_rule_current():
        print(msg("vale_glossary_stale", ", ".join(GLOSSARY_RULES), GLOSSARY), file=sys.stderr)
        return 1

    vale = find_vale()
    if not vale:
        say(msg("vale_no_binary"))
        if args.stdin:
            return 0

    if args.stdin:
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        if not text.strip():
            return 0
        output = run_vale(vale, ["--ext", args.ext, "--minAlertLevel=warning", "--output=JSON"], stdin_text=text)
        alerts = [alert for alert in alerts_from(output) if blocking(alert, file_class("stdin" + args.ext))]
        if not alerts:
            return 0
        label = args.label or msg("vale_default_label")
        print(msg("vale_stdin_failed", label, len(alerts)), file=sys.stderr)
        for alert in alerts:
            print(msg("vale_stdin_alert", alert['Line'], alert['Message']), file=sys.stderr)
        return 1

    if args.all or args.docs:
        candidates = [path for path in tracked_files() if args.all or path.endswith(".md")]
    elif args.files:
        candidates = args.files
    else:
        base = base_ref(args.base)
        if not base:
            say(msg("gate_no_base"))
            return 0
        candidates = changed_files(base, args.worktree)
        if touches_inputs(candidates):
            say(msg("vale_inputs_changed"))
            candidates = tracked_files()

    files = selectable(candidates)
    if not files:
        return 0

    values = [p for p in files if registry_path(p)]
    native = [p for p in files if not registry_path(p) and not p.endswith(EXTRACTED_CODE_SUFFIXES)]
    extracted = [p for p in files if p.endswith(EXTRACTED_CODE_SUFFIXES)]

    alerts: list[dict] = lint_coordinates(files)
    if not vale:
        return report(alerts, files)

    options = [SCAN_LEVEL, "--output=JSON"]
    viewed = [path for path in native if any(viewed_by(path, glob) for glob in view_globs())]
    for group in ([path for path in native if path not in viewed], viewed):
        for chunk in vale_batches(vale, options, group):
            alerts.extend(alerts_from(run_vale(vale, [*options, *chunk])))

    alerts.extend(lint_extracted(vale, extracted))
    alerts.extend(lint_registries(vale, values))

    return report(alerts, files)


if __name__ == "__main__":
    sys.exit(main())
