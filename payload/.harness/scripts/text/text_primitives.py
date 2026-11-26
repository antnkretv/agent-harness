from __future__ import annotations
import os
import re
from dataclasses import dataclass

KEY = r"[a-z][a-z0-9_]*"


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    snippet: str


def words(text: str) -> bool:
    return bool(CYRILLIC.search(text) or LATIN_PROSE.search(text))


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def shebang(text: str) -> str:
    return text.split("\n", 1)[0] if text.startswith("#!") else ""


def is_shell(path: str, text: str) -> bool:
    if path.endswith((".sh", ".bash")) or path.startswith(".githooks/"):
        return True
    if "." in os.path.basename(path):
        return False
    return bool(re.search(r"\b(ba)?sh\b", shebang(text)))


CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN_PROSE = re.compile(r"(?<![\w./-])[A-Za-z]{2,}(?:[,:]?[ \t]+[A-Za-z]{2,}){2}(?![\w/-])")
