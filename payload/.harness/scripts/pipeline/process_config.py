from __future__ import annotations
import os
import re
from collections.abc import Mapping
from ci_common import ROOT, get, get_records, msg, processes
from text_templates import fill
from manifest import extensions, get_record
from agent_provider import KINDS
from review_inputs import body_chars

METHODOLOGY = "methodology.json"

RUNNER_KINDS = tuple(KINDS)

BASH_RULE = re.compile(r"Bash\(([^:()]+):\*\)")


def passes() -> list[dict]:
    return processes().get("review", {}).get("steps", [])


def agent_prompts() -> list[dict]:
    return get_records("prompts.agents", [], file=METHODOLOGY)


def phrases() -> list[dict]:
    return get_records("prompts.phrases", [], file=METHODOLOGY)


def dev_kind() -> str:
    return next(iter(extensions("agents")), "")


def ci_kind() -> str:
    return os.environ.get("AGENT_PROVIDER") or next(iter(extensions("ci-agent")), "") or dev_kind()


def effort(name: str) -> str:
    return get(f"efforts.{name}", "", file=METHODOLOGY)


def tools(name: str) -> dict:
    return get_record(f"agents.tools.{name}", {})


def runner_for(name: str, kind: str) -> dict:
    if kind not in RUNNER_KINDS:
        raise ValueError(msg("pipeline_runner_kind_unknown", name, repr(kind), ', '.join(RUNNER_KINDS)))
    spec = KINDS[kind]
    found = {"kind": kind, "model": spec["model"], "effort": spec["efforts"].get(effort(name), "")}
    if spec.get("commonPrompt"):
        found["commonPrompt"] = spec["commonPrompt"]
    return {**found, **tools(name)}


def selected_runner(name: str) -> dict:
    return runner_for(name, ci_kind())


def phrase(name: str) -> str:
    return next((str(item.get("text", "")) for item in phrases() if item.get("id") == name), "")


def process_entries() -> list[dict]:
    return [{"id": name, **entry} for name, entry in processes().items() if name != "review"]


def fallback(key: str, default):
    return get(f"processes.review.fallback.{key}", default, file=METHODOLOGY)


def prompt_text(entry: dict) -> str:
    paths = [get("processes.review.commonPrompt", "", file=METHODOLOGY), entry.get("prompt", "")]
    parts = []
    for path in paths:
        if not path:
            continue
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            parts.append(handle.read().strip())
    return "\n\n".join(parts) + "\n"


def pass_values(entry: dict, change: str = "", data: str = "", decided: str = "") -> dict[str, str]:
    return {
        "ReviewType": entry.get("marker", ""),
        "ReviewBase": ROOT,
        "ReviewBodyChars": str(body_chars()),
        "ChangeLink": change,
        "ReviewInput": data or phrase("input-missing"),
        "ReviewDecided": decided.strip() or phrase("decided-missing"),
    }


def session(name: str, change: str = "", data: str = "", decided: str = "") -> dict:
    entry = next((p for p in passes() if p.get("id") == name), None)
    if entry is None:
        raise ValueError(msg("pipeline_no_pass", name))
    values = pass_values(entry, change, data, decided)
    try:
        prompt = prompt_text(entry)
    except OSError:
        raise ValueError(msg("pipeline_no_prompt_files", name)) from None
    runner = selected_runner(str(entry.get("runner", "")))
    return {
        "kind": runner.get("kind", ""),
        "model": runner.get("model", ""),
        "effort": runner.get("effort", ""),
        "prompt": fill(prompt, values),
        "context": bool(entry.get("context")),
        "context_chars": get("processes.review.contextChars", 0, file=METHODOLOGY),
    }


def agent_text(entry: dict, runner: dict | None = None) -> str:
    parts = []
    common = (runner or {}).get("commonPrompt", entry.get("commonPrompt"))
    for path in (entry.get("prompt", ""), common):
        if path is None:
            continue
        try:
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                parts.append(handle.read().strip())
        except OSError:
            raise ValueError(msg("pipeline_no_agent_prompt", entry.get("id"), path)) from None
    return "\n\n".join(parts) + "\n"


def agent_values(entry: dict) -> dict[str, str]:
    allowed = str(tools(str(entry.get("runner", ""))).get("allowedTools", ""))
    rules = (BASH_RULE.fullmatch(item.strip()) for item in allowed.split(","))
    return {"BashPrefixes": ", ".join(f"`{rule.group(1)}`" for rule in rules if rule),
            "ReviewLabel": get("processes.review.label", "", file=METHODOLOGY),
            "ManagedLabel": get("processes.review.managedLabel", "", file=METHODOLOGY)}


def agent_session(name: str, values: Mapping[str, str]) -> tuple[dict, str]:
    entry = next((item for item in agent_prompts() if item.get("id") == name), None)
    if not entry or not entry.get("runner"):
        raise ValueError(msg("pipeline_no_agent_runner", name))
    chosen = selected_runner(str(entry["runner"]))
    model = chosen["model"]
    if not chosen.get("allowedTools"):
        raise ValueError(msg("pipeline_agent_tools_unset", name, "allowedTools"))
    fields = {
        "kind": str(chosen.get("kind", "")),
        "model": str(model),
        "effort": str(chosen.get("effort", "")),
        "allowed_tools": str(chosen["allowedTools"]),
        "disallowed_tools": str(chosen.get("disallowedTools", "")),
    }
    text = agent_text(entry, chosen)
    return fields, fill(text, {**agent_values(entry), **values}, os.environ)
