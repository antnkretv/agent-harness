from __future__ import annotations

from session_results import session_result
import json
import re
from pathlib import Path
from ci_common import forge, msg
from agent_models import Outcome, Session, SessionError
import review_inputs
from markdown_text import without_code
from text_primitives import CYRILLIC
from review_trust import BASE_CONFIG
import session_cost
import session_log

VERDICTS = ("approve", "changes", "blocked")
LATIN = re.compile(r"[A-Za-z]")


def check_body(provider: str, body: str) -> None:
    limit = review_inputs.body_chars()
    if len(body) > limit:
        raise SessionError(msg("agent_run_review_body_long", provider, len(body), limit))
    prose = without_code(body)
    if len(LATIN.findall(prose)) > len(CYRILLIC.findall(prose)):
        raise SessionError(msg("agent_run_review_body_language", provider))


def events(log: Path) -> list[dict]:
    found = []
    for row in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(row)
        except ValueError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found


def stream_outcome(session: Session, log: Path, code: int) -> Outcome:
    result = session_result(str(log)) or {}
    text = result.get("result") if isinstance(result.get("result"), str) else ""
    ok = code == 0 and result.get("subtype") == "success" and result.get("is_error") is False
    return Outcome(ok=ok, text=text, structured=result.get("structured_output"), usage=session_cost.usage(result))


def codex_outcome(session: Session, log: Path, code: int) -> Outcome:
    completed, failure = False, None
    turns = []
    for event in events(log):
        if event.get("type") == "turn.completed":
            completed = True
            turns.append(session_cost.usage(event))
        elif event.get("type") in ("turn.failed", "error"):
            failure = event
    final = session.temp / "last-message.md"
    text = final.read_text(encoding="utf-8") if final.is_file() else ""
    usage = session_cost.total(turns)
    if code == 0 and completed and failure is None and text.strip():
        return Outcome(ok=True, text=text, usage=usage)
    return Outcome(text=failure_text(failure), usage=usage)


def failure_text(event: dict | None) -> str:
    if not event:
        return ""
    error = event.get("error")
    message = error.get("message") if isinstance(error, dict) else event.get("message")
    return message if isinstance(message, str) else ""


def json_object(text: str) -> object:
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    if "{" in text:
        candidates.append(text[text.find("{"):text.rfind("}") + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def valid_comment(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("path"), str) and bool(item["path"].strip()) \
        and isinstance(item.get("line"), int) and not isinstance(item["line"], bool) and item["line"] > 0 \
        and isinstance(item.get("body"), str) and item.get("side", forge.RIGHT) in forge.SIDES


def listed(found: object) -> list[str]:
    items = [item.strip() for item in found if isinstance(item, str)] if isinstance(found, list) else []
    return items if items and all(items) and len(items) == len(found) else []


def coverage(log: Path, tree: Path, inputs: str, journal: str, required: bool = True) -> dict:
    read = sorted(path for path in session_log.reads(events(log), str(tree)) if not path.startswith(BASE_CONFIG + "/"))
    if required and not read:
        raise SessionError(msg("agent_run_review_unread", journal))
    wanted = review_inputs.expected(inputs) if inputs else []
    return {"journal": journal, "read": read, "expected": wanted, "unread": review_inputs.unread(read, wanted)}


def coverage_report(found: dict) -> list[str]:
    lines = [msg("coverage_journal", found["journal"]), msg("coverage_read", len(found["read"]))]
    lines += [f"  {path}" for path in found["read"]]
    lines.append(msg("coverage_expected", len(found["expected"]), len(found["unread"])))
    return lines + [f"  {path}" for path in found["unread"]]


def verdict(provider: str, outcome: Outcome, *, purpose: str = "review") -> tuple[str, list[dict]]:
    data = outcome.structured if isinstance(outcome.structured, dict) else json_object(outcome.text)
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS or not isinstance(data.get("body"), str):
        raise SessionError(msg("agent_run_review_invalid", provider, "verdict, body"))
    comments = data.get("comments", [])
    if not isinstance(comments, list) or not all(valid_comment(item) for item in comments):
        raise SessionError(msg("agent_run_review_invalid", provider, "comments"))
    body = data["body"].strip()
    if purpose == "process-audit" and not body:
        raise SessionError(msg("agent_run_audit_body_missing", provider))
    if data["verdict"] == "blocked":
        raise SessionError("\n".join(filter(None, (msg("agent_run_review_blocked", provider), body))))
    if purpose == "review":
        check_body(provider, body)
    return (body + "\nVERDICT: " + data["verdict"],
            [{"path": item["path"], "line": item["line"], "side": item.get("side", forge.RIGHT), "body": item["body"]}
             for item in comments])
