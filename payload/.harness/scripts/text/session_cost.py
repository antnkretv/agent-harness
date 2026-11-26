from __future__ import annotations

import glob
import json
import math
import os
from collections.abc import Iterable

from ci_common import moment, msg

USAGE_NAMES = (
    ("input", ("input", "input_tokens", "inputTokens")),
    ("output", ("output", "output_tokens", "outputTokens")),
    ("cache_read", ("cache_read", "cache_read_input_tokens", "cacheReadInputTokens", "cacheReadTokens",
                    "cached_input_tokens")),
    ("cache_write", ("cache_write", "cache_creation_input_tokens", "cacheCreationInputTokens", "cacheWriteTokens")),
    ("cache_write_1h", ("cache_write_1h",)),
)


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def pick(usage: dict, names: tuple) -> float | None:
    for name in names:
        value = number(usage.get(name))
        if value is not None:
            return value
    return None


def usage(result: object) -> dict[str, float]:
    if not isinstance(result, dict):
        return {}
    own = result.get("usage") if isinstance(result.get("usage"), dict) else result
    found = {field: value for field, names in USAGE_NAMES if (value := pick(own, names)) is not None}
    if number(own.get("cached_input_tokens")) is not None and "input" in found:
        found["input"] = max(0.0, found["input"] - found.get("cache_read", 0.0))
    creation = own.get("cache_creation")
    if isinstance(creation, dict) and (hour := number(creation.get("ephemeral_1h_input_tokens"))) is not None:
        found["cache_write_1h"] = hour
    if found or not isinstance(result.get("modelUsage"), dict):
        return found
    return total(usage(one) for one in result["modelUsage"].values() if isinstance(one, dict))


def total(usages: Iterable[dict]) -> dict[str, float]:
    found: dict[str, float] = {}
    for one in usages:
        for field, value in one.items():
            if field != "tier":
                found[field] = found.get(field, 0.0) + value
    return found


def tier(model: str, speed: object = "") -> str:
    return f"{model} fast" if speed == "fast" else model


def tiers(calls: Iterable[dict], model: str = "") -> dict[str, dict[str, float]]:
    found: dict[str, list[dict]] = {}
    for one in calls:
        name = one.get("tier") or model
        if name and not name.startswith("<"):
            found.setdefault(name, []).append(one)
    return {name: total(items) for name, items in found.items()}


def seconds(*results: dict) -> float | None:
    for result in results:
        value = number(result.get("duration_ms"))
        if value is not None:
            return value / 1000
    return None


def label(model: str, effort: str = "", fast: bool = False) -> str:
    if not (model or "").strip():
        return ""
    return " ".join(part for part in (model.strip(), (effort or "").strip(), "fast" if fast else "") if part)


def duration(seconds: float) -> str:
    minutes = max(round(seconds / 60), 1 if seconds else 0)
    hours, rest = divmod(minutes, 60)
    if not hours:
        return msg("cost_minutes", minutes)
    return msg("cost_hours_minutes", hours, rest) if rest else msg("cost_hours", hours)


def tokens_table(found: dict[str, dict[str, float]], summed: dict[str, float] | None = None, name: str = "") -> str:
    import text_templates

    if not found and any((summed or {}).get(field) for field in ("input", "output")):
        found = {name or msg("token_absent"): summed}

    def cell(value: float | None) -> str:
        return msg("token_absent") if value is None else f"{int(value):,}".replace(",", " ")

    rows, notes = [], []
    for model, tokens in sorted(found.items()):
        write, hour = tokens.get("cache_write"), tokens.get("cache_write_1h")
        if write and hour is None:
            notes.append(text_templates.render("parts/token-note", {"TokenModel": model, "TokenWrite": cell(write)}))
        five = None if write is None or hour is None else write - hour
        rows.append("| " + " | ".join([model, *(cell(tokens.get(field)) for field in ("input", "output", "cache_read")),
                                       cell(five), cell(hour)]) + " |")
    return text_templates.render("parts/tokens", {"TokenRows": "\n".join(rows), "TokenNotes": "\n".join(notes)}) \
        if rows else ""


def rounded(found: dict[str, dict[str, float]]) -> dict[str, dict[str, int]]:
    return {name: {field: int(value) for field, value in tokens.items()} for name, tokens in found.items()}


def table(found: object) -> dict[str, dict[str, float]] | None:
    if not isinstance(found, dict) or not all(isinstance(tokens, dict) and all(number(value) is not None
                                                                            for value in tokens.values())
                                              for tokens in found.values()):
        return None
    return found


def combine(*items: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    names = sorted({name for found in items for name in found})
    return {name: total(found[name] for found in items if name in found) for name in names}


def fast(found: dict, model: str) -> bool:
    return tier(model, "fast") in found


def breakdown(found: dict[str, dict[str, float]]) -> str:
    return json.dumps(rounded(found), sort_keys=True) if found else ""


def values(tokens: dict[str, float], spent: float | None, model: str = "", effort: str = "",
           found: dict[str, dict[str, float]] | None = None) -> dict[str, str]:
    def count(field: str) -> str:
        return format(tokens[field], ".0f") if field in tokens else ""

    found = {model: tokens} if found is None and model and tokens else found or {}
    return {"SessionInputTokens": count("input"), "SessionOutputTokens": count("output"),
            "SessionCacheReadTokens": count("cache_read"), "SessionCacheWriteTokens": count("cache_write"),
            "SessionTokens": tokens_table(found, tokens, model),
            "SessionUsage": breakdown(found),
            "SessionTime": "" if spent is None else duration(spent),
            "SessionSeconds": "" if spent is None else format(spent, ".0f"),
            "SessionModel": label(model, effort, fast(found, model))}


def outcome(result: dict) -> dict[str, str]:
    model, effort = str(result.get("model") or ""), str(result.get("effort") or "")
    return values(usage(result), seconds(result), model, effort, table(result.get("tiers")) or None)


def journal(path: str, calls: dict[str, dict], tag: str = "", facts: dict | None = None,
            since: float = 0.0) -> list[float] | None:
    moments, own, final, before = [], len(calls), None, {}
    facts = {} if facts is None else facts
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            rows = handle.read().splitlines()
    except OSError:
        return None
    for row in rows:
        try:
            item = json.loads(row)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        stamp = moment(item.get("timestamp")) or None
        kind, payload = item.get("type"), item.get("payload") if isinstance(item.get("payload"), dict) else {}
        info = payload.get("info") if kind == "event_msg" and payload.get("type") == "token_count" else None
        totals = info.get("total_token_usage") if isinstance(info, dict) else None
        if stamp is not None and stamp < since:
            before = usage(totals) if isinstance(totals, dict) else before
            continue
        if stamp is not None:
            moments.append(stamp)
        if isinstance(item.get("agentId"), str):
            facts.setdefault("agent", item["agentId"])
        message = item.get("message") if kind == "assistant" else None
        if isinstance(message, dict):
            if isinstance(message.get("model"), str) and not message["model"].startswith("<"):
                facts["model"] = message["model"]
            if isinstance(item.get("effort"), str):
                facts["effort"] = item["effort"]
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            calls[tag + str(message.get("id") or len(calls))] = {
                **usage(message), "tier": tier(str(message.get("model") or ""), message["usage"].get("speed"))}
        elif kind == "turn.completed":
            calls[tag + str(len(calls))] = usage(item)
        elif kind == "turn_context":
            facts.update({key: str(payload[key]) for key in ("model", "effort") if payload.get(key)})
        elif isinstance(totals, dict):
            calls[tag + "codex"] = {key: value - before.get(key, 0) for key, value in usage(totals).items()}
        elif kind == "system" and item.get("subtype") == "init" and isinstance(item.get("model"), str):
            facts["model"] = item["model"]
        elif kind == "result" and "comments" not in item:
            final = item
    if final is not None and len(calls) == own and usage(final):
        calls[tag + "result"] = usage(final)
    return moments


IDLE = 600


def active(moments: list[float], windows: tuple[tuple[float, float], ...] = ()) -> float:
    ordered, spent = sorted(moments), 0.0
    for earlier, later in zip(ordered, ordered[1:]):
        if later - earlier > IDLE:
            continue
        cut = sum(max(0.0, min(later, end) - max(earlier, start)) for start, end in windows)
        spent += max(0.0, later - earlier - cut)
    return spent


def located(session: str) -> str:
    if not session or not session.replace("-", "").isalnum():
        return ""
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    codex = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    found = glob.glob(os.path.join(glob.escape(claude), "projects", "*", session + ".jsonl"))
    found += glob.glob(os.path.join(glob.escape(codex), "sessions", "**", f"rollout-*{session}.jsonl"), recursive=True)
    return max(found, key=os.path.getmtime) if found else ""


def subagents(path: str) -> list[str]:
    return sorted(glob.glob(os.path.join(glob.escape(os.path.splitext(path)[0]), "subagents", "*.jsonl")))


def session(path: str, skip: frozenset[str] = frozenset(),
            windows: tuple[tuple[float, float], ...] = (), since: float = 0.0) -> dict:
    calls: dict[str, dict] = {}
    facts: dict = {}
    moments = journal(path, calls, facts=facts, since=since)
    if moments is None:
        return {"tokens": {}, "tiers": {}, "seconds": None, "active": 0.0, "model": "", "effort": "", "agent": "",
                "read": False}
    spent = active(moments, windows)
    for sub in subagents(path):
        own: dict[str, dict] = {}
        about: dict = {}
        found = journal(sub, own, os.path.basename(sub) + ":", about, since)
        if about.get("agent") in skip:
            continue
        calls.update(own)
        spent += active(found or [])
    return {"tokens": total(calls.values()), "tiers": tiers(calls.values(), facts.get("model", "")),
            "seconds": (max(moments) - min(moments)) if len(moments) > 1 else None,
            "active": spent, "model": facts.get("model", ""), "effort": facts.get("effort", ""),
            "agent": facts.get("agent", ""), "read": True}


MARKED = ("SessionInputTokens", "SessionCacheReadTokens", "SessionCacheWriteTokens", "SessionOutputTokens", "SessionUsage",
          "SessionSeconds", "SessionModel", "Agent")


def marked(found: dict[str, str]) -> dict[str, str]:
    return {key: found[key] for key in MARKED if found.get(key)}


def cost(path: str) -> dict[str, str]:
    found = session(path)
    return {**values(found["tokens"], found["seconds"], found["model"], found["effort"], found["tiers"]),
            "Agent": found["agent"]}
