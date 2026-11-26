from __future__ import annotations

import ast
import functools
import io
import re
import runpy
import tokenize

from installer.catalog import owner, pack_skills

from .context import KIT, SHIPPED, catalog, msg
from .source import tree_files

TOOL_COMMENT = re.compile(r"(?:harness|project):(?:begin|end)\b|^(?:-\*-|pragma|ruff:|fmt:|isort:|mypy:|pyright:|requires\s|"
                          r"yaml-language-server:)", re.IGNORECASE)
LINE_COMMENT = {("#", ";"): (".ini", ".editorconfig"),
                ("#",): (".env", ".gitattributes", ".gitignore", ".dockerignore", ".rules"),
                ("<!--",): (".props", ".targets", ".csproj")}


@functools.cache
def comment_parser():
    """Разбор комментариев - гейта комментариев из payload: проверка не держит второй разбор языков."""
    scripts = KIT / "payload" / ".harness" / "scripts"
    runpy.run_path(str(scripts / "script_paths.py"))
    import comment_scan

    return comment_scan


def tool_comment(body: str) -> bool:
    return bool(comment_parser().DIRECTIVE.match(body) or TOOL_COMMENT.search(body))


def python_comments(text: str) -> list[int]:
    """Комментарии и docstring Python. Docstring модуля, который код читает через __doc__, - справка скрипта."""
    found = [token.start[0] for token in tokenize.generate_tokens(io.StringIO(text).readline)
             if token.type == tokenize.COMMENT and not token.string.startswith("#!")
             and not tool_comment(token.string.lstrip("#").strip())]
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body \
                and ast.get_docstring(node, clean=False) is not None \
                and not (isinstance(node, ast.Module) and "__doc__" in text):
            found.append(node.body[0].lineno)
    return found


def comments(path: str, text: str) -> list[int]:
    if path.endswith(".py"):
        return python_comments(text)
    for markers, suffixes in LINE_COMMENT.items():
        if path.endswith(suffixes):
            return [number for number, line in enumerate(text.splitlines(), start=1) if line.lstrip().startswith(markers)
                    and not tool_comment(line.strip().lstrip("#;").removeprefix("<!--").strip())]
    parser = comment_parser()
    if not parser.family(path, text):
        return []
    printed = parser.help_lines(text)
    return sorted(parser.pragma_lines(text)) + [number for number, body, _kind in parser.comment_lines(path, text)
                                                if number not in printed and not tool_comment(body)]


def comment_problems(kit: dict) -> list[str]:
    problems = []
    packs = pack_skills(catalog())
    vendored = {f"payload/{path}" for path, meta in kit["files"].items() if owner(meta) in packs}
    for path in [*SHIPPED, *tree_files()]:
        if path.startswith("payload/.specify/") or path in vendored:
            continue
        try:
            text = (KIT / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = comments(path, text)
        if found:
            problems.append(msg("kit_commented", path, min(found)))
    return problems


