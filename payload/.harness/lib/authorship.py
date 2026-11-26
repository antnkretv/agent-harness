from __future__ import annotations

import os

from manifest import get_list
from markdown_text import without_code


def signatures() -> list[str]:
    return [mark for mark in get_list("policies.authorship.signatures", [], file="methodology.json") if mark]


def plain(text: str) -> str:
    try:
        return without_code(text)
    except RuntimeError:
        return text


def signed(text: str, marks: list[str] | None = None) -> bool:
    lines = [line.strip() for line in plain(text).splitlines() if not line.strip().startswith(">")]
    meaningful = [line for line in lines if line]
    shown = [plain(mark).strip() for mark in (signatures() if marks is None else marks)]
    return bool(meaningful) and any(meaningful[-1].startswith(mark) for mark in shown if mark)


def agent_commit(body: str, emails: tuple[str, ...] = ()) -> bool:
    trailers = [mark.lower() for mark in get_list("policies.authorship.commitTrailers", [], file="methodology.json") if mark]
    known = {mail.lower() for mail in get_list("policies.authorship.commitEmails", [], file="methodology.json") if mail}
    text = body.lower()
    return (any(line.strip().startswith(mark) for line in text.splitlines() for mark in trailers)
            or any(mail.lower() in known for mail in emails) or any(mail in text for mail in known))


def session_agent() -> str:
    for pair in get_list("policies.authorship.agentEnv", [], file="methodology.json"):
        name, _, agent = str(pair).partition("=")
        if os.environ.get(name):
            return agent or name
    return ""


def sign(text: str) -> str:
    marks = signatures()
    if not marks or signed(text, marks):
        return text
    return text.rstrip() + "\n\n" + marks[0] + "\n"


def unsigned(text: str) -> str:
    marks = signatures()
    lines = text.rstrip().split("\n")
    if marks and signed(text, marks):
        lines.pop()
    return "\n".join(lines).rstrip() + "\n"
