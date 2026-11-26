from __future__ import annotations

import re
import sys

TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: ")
SCISSORS = re.compile(r"^# -+ >8 -+$")
BODY_WORDS = 100
HEADER_CHARS = 72
TYPES = ("feat", "fix", "docs", "refactor", "arch", "perf", "test", "build", "ci", "chore", "style", "revert")
HEADER = re.compile(rf"(?:{'|'.join(TYPES)})(?:\([a-zA-Z0-9 ._/-]+\))?!?: .+")
EXEMPT = ("Merge ", "Revert ", "Reapply ", "fixup! ", "squash! ", "amend! ")


def header_fault(header: str) -> str:
    if header.startswith(EXEMPT):
        return "exempt"
    if not HEADER.fullmatch(header):
        return "format"
    return f"long {len(header)}" if len(header) > HEADER_CHARS else "ok"


def without_template(lines: list[str]) -> list[str]:
    cut = next((index for index, line in enumerate(lines) if SCISSORS.match(line.strip())), len(lines))
    return [line for line in lines[:cut] if not line.lstrip().startswith("#")]


def body_words(text: str, title: bool = True, template: bool = False) -> int:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if template:
        lines = without_template(lines)
    if title:
        while lines and not lines[0].strip():
            lines.pop(0)
        lines = lines[1:]
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if paragraphs and all(TRAILER.match(line) for line in paragraphs[-1]):
        paragraphs.pop()
    return sum(len(line.split()) for paragraph in paragraphs for line in paragraph)


def main(argv: list[str]) -> int:
    if argv[1:2] == ["--header"]:
        print(header_fault(argv[2] if len(argv) > 2 else ""))
        return 0
    with open(argv[1], encoding="utf-8", errors="replace") as handle:
        print(body_words(handle.read(), template="--stored" not in argv[2:]), BODY_WORDS)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
