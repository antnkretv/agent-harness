from __future__ import annotations

from session_results import session_result
from session_results import native_result
from collections import Counter
import json
import ntpath
import os
import re
import shlex
import subprocess
import sys
from urllib.parse import unquote, urlsplit
from ci_common import annotation, executable, forge, moment, msg
from session_cost import number, seconds, usage

DEFAULT_KIND = "claude-code"

MAX_TARGET = 120
MAX_DOCS = 20
RECORD_DOCS = 200
REASON_CHARS = 200

DOC_SUFFIX = ".md"

READ_TOOLS = ("Read", "Grep", "Glob", "Ls")
SKILL_TOOL = "Skill"
SKILL_DOC = ".agents/skills/{}/SKILL.md"
SHELL_TOOLS = ("Bash", "Shell")

POWERSHELL_READERS = ("get-content", "gc")

SHELL_READERS = ("cat", "sed", "head", "tail", "nl", "less", "more", "bat", "rg", "grep", "egrep", "fgrep", "awk",
                 "wc", "ls")

TERMINAL_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

READER_DIAGNOSTIC = re.compile(r"^[ \t]*(?:" + "|".join(map(re.escape, (*SHELL_READERS, *POWERSHELL_READERS, "select-object")))
                               + r")[ \t]*:", re.I | re.M)

SHELL_WRAPPERS = ("bash", "bash.exe", "sh", "sh.exe", "zsh", "zsh.exe")
POWERSHELL_WRAPPERS = ("powershell", "powershell.exe", "pwsh", "pwsh.exe")
SHELL_OPERATORS = set("();<>|&")

POWERSHELL_PATHS = r"""
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
$paths = [Collections.Generic.List[string]]::new()
foreach ($script in $inputData.commands) {
    $tokens = $null
    $errors = $null
    $tree = [Management.Automation.Language.Parser]::ParseInput($script, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { continue }
    foreach ($command in $tree.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true)) {
        if ($inputData.readers -notcontains $command.GetCommandName()) { continue }
        $nested = $false
        for ($parent = $command.Parent; $parent; $parent = $parent.Parent) {
            if ($parent -is [Management.Automation.Language.ScriptBlockExpressionAst]) {
                $invocation = $parent.Parent
                if ($invocation -is [Management.Automation.Language.CommandAst] -and $invocation.CommandElements[0] -eq $parent -and $invocation.InvocationOperator -in @('Ampersand', 'Dot')) { continue }
                $nested = $true
                break
            }
            if ($parent -is [Management.Automation.Language.FunctionDefinitionAst]) {
                $nested = $true
                break
            }
        }
        if ($nested) { continue }
        foreach ($element in @($command.CommandElements | Select-Object -Skip 1)) {
            if ($element -is [Management.Automation.Language.CommandParameterAst]) {
                if ($element.ParameterName -notin @('Path', 'LiteralPath') -or $null -eq $element.Argument) { continue }
                $element = $element.Argument
            }
            $values = if ($element -is [Management.Automation.Language.ArrayLiteralAst]) { $element.Elements } else { @($element) }
            foreach ($value in $values) {
                if ($value -is [Management.Automation.Language.StringConstantExpressionAst] -and -not $value.Value.StartsWith('-')) {
                    $paths.Add($value.Value)
                }
            }
        }
    }
}
ConvertTo-Json -InputObject @($paths.ToArray()) -Compress
"""

NATIVE_COMMANDS = r"""
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$scripts = [Console]::In.ReadToEnd() | ConvertFrom-Json
$result = @()
foreach ($script in $scripts) {
    $tokens = $null
    $errors = $null
    $tree = [Management.Automation.Language.Parser]::ParseInput($script, [ref]$tokens, [ref]$errors)
    $calls = @()
    $valid = !$errors.Count -and !$tree.BeginBlock -and !$tree.ProcessBlock -and !$tree.DynamicParamBlock -and !$tree.CleanBlock `
        -and !$tree.ParamBlock -and !$tree.UsingStatements.Count -and !$tree.Attributes.Count `
        -and !$tree.ScriptRequirements -and !$tree.EndBlock.Traps.Count
    foreach ($statement in $tree.EndBlock.Statements) {
        if ($statement -isnot [Management.Automation.Language.PipelineAst] -or $statement.Background) { $valid = $false; break }
        $pipeline = @()
        foreach ($command in $statement.PipelineElements) {
            if ($command -isnot [Management.Automation.Language.CommandAst] -or $command.Redirections.Count -or $command.InvocationOperator -ne 'Unknown') { $valid = $false; break }
            $words = @()
            foreach ($element in $command.CommandElements) {
                if ($element -is [Management.Automation.Language.CommandParameterAst]) {
                    $words += '-' + $element.ParameterName
                    if (!$element.Argument) { continue }
                    $element = $element.Argument
                }
                $values = if ($element -is [Management.Automation.Language.ArrayLiteralAst]) { $element.Elements } else { @($element) }
                foreach ($value in $values) {
                    if ($value -is [Management.Automation.Language.StringConstantExpressionAst]) { $words += [string]$value.Value }
                    elseif ($value -is [Management.Automation.Language.ConstantExpressionAst]) { $words += $value.Extent.Text }
                    else { $valid = $false }
                }
            }
            $pipeline += ,@($words)
        }
        $calls += ,@($pipeline)
    }
    $result += @{ calls = @($calls); valid = $valid }
}
ConvertTo-Json -InputObject @($result) -Depth 8 -Compress
"""

PATH_KEYS = ("file_path", "path", "filePath", "notebook_path")

RECORD_MARK = "agent-session-record"
RECORD_VERSION = 1


def warn(key: str, *args: object) -> None:
    print(annotation("warning", key, *args))


def line(value: object) -> str:
    if not isinstance(value, str):
        return ""
    for part in value.splitlines():
        if part.strip():
            return " ".join(part.split())
    return ""


TARGET_KEYS = ("file_path", "path", "pattern", "url")
FREE_KEYS = ("command", "query")

HEAD_WORDS = 2

FANOUT_TOOLS = ("Agent", "Task")


def messages(raw: str) -> list:
    out, bad = [], 0
    for row in raw.splitlines():
        row = row.strip()
        if not row:
            continue
        try:
            out.append(json.loads(row))
        except ValueError:
            bad += 1
    if bad:
        warn("digest_bad_lines", bad)
    return out


def detail(denial: dict) -> str:
    name = line(denial.get("tool_name")) or msg("digest_tool_unnamed")
    args = denial.get("tool_input")
    if not isinstance(args, dict):
        return name

    for key in FREE_KEYS:
        head = line(args.get(key))
        if head:
            return f"{name}: {' '.join(head.split()[:HEAD_WORDS])}"

    for key in TARGET_KEYS:
        head = line(args.get(key))
        if head:
            return f"{name}: {head[:MAX_TARGET] + '...' if len(head) > MAX_TARGET else head}"
    return name


def uses(item: object) -> list:
    if not isinstance(item, dict):
        return []
    message = item.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def denials(final: dict, native: dict, notes: dict) -> list | None:
    ledger, vendor = final.get("denials"), native.get("permission_denials")
    if not isinstance(ledger, list) and not isinstance(vendor, list):
        notes["denials"] = annotation("warning", "digest_denials_unchecked")
        return None
    ledger, vendor = (value if isinstance(value, list) else [] for value in (ledger, vendor))
    seen = {row.get("tool_use_id") for row in ledger if isinstance(row, dict) and row.get("tool_use_id")}
    rows = ledger + [row for row in vendor if not (isinstance(row, dict) and row.get("tool_use_id") in seen)]
    return [detail(row) if isinstance(row, dict) else msg("digest_denial_unreadable") for row in rows]


def walk(items: list, turns: float | None, notes: dict) -> dict | None:
    seen, fan = 0, {}
    for item in items:
        for block in uses(item):
            seen += 1
            if block.get("name") not in FANOUT_TOOLS:
                continue
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            kind = line(args.get("subagent_type")) or msg("digest_type_unnamed")
            fan[kind] = fan.get(kind, 0) + 1

    if not seen and (turns is None or turns > 1):
        notes["walk"] = annotation("warning", "digest_no_tool_calls")
        return None
    return fan


def tool_calls(items: list):
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        for block in uses(item):
            yield block.get("name"), block.get("input") if isinstance(block.get("input"), dict) else {}
        thing = item.get("item") if isinstance(item.get("item"), dict) else {}
        if item.get("type") == "item.completed" and thing.get("type") == "command_execution":
            yield "Shell", {"command": thing.get("command")}
        key = item.get("call_id") or f"#{index}"
        if item.get("type") == "tool_call" and key not in seen:
            seen.add(key)
            found = cursor_call(item)
            if found is not None:
                yield found


def native_executions(items: list) -> list[dict]:
    found: dict[str, dict | None] = {}
    for event in items:
        payload = event.get("payload") if isinstance(event, dict) and event.get("type") == "event_msg" else None
        item = payload.get("item") if isinstance(payload, dict) and payload.get("type") == "item_completed" else None
        if not isinstance(item, dict) or item.get("type") != "CommandExecution" or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        key = item["id"]
        if key not in found:
            found[key] = item
        elif found[key] != item:
            found[key] = None
    return [item for item in found.values() if item and item.get("status") == "completed"
            and type(item.get("exit_code")) is int and item["exit_code"] == 0]


def native_script(argv: object) -> tuple[str, bool] | None:
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) for word in argv):
        return None
    name = ntpath.basename(argv[0]).lower()
    powershell = name in POWERSHELL_WRAPPERS
    switches = ("-command", "-c") if powershell else ("-c", "-lc")
    if not powershell and name not in SHELL_WRAPPERS:
        return None
    position = next((i for i, word in enumerate(argv[1:], 1) if word.lower() in switches), None)
    if position is None or position + 2 != len(argv):
        return None
    prefix = [word.lower() for word in argv[1:position]]
    safe = all(word in ("-noprofile", "-noninteractive", "-nologo") for word in prefix) if powershell else not prefix
    return (argv[-1], powershell) if safe else None


def native_cwd(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if value.startswith("file:"):
        try:
            uri = urlsplit(value)
        except ValueError:
            return ""
        if uri.netloc or uri.query or uri.fragment:
            return ""
        value = unquote(uri.path)
        if sys.platform == "win32" and re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    return value if os.path.isabs(value) else ""


def native_options(args: list[str], flags: set[str], values: set[str]) -> tuple[dict[str, str], list[str]] | None:
    options, paths, index, enabled = {}, [], 0, True
    while index < len(args):
        arg = args[index]
        index += 1
        if enabled and arg == "--":
            enabled = False
        elif enabled and arg.startswith("-"):
            if arg in options or arg not in flags | values:
                return None
            options[arg] = ""
            if arg in values:
                if index == len(args):
                    return None
                options[arg] = args[index]
                index += 1
        else:
            paths.append(arg)
    return options, paths


def native_count(value: str, minimum: int = 1) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,10}", value)) and minimum <= int(value) <= 2147483647


def content_reader(words: list[str], powershell: bool) -> tuple[str, str, bool, list[tuple[str, int]]] | None:
    name, args = ntpath.basename(words[0]).lower(), words[1:]
    if powershell and name in (*POWERSHELL_READERS, "cat"):
        args = [arg.lower() if arg.startswith("-") else arg for arg in args]
        parsed = native_options(args, {"-path", "-literalpath", "-raw"},
                                {"-encoding", "-totalcount", "-head", "-tail"})
        if parsed is None:
            return None
        options, paths = parsed
        limits = [(key, value) for key, value in options.items() if key in ("-totalcount", "-head", "-tail")]
        if len(limits) > 1 or any(not native_count(value) for key, value in limits) or "-raw" in options and limits:
            return None
        codecs = {"utf8": "utf-8-sig", "utf8bom": "utf-8-sig", "utf8nobom": "utf-8-sig", "ascii": "ascii",
                  "unicode": "utf-16-le", "bigendianunicode": "utf-16-be", "utf32": "utf-32"}
        encoding = options.get("-encoding", "utf8").lower()
        if encoding not in codecs:
            return None
        operations = [("last" if key == "-tail" else "first", int(value)) for key, value in limits]
        raw, codec = "-raw" in options, codecs[encoding]
    elif not powershell and name in ("cat", "head", "tail"):
        parsed = native_options(args, {"-n", "-b", "-s"} if name == "cat" else set(),
                                set() if name == "cat" else {"-n", "--lines"})
        if parsed is None:
            return None
        options, paths = parsed
        if name != "cat" and (len(options) > 1 or any(not native_count(value) for value in options.values())):
            return None
        count = int(next(iter(options.values()), "10")) if name != "cat" else 0
        operations = [("first" if name == "head" else "last", count)] if name != "cat" else []
        raw, codec = False, "utf-8-sig"
    else:
        return None
    return (paths[0], codec, raw, operations) if len(paths) == 1 else None


def content_selection(words: list[str]) -> list[tuple[str, int]] | None:
    if ntpath.basename(words[0]).lower() != "select-object":
        return None
    parsed = native_options([arg.lower() for arg in words[1:]], set(), {"-first", "-last", "-skip", "-skiplast"})
    if parsed is None:
        return None
    options, paths = parsed
    if (paths or not options or "-first" in options and "-last" in options or "-skip" in options and "-skiplast" in options
            or "-skiplast" in options and any(key in options for key in ("-first", "-last"))):
        return None
    if any(not native_count(value, 0 if key in ("-skip", "-skiplast") else 1) for key, value in options.items()):
        return None
    return [("skiplast" if key == "-skip" and "-last" in options else key[1:], int(options[key]))
            for key in ("-skip", "-skiplast", "-first", "-last") if key in options]


def content_search(words: list[str]) -> str:
    name, args = ntpath.basename(words[0]).lower(), words[1:]
    if name == "rg":
        flags = {"-n", "--line-number", "-i", "--ignore-case", "-s", "--case-sensitive", "-w", "--word-regexp",
                 "-F", "--fixed-strings", "-U", "--multiline", "-P", "--pcre2", "--hidden", "--no-ignore"}
        values = {"-e", "--regexp", "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not",
                  "-A", "-B", "-C", "-m", "--after-context", "--before-context", "--context", "--max-count"}
    elif name == "grep":
        flags = {"-n", "--line-number", "-i", "--ignore-case", "-w", "--word-regexp", "-F", "--fixed-strings"}
        values = {"-e", "--regexp", "-A", "-B", "-C", "-m", "--after-context", "--before-context", "--context", "--max-count"}
    else:
        return ""
    parsed = native_options(args, flags, values)
    if parsed is None:
        return ""
    options, paths = parsed
    for key in ("-A", "-B", "-C", "--after-context", "--before-context", "--context", "-m", "--max-count"):
        if key in options and not native_count(options[key], 1 if key in ("-m", "--max-count") else 0):
            return ""
    if not any(key in options for key in ("-e", "--regexp")):
        paths = paths[1:]
    return paths[0] if len(paths) == 1 else ""


def content_visible(path: str, codec: str, raw: bool, operations: list[tuple[str, int]], output: str) -> bool:
    try:
        with open(path, encoding=codec) as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        return False
    text = text.removeprefix("\ufeff")
    lines = [text] if raw else text.splitlines()
    for operation, count in operations:
        if operation == "first":
            lines = lines[:count]
        elif operation == "last":
            lines = lines[-count:]
        elif operation == "skip":
            lines = lines[count:]
        elif operation == "skiplast" and count:
            lines = lines[:-count]
    if raw:
        lines = [line for value in lines for line in value.splitlines()]
    return any((line := value.strip()) and line in output for value in lines)


def powershell_json(script: str, payload: object):
    shell = executable("powershell" if sys.platform == "win32" else "pwsh")
    try:
        done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(payload),
                              text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
        return json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError(msg("digest_powershell_unread", str(error))) from error


def native_reads(items: list, tree: str) -> list[str]:
    executions, powershell = [], []
    for item in native_executions(items):
        script = native_script(item.get("command"))
        cwd = native_cwd(item.get("cwd"))
        if script is None or not cwd:
            continue
        source, native = script
        if not native and (len(source.splitlines()) != 1 or any(char in source for char in "$`{}();|&<>")):
            continue
        if not isinstance(item.get("aggregated_output"), str):
            continue
        output = TERMINAL_CSI.sub("", item["aggregated_output"])
        if not output.strip() or READER_DIAGNOSTIC.search(output):
            continue
        if native:
            executions.append((cwd, len(powershell), output, native))
            powershell.append(source)
        else:
            executions.append((cwd, [[words] for words in shell_segments(source)], output, native))
    parsed = []
    if powershell:
        parsed = powershell_json(NATIVE_COMMANDS, powershell)
    paths = []
    for cwd, commands, output, native in executions:
        if isinstance(commands, int):
            entry = parsed[commands]
            commands = entry["calls"] if entry["valid"] else []
        if len(commands) == 1 and len(commands[0]) == 1 and (target := content_search(commands[0][0])):
            targets = [(target, "utf-8-sig", False, [])]
        else:
            targets = []
            for pipeline in commands:
                reader = content_reader(pipeline[0], native) if pipeline else None
                selections = [content_selection(words) if native else None for words in pipeline[1:]]
                if reader is None or any(selection is None for selection in selections):
                    targets = []
                    break
                target, codec, raw, operations = reader
                operations += [operation for selection in selections for operation in selection]
                targets.append((target, codec, raw, operations))
        for target, codec, raw, operations in targets:
            found = tree_file(os.path.join(cwd, target), tree) if not any(char in target for char in "*?[]") else ""
            if found and content_visible(os.path.join(tree, found), codec, raw, operations, output):
                paths.append(found)
    return paths


def shell_command(script: str, powershell: bool) -> tuple[str, bool]:
    try:
        words = shlex.split(script)
    except ValueError:
        words = []
    if len(words) == 3 and ntpath.basename(words[0]).lower() in SHELL_WRAPPERS and words[1] in ("-c", "-lc"):
        return shell_command(words[2], powershell=False)
    if words and ntpath.basename(words[0]).lower() in POWERSHELL_WRAPPERS:
        command = next((index for index, word in enumerate(words[1:], 1) if word.lower() in ("-command", "-c")), None)
        if command is not None and command + 2 == len(words):
            return shell_command(words[command + 1], powershell=True)
    return script, powershell


def shell_segments(script: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for row in script.splitlines():
        lexer = shlex.shlex(row, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            words = list(lexer)
        except ValueError:
            continue
        segment: list[str] = []
        for word in words + [";"]:
            if word and set(word) <= SHELL_OPERATORS:
                segments += [segment] if segment else []
                segment = []
            else:
                segment.append(word)
    return segments


def shell_paths_many(commands: list[tuple[str, bool]]) -> list[str]:
    paths: list[str] = []
    powershell = []
    for command, default_powershell in commands:
        script, native = shell_command(command, default_powershell)
        if native:
            powershell.append(script)
        else:
            paths += [word for segment in shell_segments(script) if ntpath.basename(segment[0]).lower() in SHELL_READERS
                      for word in segment[1:] if not word.startswith("-")]
    if powershell:
        paths += powershell_json(POWERSHELL_PATHS, {"commands": powershell, "readers": [*SHELL_READERS, *POWERSHELL_READERS]})
    return paths


def tree_file(target: str, tree: str) -> str:
    full = os.path.realpath(os.path.join(tree, target))
    try:
        contained = os.path.normcase(os.path.commonpath((full, tree))) == os.path.normcase(tree)
    except ValueError:
        contained = False
    return os.path.relpath(full, tree).replace(os.sep, "/") if contained and os.path.isfile(full) else ""


def reads(items: list, tree: str, *others: str, skills: bool = False) -> dict[str, int]:
    roots = [os.path.realpath(root) for root in dict.fromkeys((tree, *others))]
    calls = list(tool_calls(items))
    targets = [first(args, PATH_KEYS) for name, args in calls if name in READ_TOOLS]
    targets += [SKILL_DOC.format(args["skill"]) for name, args in calls
                if skills and name == SKILL_TOOL and isinstance(args.get("skill"), str)]
    targets += shell_paths_many([(args["command"], name == "Shell" and sys.platform == "win32") for name, args in calls
                                 if name in SHELL_TOOLS and isinstance(args.get("command"), str)])
    found: Counter[str] = Counter()
    for target, count in Counter(filter(None, targets)).items():
        if path := next(filter(None, (tree_file(target, root) for root in roots)), ""):
            found[path] += count
    found.update(native_reads(items, roots[0]))
    return dict(found)


def merged(*found: object) -> dict[str, int]:
    out: dict[str, int] = {}
    for one in found:
        for path, count in (one.items() if isinstance(one, dict) else ()):
            if isinstance(path, str) and type(count) is int and count > 0:
                out[path] = out.get(path, 0) + count
    return out


def journal(path: str, since: float = 0.0) -> list:
    with open(path, encoding="utf-8", errors="replace") as handle:
        items = messages(handle.read())
    return [item for item in items if not isinstance(item, dict) or not item.get("timestamp")
            or moment(item["timestamp"]) >= since]


def top(docs: dict) -> dict[str, int]:
    return dict(ranked({path: count for path, count in docs.items() if path.endswith(DOC_SUFFIX)})[:RECORD_DOCS])


def model_of(native: dict, final: dict) -> str:
    used = native.get("modelUsage") if isinstance(native.get("modelUsage"), dict) else {}
    spent = {name: number(row.get("costUSD")) or 0.0 for name, row in used.items() if isinstance(row, dict)}
    return max(spent, key=spent.__getitem__) if spent else line(final.get("model"))


def session_tree() -> str:
    return os.environ.get("SESSION_TREE") or forge.workspace() or os.getcwd()


def record(final: dict, native: dict, notes: dict) -> dict:
    reason = final.get("result")
    return {
        "notes": notes,
        "outcome": line(final.get("subtype")),
        "failed": bool(final.get("is_error")),
        "turns": None,
        "duration_s": seconds(final, native),
        "cost_usd": None,
        "tokens": usage(final) or usage(native),
        "reason": " ".join(reason.split()) if isinstance(reason, str) else "",
        "denials": denials(final, native, notes),
        "fanout": None,
        "documents": None,
        "model": model_of(native, final),
    }


def cut_session(final: dict) -> dict:
    return record(final, {}, {"walk": annotation("warning", "digest_no_native_result")})


def vendor_unread(out: dict) -> dict:
    out["notes"]["denials"] = annotation("warning", "digest_vendor_denials_unread")
    out["denials"] = out["denials"] or None
    return out


def read_claude_code(items: list, final: dict) -> dict:
    native = native_result(items)
    if not native:
        return vendor_unread(cut_session(final))

    notes: dict = {}
    out = record(final, native, notes)
    out["turns"] = number(native.get("num_turns"))
    out["cost_usd"] = number(native.get("total_cost_usd"))
    out["fanout"] = walk(items, out["turns"], notes)
    out["documents"] = reads(items, session_tree(), skills=True)
    return out if isinstance(native.get("permission_denials"), list) else vendor_unread(out)


def read_codex(items: list, final: dict) -> dict:
    out = record(final, {}, {"walk": annotation("warning", "codex_digest_no_walk")})
    out["documents"] = reads(items, session_tree(), skills=True)
    return out


CURSOR_TOOL_SUFFIX = "ToolCall"
CURSOR_SUBAGENT_KEYS = ("subagentType", "subagent_type")


def cursor_call(event: dict) -> tuple[str, dict] | None:
    call = event.get("tool_call")
    if not isinstance(call, dict):
        return None
    tool = call.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("case"), str):
        named = [(tool["case"], tool.get("value"))]
    else:
        named = list(call.items())
    named = [(key, value) for key, value in named if key.endswith(CURSOR_TOOL_SUFFIX)]
    if len(named) != 1 or not isinstance(named[0][1], dict):
        return None
    name, body = named[0]
    base = name[:-len(CURSOR_TOOL_SUFFIX)]
    args = body.get("args")
    return base[:1].upper() + base[1:], args if isinstance(args, dict) else {}


def first(args: dict, keys: tuple) -> str:
    for key in keys:
        value = line(args.get(key))
        if value:
            return value
    return ""


def cursor_walk(items: list, notes: dict) -> dict | None:
    seen, unread, fan = set(), 0, {}
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "tool_call":
            continue
        key = item.get("call_id") or f"#{index}"
        if key in seen:
            continue
        seen.add(key)
        found = cursor_call(item)
        if found is None:
            unread += 1
            continue
        name, args = found
        if name in FANOUT_TOOLS:
            kind = first(args, CURSOR_SUBAGENT_KEYS) or msg("digest_type_unnamed")
            fan[kind] = fan.get(kind, 0) + 1

    if not seen:
        notes["walk"] = annotation("warning", "digest_no_tool_calls")
        return None
    if unread == len(seen):
        notes["walk"] = annotation("warning", "digest_cursor_tool_unreadable", unread)
        return None
    if unread:
        warn("digest_cursor_tool_unreadable", unread)
    return fan


def read_cursor(items: list, final: dict) -> dict:
    native = native_result(items)
    if not native:
        return cut_session(final)

    notes: dict = {}
    out = record(final, native, notes)
    out["fanout"] = cursor_walk(items, notes)
    out["documents"] = reads(items, session_tree(), skills=True)
    return out


READERS = {DEFAULT_KIND: read_claude_code, "codex": read_codex, "cursor": read_cursor}

TOKEN_TITLES = (
    ("input", msg("digest_tokens_input")),
    ("output", msg("digest_tokens_output")),
    ("cache_read", msg("digest_tokens_cache_read")),
    ("cache_write", msg("digest_tokens_cache_write")),
)


def report(record: dict) -> None:
    print(msg("digest_title"))
    print(msg("digest_outcome", record['outcome'] or msg("digest_outcome_unknown")))
    print(msg("digest_failed", msg("digest_yes") if record['failed'] else msg("value_no")))

    if record["turns"] is not None:
        print(msg("digest_turns", format(record['turns'], ".0f")))
    if record["duration_s"] is not None:
        print(msg("digest_duration", format(record['duration_s'], ".0f")))
    if record["cost_usd"] is not None:
        print(msg("digest_cost", format(record['cost_usd'], ".4f")))

    parts = [f"{title} {record['tokens'][field]:.0f}"
             for field, title in TOKEN_TITLES if field in record["tokens"]]
    if parts:
        print(msg("digest_tokens", ', '.join(parts)))
    elif not record["failed"]:
        warn("digest_no_tokens")

    if record["failed"]:
        text = record["reason"]
        if text:
            cut = text[:REASON_CHARS] + (msg("digest_cut") if len(text) > REASON_CHARS else "")
            print(msg("digest_reason", cut))
        else:
            warn("digest_no_reason")

    notes = record.get("notes") or {}

    refused = record["denials"]
    if notes.get("denials"):
        print(notes["denials"])
    if refused is None:
        pass
    elif refused:
        print(msg("digest_denials", len(refused)))
        for item in refused:
            print(f"  {item}")
    else:
        print(msg("digest_denials_none"))

    fan, docs = record["fanout"], record["documents"]
    if fan is None:
        if notes.get("walk"):
            print(notes["walk"])
    else:
        total = sum(fan.values())
        print(msg("digest_fanout", total) if total else msg("digest_fanout_none"))
        for kind in sorted(fan):
            print(f"  {kind}: {fan[kind]}")

    if docs is None:
        return
    if not docs:
        print(msg("digest_docs_none"))
        return
    print(msg("digest_docs", sum(docs.values()), len(docs)))
    order = ranked(docs)
    for target, count in order[:MAX_DOCS]:
        print(f"  {count}  {target[:MAX_TARGET]}")
    if len(order) > MAX_DOCS:
        print(msg("digest_docs_more", len(order) - MAX_DOCS))


def digest(path: str, kind: str) -> None:
    kind = kind or DEFAULT_KIND
    reader = READERS.get(kind)
    if reader is None:
        warn("digest_kind_unknown", kind)
        return

    try:
        final = session_result(path)
        items = journal(path) if final else []
    except OSError as error:
        warn("digest_log_unread", error.strerror, path)
        return
    if final is None:
        warn("digest_log_no_result", path)
        return

    record = reader(items, final)
    if record["documents"] is not None:
        record["documents"] = merged(record["documents"], final.get("documents"))
    report(record)
    print(f"{RECORD_MARK} {machine(record, kind)}")


def ranked(docs: dict) -> list:
    return sorted(docs.items(), key=lambda pair: (-pair[1], pair[0]))


def machine(record: dict, kind: str) -> str:
    docs = record["documents"]
    if docs is not None:
        docs = top(docs)

    refused = record["denials"]
    return json.dumps({
        "v": RECORD_VERSION,
        "pass": os.environ.get("PASS", ""),
        "kind": kind,
        "failed": record["failed"],
        "turns": record["turns"],
        "duration_s": record["duration_s"],
        "cost_usd": record["cost_usd"],
        "tokens": record["tokens"],
        "denials": None if refused is None else len(refused),
        "fanout": record["fanout"],
        "documents": docs,
        "model": record["model"],
    }, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    if not 2 <= len(sys.argv) <= 3:
        warn("digest_usage")
        return 0

    try:
        digest(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else "")
    except Exception as error:  # noqa: BLE001
        warn("digest_failed_build", type(error).__name__, error)
    return 0
