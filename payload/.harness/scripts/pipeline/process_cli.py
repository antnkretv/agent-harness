from __future__ import annotations
import argparse
import json
import os
import sys
from ci_common import forge, msg
from text_templates import fill, variables
from process_config import agent_prompts, agent_session, agent_text, agent_values, fallback, passes, phrase, phrases, process_entries, selected_runner, session
from process_validation import MANIFEST, check_authorship, check_tool_copies, check_contract, check_docs_free_of_markers, check_fact_copies, check_fallback, check_findings, check_gitlab_root, check_graph, check_installed_manifest, check_handoff, check_kinds, check_paths, check_prompts, check_review, check_workflow, diverged


def emit_fallback() -> int:
    if not fallback("label", ""):
        print(msg("pipeline_no_fallback"), file=sys.stderr)
        return 1
    for key in ("label", "mark", "runner", "waitMinutes", "pollMinutes", "retries", "timeoutMinutes"):
        print(f"{key}={fallback(key, '')}")
    return 0


def given() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in variables()}


def emit_agent(name: str) -> int:
    fields, prompt = agent_session(name, given())
    for key, value in fields.items():
        print(f"{key}={value}")
    print("prompt<<PIPELINE_PROMPT_EOF")
    print(prompt.rstrip("\n"))
    print("PIPELINE_PROMPT_EOF")
    return 0


def emit_provider(name: str) -> int:
    entry = next((entry for entry in agent_prompts() if entry.get("id") == name), None)
    if entry is None:
        raise ValueError(msg("pipeline_no_agent_runner", name))
    print(selected_runner(str(entry.get("runner", "")))["kind"])
    return 0


def emit_prompt(name: str) -> int:
    entry = next((item for item in agent_prompts() if item.get("id") == name), None)
    if entry is not None:
        text = agent_text(entry)
    elif any(item.get("id") == name for item in phrases()):
        text = phrase(name)
    else:
        print(msg("pipeline_no_prompt_id", name), file=sys.stderr)
        return 1
    print(fill(text, {**given(), **(agent_values(entry) if entry is not None else {})}, os.environ).rstrip("\n"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Сверка манифеста конвейера с деревом и конвейером круга.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--emit-pass", metavar="ID", help="JSON сессии одного прохода")
    group.add_argument("--emit-fallback", action="store_true", help="величины запасного круга")
    group.add_argument("--emit-prompt", metavar="ID", help="промпт агента или фраза входа модели")
    group.add_argument("--emit-agent", metavar="ID", help="строки выходов шага CI сессии агента вне ревью")
    group.add_argument("--emit-provider", choices=("issue-agent", "alert-triage"),
                       help="провайдер исполнителя процесса: AGENT_PROVIDER или расширение модуля ci-agent")
    parser.add_argument("--change", default="", metavar="N", help="номер запроса для промпта прохода; только с --emit-pass")
    args = parser.parse_args()

    emitting = args.emit_pass or args.emit_fallback or args.emit_prompt or args.emit_agent or args.emit_provider
    if not os.path.exists(MANIFEST):
        print(msg("pipeline_no_manifest"),
              file=sys.stderr if emitting else sys.stdout)
        return 1

    problems: list[str] = []
    check_contract(problems)
    if problems:
        return diverged(problems)

    try:
        if args.emit_pass:
            values = given()
            print(json.dumps(session(args.emit_pass, forge.change_link(args.change) if args.change else "",
                                     values.get("ReviewInput", ""), values.get("ReviewDecided", "")),
                             ensure_ascii=False))
            return 0
        if args.emit_fallback:
            return emit_fallback()
        if args.emit_prompt:
            return emit_prompt(args.emit_prompt)
        if args.emit_agent:
            return emit_agent(args.emit_agent)
        if args.emit_provider:
            return emit_provider(args.emit_provider)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    check_gitlab_root(problems)
    check_installed_manifest(problems)
    if not passes() or not process_entries():
        print(msg("pipeline_manifest_empty"), file=sys.stderr)
        return 1

    check_paths(problems)
    check_graph(problems)
    check_handoff(problems)
    check_findings(problems)
    check_authorship(problems)
    check_review(problems)
    check_workflow(problems)
    check_fallback(problems)
    check_kinds(problems)
    check_prompts(problems)
    check_docs_free_of_markers(problems)
    check_fact_copies(problems)
    check_tool_copies(problems)

    if problems:
        return diverged(problems)

    steps = sum(len(p.get("steps", [])) for p in process_entries())
    print(msg("pipeline_clean", steps, len(passes())))
    return 0
