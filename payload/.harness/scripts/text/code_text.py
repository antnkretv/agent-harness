from __future__ import annotations
import ast
import json
import os
import re
from ci_common import msg
from text_primitives import CYRILLIC, LATIN_PROSE
from text_primitives import Violation, line_of, shebang, words
from ci_common import MESSAGE_LINE, app_prefix, get_records, json_strings

FRONTEND_ROOTS = "|".join([re.escape(app_prefix(str(app.get("path", "")))) for app in get_records("apps", [])
                           if app.get("stack") == "node"] or ["(?!)"])

CYRILLIC_WORD = re.compile(r"[А-Яа-яЁё]{2}")

JS_SUFFIXES = (".mjs", ".cjs", ".js", ".ts", ".mts", ".cts")

JS_OUTSIDE = re.compile(r"(^|/)(src|public|eslint-rules|node_modules|dist)/|^e2e/|\.d\.[mc]?ts$"
                        r"|(^|/)[^/]*\.config\.[mc]?[jt]s$|(^|/)messages\.mjs$")

JS_PRINTERS = {"log", "error", "warn", "info", "debug", "write", "Error", "TypeError", "RangeError",
               "fail", "die", "setFailed", "warning", "notice"}

JS_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}
JS_EXPORT = re.compile(r"^export\s+(?:const|function|let)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
JS_COMMENTS = (("//", "\n"), ("/*", "*/"), ("<!--", "-->"))


def js_tokens(src: str):
    calls: list[str] = []
    i, line, last, word = 0, 1, "", ""
    while i < len(src):
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        comment = next((pair for pair in JS_COMMENTS if src.startswith(pair[0], i)), None) if c in "/<" else None
        if comment:
            opener, closer = comment
            end = src.find(closer, i + len(opener))
            end = len(src) if end < 0 else end + (0 if closer == "\n" else len(closer))
            yield "comment", i, end, line, src[i:end], ""
            line += src.count("\n", i, end)
            i = end
            continue
        if c in "'\"`":
            j, parts = i + 1, []
            while j < len(src) and src[j] != c and (c == "`" or src[j] != "\n"):
                if src[j] == "\\":
                    parts.append(src[j + 1:j + 2])
                    j += 2
                    continue
                if c == "`" and src.startswith("${", j):
                    depth, j = 1, j + 2
                    while j < len(src) and depth:
                        depth += {"{": 1, "}": -1}.get(src[j], 0)
                        j += 1
                    continue
                parts.append(src[j])
                j += 1
            end = j + 1 if j < len(src) and src[j] == c else j
            yield "string", i, end, line, "".join(parts), calls[-1] if calls else ""
            line += src.count("\n", i, end)
            i, last = end, "x"
            continue
        if c == "/" and (last in JS_REGEX_BEFORE or word in ("return", "typeof", "case")):
            j = i + 1
            klass = False
            while j < len(src) and src[j] != "\n" and (src[j] != "/" or klass):
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    klass = True
                elif src[j] == "]":
                    klass = False
                j += 1
            i, last = j + 1, "x"
            continue
        if c.isalnum() or c in "_$":
            j = i
            while j < len(src) and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            word, last = src[i:j], "x"
            i = j
            continue
        if c == "(":
            calls.append(word)
        elif c == ")" and calls:
            calls.pop()
        if not c.isspace():
            last, word = c, ""
        i += 1


def js_literals(src: str):
    return [(line, text, call) for kind, _, _, line, text, call in js_tokens(src) if kind == "string"]


def js_code(src: str) -> str:
    out, at = [], 0
    for kind, start, end, *_ in js_tokens(src):
        if kind == "comment":
            out += [src[at:start], re.sub(r"[^\n]", " ", src[start:end])]
            at = end
    return "".join(out) + src[at:]


def js_text(src: str):
    found = []
    for line, text, call in js_literals(src):
        if CYRILLIC_WORD.search(text):
            found.append((line, msg("textreg_rule_js_cyrillic"), text.strip().split("\n")[0]))
        elif call in JS_PRINTERS and LATIN_PROSE.search(text):
            found.append((line, msg("textreg_rule_literal", call), text.strip().split("\n")[0]))
    return found


def js_surface(files: dict[str, str]) -> list[Violation]:
    found = []
    for path, text in files.items():
        if path.endswith(JS_SUFFIXES) and not JS_OUTSIDE.search(path):
            found += [Violation(path, line, rule, snippet) for line, rule, snippet in js_text(text)]
    for path, text in files.items():
        if os.path.basename(path) != "messages.mjs":
            continue
        folder = os.path.dirname(path)
        readers = [body for other, body in files.items()
                   if other != path and other.startswith(folder + "/") and "messages.mjs" in body]
        for m in JS_EXPORT.finditer(text):
            if not any(re.search(rf"\b{re.escape(m.group(1))}\b", body) for body in readers):
                found.append(Violation(path, line_of(text, m.start()), msg("textreg_rule_js_unused"), m.group(1)))
    return found


PY_PRINTERS = {"print", "exit", "error", "fail", "die", "warn", "warning", "say", "log", "write"}
PY_DECLARATION_KWARGS = {"help", "description", "epilog", "metavar", "prog", "usage"}
PY_REGEX = {"compile", "search", "match", "fullmatch", "sub", "subn", "findall", "finditer", "split"}


def py_literal(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(node, ast.BinOp):
        return py_literal(node.left) + py_literal(node.right)
    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "format":
        return py_literal(node.func.value)
    return ""


def raw_literal(text: str, node: ast.AST) -> bool:
    source = ast.get_source_segment(text, node) or ""
    return bool(re.match(r"(?:[rR][fFbB]?|[fFbB][rR])['\"]", source))


def python_exempt(tree: ast.AST) -> set[int]:
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            exempt.add(id(node.body[0].value))
        inner: list[ast.AST] = []
        if isinstance(node, ast.keyword) and node.arg in PY_DECLARATION_KWARGS:
            inner = [node.value]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") in PY_REGEX \
                and getattr(getattr(node.func, "value", None), "id", "") == "re":
            inner = node.args[:1]
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("MSG_") for t in node.targets):
            inner = [node.value]
        elif isinstance(node, ast.JoinedStr):
            inner = [child for child in node.values if isinstance(child, ast.Constant)]
        for part in inner:
            exempt.update(id(child) for child in ast.walk(part))
    return exempt


def python_file(path: str, text: str) -> list[Violation]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    printers = set(PY_PRINTERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
                isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) in PY_PRINTERS
                for n in ast.walk(node)):
            printers.add(node.name)
    found = []

    def check(node: ast.AST, rule: str):
        text_part = py_literal(node)
        if words(text_part):
            found.append(Violation(path, node.lineno, rule, text_part.strip().split("\n")[0][:80]))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name in printers and name != "msg":
                for arg in node.args:
                    check(arg, msg("textreg_rule_literal", name))
                for kw in node.keywords:
                    if kw.arg not in PY_DECLARATION_KWARGS:
                        check(kw.value, msg("textreg_rule_literal", name))
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            for arg in node.exc.args:
                check(arg, msg("textreg_rule_raise"))
        elif isinstance(node, ast.Assert) and node.msg is not None:
            check(node.msg, msg("textreg_rule_assert"))

    exempt = python_exempt(tree)
    reported = {(v.line, v.snippet) for v in found}
    for node in ast.walk(tree):
        if id(node) in exempt or not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        text_part = py_literal(node)
        if not CYRILLIC_WORD.search(text_part) or raw_literal(text, node):
            continue
        snippet = text_part.strip().split("\n")[0][:80]
        if (node.lineno, snippet) not in reported:
            found.append(Violation(path, node.lineno, msg("textreg_rule_py_cyrillic"), snippet))
    return found


def python_surface(files: dict[str, str]) -> list[Violation]:
    found = []
    for path, text in files.items():
        if path.endswith(".py") or (not os.path.splitext(path)[1] and "python" in shebang(text)):
            found += python_file(path, text)
    return found


PS_PRINTERS = r"Write-Host|Write-Warning|Write-Error|Write-Output|Write-Information|Write-Verbose|throw"
PS_STRING = re.compile(r'"((?:`.|[^"`])*)"|\'([^\']*)\'')


def ps_literal(text: str) -> str:
    text = re.sub(r"\$\((?:[^()]|\([^()]*\))*\)|\$\{[^}]*\}|\$[\w:]+", "", text)
    return text


def powershell_surface(files: dict[str, str]) -> list[Violation]:
    found = []
    for path, text in files.items():
        if not path.endswith((".ps1", ".psm1")):
            continue
        helpers = set(re.findall(r"^\s*function\s+([\w-]+)[^{]*\{[^}]*(?:Write-|\$Host\.UI)", text, re.MULTILINE | re.IGNORECASE))
        call = re.compile(rf"(?<![\w-])(?:{PS_PRINTERS}{''.join('|' + re.escape(h) for h in helpers)})\b(.*)$", re.IGNORECASE)
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            m = call.search(line)
            if not m:
                continue
            for s in PS_STRING.finditer(m.group(1)):
                if words(ps_literal(s.group(1) if s.group(1) is not None else s.group(2))):
                    found.append(Violation(path, number, msg("textreg_rule_powershell"), s.group(0)))
                    break
    return found


FRONTEND_SOURCES = re.compile(
    rf"^(?:{FRONTEND_ROOTS})(?:src/|public/|[^/]+\.html$|(?:vite|astro)\.config\.[^/]+$)"
)

FRONTEND_EXTENSIONS = (
    ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".astro", ".html", ".webmanifest", ".css",
)

FRONTEND_TEXT = re.compile(rf"{CYRILLIC.pattern}|№")
DICTIONARY = re.compile(rf"^(?:{FRONTEND_ROOTS})(?:[^/]+/)*locales/")
ESCAPE = re.compile(r"\\u\{([0-9a-fA-F]{1,6})\}|\\u([0-9a-fA-F]{4})")


def unescaped(text: str) -> str:
    return ESCAPE.sub(lambda m: chr(int(next(group for group in m.groups() if group), 16)), text)

STYLE_TOKENS = re.compile(r"""/\*.*?\*/|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*\"""", re.S)

MARKUP_COMMENTS = re.compile(r"<!--.*?-->|\{/\*.*?\*/\}", re.S)
MARKUP_CODE = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)|^(---\n)(.*?)(\n---)", re.S)


def blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def keep_strings(match: re.Match[str]) -> str:
    return blank(match) if match.group(0).startswith("/*") else match.group(0)


def frontend_code(path: str, text: str) -> str:
    if path.endswith(".webmanifest"):
        return text
    if path.endswith(".css"):
        return STYLE_TOKENS.sub(keep_strings, text)
    if path.endswith((".html", ".astro")):
        return MARKUP_CODE.sub(
            lambda m: (
                m.group(1) + js_code(m.group(2)) + m.group(3)
                if m.group(1)
                else m.group(4) + js_code(m.group(5)) + m.group(6)
            ),
            MARKUP_COMMENTS.sub(blank, text),
        )
    return js_code(text)


def registry_entries(path: str, text: str):
    if path.endswith(".env"):
        for match in MESSAGE_LINE.finditer(text):
            yield match.group(1), match.group(2), match.start(2)
        return
    values = json_strings(json.loads(text))
    separator = re.compile(r"\s*:")
    offsets = (start + 1 for kind, start, end, *_ in js_tokens(text)
               if kind == "string" and not separator.match(text, end))
    for (key, value), offset in zip(values, offsets, strict=True):
        yield key, value, offset


def frontend_surface(files: dict[str, str]) -> list[Violation]:
    found = []
    for path, text in files.items():
        if not FRONTEND_SOURCES.match(path) or not path.endswith(FRONTEND_EXTENSIONS) or DICTIONARY.match(path):
            continue
        if not FRONTEND_TEXT.search(unescaped(text)):
            continue
        for number, line in enumerate(frontend_code(path, text).split("\n"), 1):
            if FRONTEND_TEXT.search(unescaped(line)):
                found.append(Violation(path, number, msg("textreg_rule_frontend"), line))
    return found
