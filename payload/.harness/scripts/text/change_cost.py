from __future__ import annotations

import json
from pathlib import Path

import review_policy
import session_cost
import text_templates
from ci_common import forge, moment, msg
from text_data import mark, marked, marks, opening, unmarked

TOKENS = ("in", "cache_read", "cache_write", "out")
FIELDS = dict(zip(TOKENS, ("input", "cache_read", "cache_write", "output")))
COUNTS = (*TOKENS, "seconds")
ZERO = {**dict.fromkeys(COUNTS, 0), "model": ""}
JOURNALED = ("claude", "codex")
DESCRIPTIONS = ("change/description", "change/cost")
RESULTS = ("comment/round-result", "comment/cost-revision")


def review(items: list[tuple[str, str]]) -> dict:
    total, heads, agents, spans, done, unknown, used = dict.fromkeys(COUNTS, 0), set(), set(), [], {}, set(), []
    for body, created in items:
        data = marked(body or "", "review/verdict")
        if not data:
            continue
        heads.add(data.get("SHA", ""))
        done.setdefault(data.get("SHA", ""), []).append(moment(created))
        agents.add(data.get("Agent", ""))
        try:
            found = json.loads(data.get("SessionUsage") or "{}")
        except ValueError:
            found = None
        used.append(part(found, "SessionInputTokens" in data))
        if "SessionInputTokens" in data and not {"SessionCacheReadTokens", "SessionCacheWriteTokens"} & data.keys():
            unknown.update(("cache_read", "cache_write"))
        for key, field in (("in", "SessionInputTokens"), ("cache_read", "SessionCacheReadTokens"),
                           ("cache_write", "SessionCacheWriteTokens"), ("out", "SessionOutputTokens"),
                           ("seconds", "SessionSeconds")):
            total[key] += int(data[field]) if str(data.get(field, "")).isdigit() else 0
        if str(data.get("SessionSeconds", "")).isdigit() and moment(created):
            spans.append((moment(created) - int(data["SessionSeconds"]), moment(created)))
    return {**{key: value for key, value in total.items() if key not in unknown}, "usage": whole(used),
            "rounds": len(heads - {""}), "heads": frozenset(heads - {""}), "agents": frozenset(agents - {""}),
            "spans": tuple(spans), "done": done}


def part(found: object, spent: bool) -> dict | None:
    return session_cost.table(found) if found else None if spent else {}


def whole(used: list) -> dict:
    return session_cost.combine(*used) if None not in used else {}


def windows(spent: dict, rounds: list[dict]) -> tuple[tuple[float, float], ...]:
    spans = list(spent["spans"])
    for item in rounds:
        start = moment(str(item.get("at", "")))
        end = min((value for value in spent["done"].get(item.get("head", ""), []) if value > start), default=0.0)
        if start and end:
            spans.append((start, end))
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def own(state: dict, spent: dict, known: dict | None = None) -> dict:
    path = transcript(state)
    since = min((str(value) for value in (state.get("since"), (known or {}).get("since")) if value),
                key=moment, default="")
    dev = session_cost.session(path, spent["agents"], windows(spent, state.get("rounds", [])), moment(since)) \
        if path else None
    if not dev or not dev["read"]:
        return {**ZERO, "since": since}
    return {**{key: int(dev["tokens"].get(field, 0)) for key, field in FIELDS.items()},
            "seconds": int(dev["active"]), "model": session_cost.label(dev["model"], dev["effort"], session_cost.fast(dev["tiers"], dev["model"])),
            "usage": session_cost.rounded(dev["tiers"]), "since": since, "journal": True}


def empty(item: dict) -> bool:
    return not (item.get("in") or item.get("out") or item.get("seconds"))


def missing(item: dict) -> bool:
    return empty(item) and not item.get("journal")


def transcript(state: dict) -> str:
    return state.get("transcript") or session_cost.located(state.get("session", ""))


def journaled(state: dict) -> bool:
    return state.get("agent") in JOURNALED or bool(transcript(state))


def pending(state: dict) -> bool:
    return not transcript(state) and state.get("agent") in JOURNALED


def entry(text: str, template: str) -> tuple[str, dict] | None:
    data = marked(text or "", template)
    try:
        item = json.loads(data["Dev"])
        return (data["Session"], item) if isinstance(item, dict) else None
    except (KeyError, ValueError, TypeError):
        return None


def records(body: str, notes: list, login: str) -> dict[str, dict]:
    texts = [(body, template) for template in DESCRIPTIONS] + [(item.body, template) for item in notes
                                                               if item.author == login for template in RESULTS]
    return dict(filter(None, (entry(text, template) for text, template in texts)))


def tokens(item: dict, name: str = "") -> str:
    return session_cost.tokens_table(session_cost.table(item.get("usage")) or {},
                                     {field: item[key] for key, field in FIELDS.items() if key in item}, name)


def values(item: dict) -> dict[str, str]:
    return {"SessionModel": str(item.get("model", "")),
            "DevTime": session_cost.duration(item["seconds"]) if item.get("seconds") else "",
            "DevTokens": tokens(item, str(item.get("model", ""))),
            **({"DevCost": msg("cost_unknown")} if empty(item) and item.get("journal") else {})}


def stats(item: dict) -> str:
    return text_templates.render("parts/change-stats", {"SessionLink": item.get("link", ""),
                                                        "EntryTerms": item.get("terms", ""), **values(item)})


def round_of(number: int, head: str) -> str:
    try:
        return str(len(spent_of(forge.notes(number), forge.reviews(number))["heads"] | {head}))
    except forge.ForgeError:
        return ""


def spent_of(notes: list, reviews: list) -> dict:
    return review([(item.body, item.created) for item in [*notes, *reviews]])


def parts(body: str) -> tuple[str, str]:
    cut = max((match for match, item in marks(body) if item.get("template") in DESCRIPTIONS),
              key=lambda match: match.end(), default=None)
    return (body[:cut.start()], body[cut.end():].strip()) if cut else (body, "")


def authored(body: str) -> str:
    head, tail = parts(body.replace("\r\n", "\n"))
    found = kept(body)
    block = stats(found[1]) if found else ""
    head = head.rstrip()
    return unmarked((head[:-len(block)] if block and head.endswith(block) else head) + "\n\n" + tail).strip()


def kept(body: str) -> tuple[str, dict] | None:
    return next(filter(None, (entry(body, template) for template in DESCRIPTIONS)), None)


def work_tree() -> Path:
    import pipeline_next

    return Path(pipeline_next.git(Path.cwd(), "rev-parse", "--show-toplevel"))


def state(session: str, work: Path | None = None) -> dict:
    import pipeline_next

    return pipeline_next.resolve(work or work_tree(), session)


def dev(session: str, number: int | None = None) -> dict:
    work = work_tree()
    found = state(session, work)
    spent = spent_of(forge.notes(number), forge.reviews(number)) if number is not None else review([])
    import pipeline_next

    return {**own(found, spent), "terms": pipeline_next.terms(found), **documents(found, work)}


def reread(session: str, item: dict) -> dict:
    return {**item, **documents(state(session), work_tree())}


def documents(state: dict, work: Path) -> dict:
    if forge.in_ci() or not (path := transcript(state)):
        return {}
    import pipeline_next
    import session_log

    since = moment(state.get("since", ""))
    items = [item for log in (path, *session_cost.subagents(path)) for item in session_log.journal(log, since)]
    docs = session_log.top(session_log.reads(items, str(work), str(pipeline_next.common(work).parent), skills=True))
    return {"documents": docs, "kind": pipeline_next.agent_kind(state.get("agent", ""))} if docs else {}


def round_result(number: int, state: dict, head: str, verdict: str) -> None:
    notes, login = forge.notes(number), forge.login()
    spent = spent_of(notes, forge.reviews(number))
    written = {(data.get("Session"), data.get("SHA"), data.get("Result")) for data in
               (marked(item.body, "comment/round-result") for item in notes if item.author == login)}
    if head not in spent["heads"] or (state["session"], head, verdict) in written:
        return
    before = records(forge.change_body(number), notes, login).get(state["session"], ZERO)
    current = own(state, spent, before)
    delta = {**{key: max(0, current[key] - int(before[key])) for key in COUNTS if key in before},
             "model": current["model"]}
    was = part(before.get("usage"), bool(before.get("in") or before.get("out")))
    if was is not None:
        delta["usage"] = {name: {field: max(0, value - (was.get(name) or {}).get(field, 0))
                                 for field, value in found.items()}
                          for name, found in (session_cost.table(current.get("usage")) or {}).items()}
    text = text_templates.render("comment/round-result", {
        **values(delta), "ReviewRound": str(spent["rounds"]), "ReviewIcon": review_policy.icon(verdict),
        "RoundResult": text_templates.choice("RoundResult", verdict)})
    record = mark("comment/round-result", {"Session": state["session"], "SHA": head, "Result": verdict,
                                            "Dev": json.dumps(current, sort_keys=True)})
    forge.comment(number, f"{text}\n\n{record}")


def publish(number: int, state: dict) -> None:
    notes = forge.notes(number)
    spent = spent_of(notes, forge.reviews(number))
    sessions = records(forge.change_body(number), notes, forge.login())
    current = own(state, spent, sessions.get(state["session"]))
    if current["in"] or current["out"] or current["seconds"]:
        sessions[state["session"]] = current
    total = {key: sum(int(item[key]) for item in sessions.values()) for key in COUNTS
             if all(key in item for item in sessions.values())}
    lead = sessions.get(state["session"]) or (list(sessions.values())[-1] if sessions else ZERO)
    usage = {"dev": session_cost.rounded(whole([part(item.get("usage"), bool(item.get("in") or item.get("out")))
                                                for item in sessions.values()])),
             "review": session_cost.rounded(spent["usage"])}
    dev = {**total, "model": lead.get("model", ""), "usage": usage["dev"]}
    text = text_templates.render("comment/cost", {
        **values(dev), "DevTokens": tokens(dev, dev["model"] if len(sessions) == 1 else ""),
        "ReviewRounds": str(spent["rounds"]), "ReviewTime": session_cost.duration(spent["seconds"]),
        "ReviewTokens": tokens(spent)})
    record = mark("comment/cost", {"Usage": json.dumps(usage, sort_keys=True)})
    forge.comment(number, f"{text}\n\n{record}", opening("comment/cost"), last=True)
