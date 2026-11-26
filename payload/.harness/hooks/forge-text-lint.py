#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

TEXT_FIELDS = ("body", "title", "message", "commit_message", "description")
BODY_FIELDS = ("body", "description")
COMMIT_FIELDS = ("message", "commit_message")

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(ROOT, ".harness", "lib"))
import agent_hook
import forge
from authorship import signatures, signed
from commit_text import BODY_WORDS, HEADER_CHARS, body_words, header_fault
from manifest import get, get_list, setting
from platform_messages import msg
from text_data import altered, marked, rendered

METHODOLOGY = "methodology.json"
WORD_CAPS = {"comment": 200, "commit": BODY_WORDS}


def words(field: str, text: str) -> int:
    return body_words(text, title=field == "message") if field in COMMIT_FIELDS else len(text.split())


def word_cap(tools, action: str, field: str) -> int | None:
    if field in COMMIT_FIELDS:
        return WORD_CAPS["commit"]
    if field in ("body", "description") and action in tools.comments:
        return WORD_CAPS["comment"]
    return None


def defer_phrases() -> tuple[str, ...]:
    return tuple(get_list("policies.findings.defer", (), file=METHODOLOGY))


def waiver() -> str:
    return get("policies.findings.waiver", "", file=METHODOLOGY)


def own_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def deferrals(text: str) -> list[str]:
    phrases, mark = defer_phrases(), waiver()
    if not phrases or not mark:
        return []
    if re.search(rf"^\s*{re.escape(mark)}\s*\S", text, re.I | re.M):
        return []
    body = own_text(text).lower()
    return [phrase for phrase in phrases if phrase in body]


def verdict(text: str) -> bool:
    mark = get("processes.review.fallback.mark", "", file=METHODOLOGY)
    return bool(mark) and marked(text, "review/verdict").get("ReviewMode", "").startswith(mark)


def title_faults(title: str) -> list[str]:
    if "\n" in title.strip():
        return [msg("textlint_title_lines")]
    title = forge.merged_title(title.strip())
    fault = header_fault(title)
    if fault == "format":
        return [msg("textlint_title_format", title)]
    if fault.startswith("long "):
        return [msg("textlint_title_long", fault[len("long "):], HEADER_CHARS)]
    return []


def project_name() -> str:
    return get("project.name", msg("textlint_repo_fallback"))


def evaluate(payload: dict) -> str | None:
    tool = payload.get("tool_name", "")
    tools = forge.tools
    if tools is None or not tool.startswith(tools.prefix):
        return None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    chunks = []
    for field in TEXT_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            chunks.append((field, value))
    merge = tools.merge if tools.merge and tool[len(tools.prefix):] == tools.merge.action else None
    number = tool_input.get(merge.number) if merge else None
    if merge and merge.method and tool_input.get(merge.method) == merge.no_commit:
        number = None
    url = forge.change_url(number) if number else ""
    trailer = "PR: " + url if url else ""
    absent = [] if not trailer or str(tool_input.get(merge.body) or "").strip() else [
        msg("textlint_change_trailer", merge.body, trailer, number)]
    action = tool[len(tools.prefix):]
    if action in tools.opens and not any(field in BODY_FIELDS for field, _ in chunks):
        absent.append(msg("textlint_change_body", "/".join(BODY_FIELDS)))
    if not chunks and not absent:
        return None

    linter = os.path.join(ROOT, ".harness", "scripts", "check", "vale-lint.py")
    lints = os.path.isfile(linter)

    checks_defer = action not in tools.documents and setting("agents", "findings") == "same-pr"
    style: list[str] = []
    defer: list[str] = []
    unsigned: list[str] = absent
    marks = signatures()
    capped = payload.get("capped", True)
    for field, text in chunks:
        reviewed = field == "body" and action in tools.comments and verdict(text)
        cap = word_cap(tools, action, field) if capped and not reviewed else None
        count = words(field, text) if cap is not None else 0
        if cap is not None and count > cap:
            style.append(msg("textlint_words_over", field, count, cap))
        if marks and field == "body" and action in tools.comments and not signed(text, marks):
            unsigned.append(msg("textlint_unsigned", field, marks[0]))
        if field in BODY_FIELDS and not rendered(text):
            unsigned.append(msg("textlint_unmarked", field))
        if field == "title" and action in tools.changes:
            unsigned.extend(title_faults(text))
        if template := altered(text):
            unsigned.append(msg("textlint_altered", field, template))
        if trailer and field == merge.body and trailer not in text.splitlines():
            unsigned.append(msg("textlint_change_trailer", field, trailer, number))
        found = deferrals(text) if checks_defer and not reviewed else []
        if found:
            defer.append(msg("textlint_deferred", field, ', '.join(found)))
        if not lints:
            continue
        result = subprocess.run(
            [sys.executable, linter, "--stdin", *(("--ext", ".commit.md") if field in COMMIT_FIELDS else ()),
             "--label", msg("textlint_field_label", field)],
            input=text.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            style.append(result.stderr.decode("utf-8", "replace").strip())

    if not style and not defer and not unsigned:
        return None

    notes = []
    if defer:
        doc = get("policies.findings.doc", "", file=METHODOLOGY)
        notes.append(msg("textlint_defer_note", f" ({doc})" if doc else "", waiver()))

    return (
        msg("textlint_failed", project_name()) + "\n"
        + "\n".join(defer + unsigned + style)
        + "\n\n" + "\n".join(notes + [msg("textlint_retry")])
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin.buffer)
    except (json.JSONDecodeError, ValueError):
        return 0

    try:
        reason = evaluate(payload)
    except Exception as error:
        reason = msg("textlint_fault", f"{type(error).__name__}: {error}")
    if reason:
        agent = sys.argv[1] if len(sys.argv) > 1 else ""
        print(json.dumps(agent_hook.answer(agent, reason) if agent else {"deny": True, "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
