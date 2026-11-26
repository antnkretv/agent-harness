from __future__ import annotations
import re
from ci_common import msg
from code_text import js_text
from text_primitives import CYRILLIC, KEY, Violation, is_shell, line_of, words

MSG_LINE = re.compile(rf"^(?:ARG[ \t]+)?MSG_({KEY})='([^']*)'", re.MULTILINE)

LIB_HELPERS = {"say", "die", "error", "log", "info", "debug", "warn", "changed", "unchanged", "reject_arg",
               "section", "row", "annotate", "say_err", "usage_die", "check_fail", "check_verdict",
               "platform_say", "platform_warn", "platform_die"}

KEY_FIRST = {"say", "die", "error", "log", "info", "debug", "warn", "changed", "unchanged", "section",
             "say_err", "usage_die", "check_fail", "check_verdict", "platform_say", "platform_warn",
             "platform_die"}

FUNCTION_DEF = re.compile(r"^[ \t]*(?:function[ \t]+)?([A-Za-z_][\w-]*)[ \t]*\(\)", re.MULTILINE)

PRINTERS = {"echo", "printf"}

SKIP_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time",
              "exec", "command", "builtin", "fi", "done", "esac"}

DECLARATIONS = {"local", "export", "readonly", "declare", "typeset"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")

REQUIRED_VAR = re.compile(r"\$\{[#!]?[A-Za-z_0-9@*]+(?:\[[^\]]*\])?:?\?([^}]*)\}")
PARAM_DEFAULT = re.compile(r"^[#!]?[A-Za-z_0-9@*]+(?:\[[^\]]*\])?:?[-=+?](.*)$", re.DOTALL)

MACHINE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*$|::(?:group|endgroup|add-mask)::|[\[{])")


class Command:
    def __init__(self, line: int):
        self.line = line
        self.words: list[str] = []
        self.redirs: list[str] = []
        self.heredocs: list[str] = []
        self.piped = False
        self.to_file = False


def file_target(redirs: list[str]) -> bool:
    return any(not r.startswith("&") and r not in ("/dev/stderr", "/dev/stdout") for r in redirs)


def mark_grouped_output(commands: list[Command]) -> None:
    stack: list[int] = []
    for index, cmd in enumerate(commands):
        head = cmd.words[0] if cmd.words else ""
        if head in ("{", "do"):
            stack.append(index)
        elif head in ("}", "done") and stack:
            start = stack.pop()
            if file_target(cmd.redirs) or cmd.piped:
                for inner in commands[start:index]:
                    inner.to_file = True


def balanced(src: str, i: int, opener: str, closer: str) -> int:
    depth = 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "'" and opener == "(":
            end = src.find("'", i + 1)
            i = len(src) if end < 0 else end + 1
            continue
        if c == '"':
            i = skip_double(src, i + 1)
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def skip_double(src: str, i: int) -> int:
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if src.startswith("$(", i):
            i = balanced(src, i + 2, "(", ")")
            continue
        if src.startswith("${", i):
            i = balanced(src, i + 2, "{", "}")
            continue
        if c == "`":
            end = src.find("`", i + 1)
            i = len(src) if end < 0 else end + 1
            continue
        i += 1
    return i


def lex(src: str, first_line: int = 1) -> tuple[list[Command], list[tuple[str, int]]]:
    commands: list[Command] = []
    substitutions: list[tuple[str, int]] = []
    line = first_line
    cmd = Command(line)
    word = ""
    in_word = False
    redirect_next = False
    pending: list[tuple[str, bool, Command]] = []
    i = 0

    def end_word():
        nonlocal word, in_word, redirect_next
        if in_word:
            (cmd.redirs if redirect_next else cmd.words).append(word)
            redirect_next = False
        word, in_word = "", False

    def end_command(piped: bool = False):
        nonlocal cmd
        end_word()
        cmd.piped = cmd.piped or piped
        if cmd.words or cmd.heredocs or cmd.redirs:
            commands.append(cmd)
        cmd = Command(line)

    while i < len(src):
        c = src[i]
        if c == "\\" and src.startswith("\n", i + 1):
            i += 2
            line += 1
            continue
        if c == "\\":
            word += src[i:i + 2]
            in_word = True
            i += 2
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "\n":
            end_word()
            line += 1
            i += 1
            for delim, strip, owner in pending:
                body = []
                while i < len(src):
                    nl = src.find("\n", i)
                    nl = len(src) if nl < 0 else nl
                    text = src[i:nl]
                    i = nl + 1
                    line += 1
                    if (text.lstrip("\t") if strip else text) == delim:
                        break
                    body.append(text)
                owner.heredocs.append("\n".join(body))
            pending = []
            end_command()
            continue
        if c == "#" and not in_word:
            nl = src.find("\n", i)
            i = len(src) if nl < 0 else nl
            continue
        if c == "'":
            end = src.find("'", i + 1)
            end = len(src) - 1 if end < 0 else end
            chunk = src[i:end + 1]
            line += chunk.count("\n")
            word += chunk
            in_word = True
            i = end + 1
            continue
        if c == '"':
            end = skip_double(src, i + 1)
            chunk = src[i:end]
            for m in re.finditer(r"\$\(", chunk):
                inner_end = balanced(chunk, m.end(), "(", ")")
                substitutions.append((chunk[m.end():inner_end - 1], line + chunk.count("\n", 0, m.start())))
            line += chunk.count("\n")
            word += chunk
            in_word = True
            i = end
            continue
        if src.startswith("$(", i) or (c in "<>" and src.startswith("(", i + 1) and not in_word):
            end = balanced(src, i + 2, "(", ")")
            inner = src[i + 2:end - 1]
            if not inner.startswith("("):
                substitutions.append((inner, line))
            line += src.count("\n", i, end)
            word += src[i:end]
            in_word = True
            i = end
            continue
        if src.startswith("${", i):
            end = balanced(src, i + 2, "{", "}")
            line += src.count("\n", i, end)
            word += src[i:end]
            in_word = True
            i = end
            continue
        if c == "`":
            end = src.find("`", i + 1)
            end = len(src) - 1 if end < 0 else end
            substitutions.append((src[i + 1:end], line))
            line += src.count("\n", i, end + 1)
            word += src[i:end + 1]
            in_word = True
            i = end + 1
            continue
        if src.startswith("<<", i) and not src.startswith("<<<", i):
            end_word()
            i += 2
            strip = src.startswith("-", i)
            i += 1 if strip else 0
            while i < len(src) and src[i] in " \t":
                i += 1
            m = re.match(r"""(['"]?)([A-Za-z_0-9]+)\1""", src[i:])
            if m:
                pending.append((m.group(2), strip, cmd))
                i += m.end()
            continue
        if c in "<>" or (c == "&" and src.startswith(">", i + 1)):
            if in_word and word.isdigit():
                word, in_word = "", False
            end_word()
            m = re.match(r"&?>>?|<<<|<&|>&|<>|<|>\|?", src[i:])
            op = m.group(0) if m else src[i]
            i += len(op)
            if op.endswith("&") and (src[i:i + 1].isdigit() or src.startswith("-", i)):
                j = i
                while j < len(src) and (src[j].isdigit() or src[j] == "-"):
                    j += 1
                cmd.redirs.append("&" + src[i:j])
                i = j
                continue
            redirect_next = ">" in op
            if not redirect_next:
                while i < len(src) and src[i] in " \t":
                    i += 1
            continue
        if c in ";&|()":
            op = src[i:i + 2]
            piped = c == "|" and op != "||"
            if op in (";;", "&&", "||", ";&", "|&"):
                i += 2
            else:
                i += 1
            end_command(piped)
            continue
        word += c
        in_word = True
        i += 1
    end_command()
    return commands, substitutions


def literal_text(raw: str) -> str:
    out = []
    i = 0
    quote = ""
    while i < len(raw):
        c = raw[i]
        if quote != "'" and c == "\\":
            out.append(raw[i + 1:i + 2])
            i += 2
            continue
        if c == "'" and quote != '"':
            quote = "" if quote == "'" else "'"
            i += 1
            continue
        if quote == "'":
            out.append(c)
            i += 1
            continue
        if c == '"':
            quote = "" if quote == '"' else '"'
            i += 1
            continue
        if raw.startswith("$(", i):
            i = balanced(raw, i + 2, "(", ")")
            continue
        if raw.startswith("${", i):
            end = balanced(raw, i + 2, "{", "}")
            m = PARAM_DEFAULT.match(raw[i + 2:end - 1])
            if m:
                out.append(literal_text(m.group(1)))
            i = end
            continue
        if c == "$":
            m = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])", raw[i:])
            i += m.end() if m else 1
            continue
        if c == "`":
            end = raw.find("`", i + 1)
            i = len(raw) if end < 0 else end + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def local_helpers(text: str) -> set[str]:
    found = set()
    for m in re.finditer(r"^[ \t]*(?:function[ \t]+)?([A-Za-z_][\w-]*)[ \t]*\(\)[ \t]*\{?(.*)$", text, re.MULTILINE):
        body = m.group(2)
        if not body.strip().endswith("}"):
            end = re.compile(r"^[ \t]*\}", re.MULTILINE).search(text, m.end())
            body = text[m.end():end.start() if end else len(text)]
        if re.search(r"\b(printf|echo|say|die|log|info|warn)\b|>&2", body):
            found.add(m.group(1))
    return found


def shell_file(path: str, text: str, helpers: set[str], first_line: int = 1) -> list[Violation]:
    found: list[Violation] = []
    own = set(FUNCTION_DEF.findall(text))
    body = MSG_LINE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    queue = [(body, first_line)]
    while queue:
        src, first = queue.pop()
        commands, subs = lex(src, first)
        mark_grouped_output(commands)
        queue.extend(subs)
        for cmd in commands:
            for raw in cmd.words + cmd.redirs:
                for m in REQUIRED_VAR.finditer(raw):
                    if words(m.group(1)):
                        found.append(Violation(path, cmd.line, msg("textreg_rule_required_var"), m.group(0)))
            names = list(cmd.words)
            while names and (names[0] in SKIP_WORDS or ASSIGNMENT.match(names[0])):
                names.pop(0)
            if not names or names[0] in DECLARATIONS:
                continue
            name = names[0]
            to_file = cmd.to_file or file_target(cmd.redirs)
            if name == "cat" and cmd.heredocs and not cmd.piped and not to_file:
                for doc in cmd.heredocs:
                    if words("\n".join(line for line in doc.split("\n") if not line.lstrip().startswith("#"))):
                        found.append(Violation(path, cmd.line, msg("textreg_rule_heredoc"), doc.strip().split("\n")[0]))
            if name in KEY_FIRST and name not in own and len(names) > 1 \
                    and not names[1].lstrip("\"'").startswith("@") and words(literal_text(names[1])):
                found.append(Violation(path, cmd.line, msg("textreg_rule_key_first", name), names[1]))
                continue
            if name not in PRINTERS and name not in helpers or name in PRINTERS and to_file:
                continue
            for raw in names[1:]:
                text_part = literal_text(raw)
                if name in PRINTERS and MACHINE.match(text_part) and not CYRILLIC.search(text_part):
                    continue
                if words(text_part):
                    found.append(Violation(path, cmd.line, msg("textreg_rule_literal", name), raw))
                    break
    return found


def shell_surface(files: dict[str, str]) -> list[Violation]:
    found = []
    for path, text in files.items():
        if is_shell(path, text):
            found += shell_file(path, text, LIB_HELPERS | local_helpers(text))
        elif path.startswith(WORKFLOW_ROOT):
            continue
        elif path.endswith((".yml", ".yaml", ".tmpl")):
            for m in REQUIRED_VAR.finditer(text):
                if words(m.group(1)):
                    found.append(Violation(path, line_of(text, m.start()), msg("textreg_rule_required_var"), m.group(0)))
    return found


WORKFLOW_ROOT = ".github/workflows/"
WORKFLOW_BLOCK = re.compile(r"^(\s*)(?:-\s+)?(run|script|prompt):[ \t]*(.*)$")
WORKFLOW_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


def workflow_blocks(text: str):
    lines = text.split("\n")
    for index, line in enumerate(lines):
        m = WORKFLOW_BLOCK.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        head = m.group(3).strip()
        if not re.fullmatch(r"[|>][-+]?", head):
            if head and not head.startswith("#"):
                yield index + 1, m.group(2), head.strip("'\"")
            continue
        indent = len(m.group(1))
        body: list[str] = []
        for inner in lines[index + 1:]:
            if inner.strip() and len(inner) - len(inner.lstrip()) <= indent:
                break
            body.append(inner)
        depth = min((len(b) - len(b.lstrip()) for b in body if b.strip()), default=0)
        yield index + 2, m.group(2), "\n".join(b[depth:] for b in body)


def workflow_surface(files: dict[str, str]) -> list[Violation]:
    found = []
    for path, text in files.items():
        if not path.startswith(WORKFLOW_ROOT) or not path.endswith((".yml", ".yaml")):
            continue
        for m in REQUIRED_VAR.finditer(text):
            if words(m.group(1)):
                found.append(Violation(path, line_of(text, m.start()), msg("textreg_rule_required_var"), m.group(0)))
        for first, key, body in workflow_blocks(text):
            code = WORKFLOW_EXPRESSION.sub("", body)
            if key == "run":
                found += shell_file(path, code, LIB_HELPERS | local_helpers(code), first)
            elif key == "script":
                found += [Violation(path, first + line - 1, rule, snippet) for line, rule, snippet in js_text(code)]
            elif words(code):
                found.append(Violation(path, first, msg("textreg_rule_prompt"),
                                       code.strip().split("\n")[0]))
    return found
