from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import NamedTuple

from .catalog import (
    WINDOWS_PATH,
    check_problem,
    full_check,
    listed_names,
    setting_problems,
)
from .detect import Tree
from .resources import EVIDENCE_MAX, NONE, OFF, ON, Refusal, SIDES, msg


class Pending(Exception):
    def __init__(self, question: "Question"):
        super().__init__(question.id)
        self.question = question


MISSING = object()


@dataclass
class Question:
    id: str
    kind: str
    text: str
    options: list = field(default_factory=list)
    default: object = None
    why: str = ""
    rule: str = ""
    parse: object = None
    decision: bool = False
    detected: bool = False
    batch: str = ""
    static: bool = False
    hint: str = ""
    items: list | None = None
    proposed: object = None
    catalog: bool = False
    catalog_value: object = None


def value_text(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ", ".join(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def option(value: str, label: str, description: str = "") -> dict:
    return {"value": value, "label": label, "description": description}


def evidence_text(lines: list[str]) -> str:
    return "; ".join(lines[:EVIDENCE_MAX]) + ("; ..." if len(lines) > EVIDENCE_MAX else "")


def listed(raw) -> list:
    if isinstance(raw, list):
        return raw
    text = str(raw).strip()
    if text.startswith("["):
        value = json_value(text)
        if not isinstance(value, list):
            raise ValueError(msg("answer_list", text))
        return value
    return [part.strip() for part in text.split(",") if part.strip()]


def object_parser(raw) -> dict:
    value = json_value(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError(msg("answer_mapping"))
    return value


def json_value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(msg("answer_json", error)) from None


def one_of(values: list[str]):
    def parse(raw):
        if isinstance(raw, list) and len(raw) == 1:
            raw = raw[0]
        if raw not in values:
            raise ValueError(msg("answer_not_option", raw, ", ".join(values)))
        return raw
    return parse


def matching(pattern: str):
    def parse(raw):
        text = str(raw).strip()
        if not re.fullmatch(pattern, text):
            raise ValueError(msg("answer_pattern", text, pattern))
        return text
    return parse


def present(raw) -> str:
    text = str(raw).strip()
    if not text:
        raise ValueError(msg("answer_empty"))
    return text


def optional_text(raw) -> str:
    return str(raw).strip()


PACKED = "packed"
STACK, DEFAULTS = "stack", "defaults"
SUMMARIES = (STACK, DEFAULTS)
KEEP, CHANGE = "keep", "change"
EMPTY = ([], {})


class Session:
    def __init__(self, catalog: dict, tree: Tree, answers: dict, incoming=(), defaults: bool = False,
                 reopened: dict | None = None, decisions: bool = False, batching: bool = False, alone=(),
                 strict: bool = False):
        self.catalog, self.tree, self.answers = catalog, tree, answers
        self.batching, self.alone, self.strict = batching, set(alone), strict
        self.incoming, self.defaults, self.decisions = set(incoming), defaults, decisions
        self.reopened = reopened if reopened is not None else {}
        self.seen: set[str] = set()
        self.decided: dict = {}
        self.answered: list[dict] = []
        self.traces: dict[tuple[str, str], list[str]] = {}
        self.batch: dict[str, list[tuple[Question, object]]] = {group: [] for group in SUMMARIES}
        self.single: Question | None = None

    def evidence(self, name: str, extension: str) -> list[str]:
        if (name, extension) not in self.traces:
            spec = self.catalog["modules"][name]
            detect = (spec["extensions"][extension] if extension else spec).get("detect", {})
            self.traces[(name, extension)] = self.tree.evidence(detect)
        return self.traces[(name, extension)]

    def stored(self, key: str, parse):
        entry = self.answers.get(key)
        if entry is None:
            return MISSING
        try:
            value = parse(entry["raw"] if "raw" in entry else entry["value"])
        except ValueError as error:
            if key in self.incoming or self.strict and entry["by"] == "owner":
                raise Refusal(msg("configure_bad_answer", key, error)) from None
            del self.answers[key]
            return MISSING
        self.answers[key] = {"value": value, "by": entry["by"]}
        return value

    def unowned(self, key: str, catalog: bool = False) -> bool:
        by = self.answers.get(key, {}).get("by")
        return by not in (None, "owner") and not (by == "default" and catalog) and not self.defaults

    def ask(self, question: Question):
        self.seen.add(question.id)
        reopened = question.id in self.reopened
        if reopened:
            question.default, question.detected = self.reopened[question.id], False
        empty = bool(question.batch) and parsed_default(question) in EMPTY \
            and self.stored(question.id, question.parse) in (MISSING, *EMPTY)
        by = self.answers.get(question.id, {}).get("by")
        current = parsed_default(question)
        if question.decision and not reopened and by == PACKED:
            if self.stored(question.id, question.parse) != question.catalog_value:
                by = "owner"
            elif question.catalog:
                by = "default"
            else:
                by = None
                del self.answers[question.id]
            if by is not None:
                self.answers[question.id]["by"] = by
        elif question.decision and not reopened and question.static and by not in (None, "default", "owner") \
                and self.stored(question.id, question.parse) == current:
            by = "default"
        if question.decision and question.catalog and not reopened and by == "default" and current is not None:
            self.answers[question.id] = {"value": current, "by": "default"}
        fixed = (question.static and by in (None, "default", "owner") or empty) and not reopened
        settled = by == "default" and question.catalog_value is not None and not reopened \
            and self.stored(question.id, question.parse) == question.catalog_value
        if question.decision and self.unowned(question.id, question.catalog or settled) and not fixed:
            prior = self.stored(question.id, question.parse)
            question.proposed = current
            if prior is not MISSING:
                question.default = prior
            question.detected = False
            return self.wait(question, reopened)
        value = self.stored(question.id, question.parse)
        if value is MISSING:
            default = parsed_default(question)
            found = question.detected and not question.decision
            if default is None or not (self.defaults or found or fixed):
                return self.wait(question, reopened)
            value = default
            by = "detected" if question.detected else "default"
            self.answers[question.id] = {"value": value, "by": by}
            if found:
                self.answered.append({"id": question.id, "value": value, "why": question.why})
        self.reopened.pop(question.id, None)
        if question.decision:
            self.decided[question.id] = value
        return value

    def wait(self, question: Question, reopened: bool):
        if not (self.batching and question.decision):
            raise Pending(question)
        default = parsed_default(question)
        if question.batch and question.id not in self.alone and default is not None:
            self.batch[question.batch].append((question, default))
            return default
        if self.single is None or (reopened or default is None) and self.single.id not in self.reopened:
            self.single = question
        if default is None:
            raise Pending(question)
        return default

    def value(self, key: str, parse, default, decision: bool = False, asked: bool = False):
        self.seen.add(key)
        if key in self.reopened and key not in self.answers and not asked:
            self.answers[key] = {"value": self.reopened.pop(key), "by": "owner"}
        value = self.stored(key, parse)
        if value is MISSING:
            return default
        if decision and value != default:
            self.decided[key] = value
        return value


def parsed_default(question: Question):
    try:
        return None if question.default is None else question.parse(question.default)
    except ValueError:
        return None


def docs_list(raw) -> list[str]:
    value = listed(raw)
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(msg("answer_docs_list"))
    converted = [item for item in value if WINDOWS_PATH.match(item.strip())]
    if converted:
        raise ValueError(msg("answer_windows_path", converted[0]))
    return [item.strip() for item in value]


def module_parser(values: list[str], low: int, high: int | None, multi: bool):
    def parse(raw):
        picked = listed(raw) if multi else [one_of(values)(raw)]
        unknown = [value for value in picked if value not in values]
        if unknown or not picked:
            raise ValueError(msg("answer_not_option", ", ".join(map(str, unknown)) or "-", ", ".join(values)))
        special = [value for value in picked if value in (OFF, NONE, ON)]
        if special and len(picked) > 1:
            raise ValueError(msg("answer_alone", special[0]))
        if not special and (len(picked) < low or high is not None and len(picked) > high):
            raise ValueError(msg("answer_count", len(picked), low, "-" if high is None else high))
        ordered = [value for value in values if value in picked]
        return ordered if multi else ordered[0]
    return parse


class ChoiceGroup(NamedTuple):
    id: str
    slot: str | None
    bounds: dict
    members: list[str]
    multi: bool


def choice_groups(name: str, spec: dict):
    groups = list(spec["slots"].items()) if "slots" in spec else [(None, spec.get("select", {"max": 0}))]
    for index, (slot, bounds) in enumerate(groups):
        members = [extension for extension, meta in spec.get("extensions", {}).items()
                   if slot is None or meta.get("slot") == slot]
        high = bounds.get("max")
        yield ChoiceGroup(f"modules.{name}" if index == 0 else f"modules.{name}:{slot}", slot, bounds, members,
                          high is None or high > 1)


def choice_question(name: str, spec: dict, group: ChoiceGroup, first: bool, values: list[str], extensions: dict,
                    evidence: dict, found: list[str], own: list[str]) -> Question:
    multi = group.multi
    from_catalog = False
    catalog_default = spec.get("default") if first else None
    listed = catalog_default if isinstance(catalog_default, list) else [catalog_default]
    catalog_value = (listed if multi else listed[0]) if catalog_default is not None else None
    if found:
        default = found if multi else found[0] if len(found) == 1 else None
    elif own and ON in values:
        default = [ON] if multi else ON
    elif catalog_default is not None and all(item in values for item in listed) and (multi or len(listed) == 1):
        default = listed if multi else listed[0]
        from_catalog = True
    else:
        fallback = OFF if OFF in values else NONE if NONE in values and name not in SIDES else None
        default = None if fallback is None else [fallback] if multi else fallback
    off = spec.get("off", {})
    options = [option(extension, extensions[extension]["title"], extension_note(extensions[extension], evidence[extension]))
               for extension in values if extension in extensions]
    alone = msg("option_alone") if multi else ""
    options += [option(NONE, msg("option_none"), rules(msg("option_none_side") if name in SIDES else msg("option_none_module"),
                                                        alone))] * (NONE in values)
    options += [option(ON, msg("option_on"), rules(spec["title"], alone))] * (ON in values)
    options += [option(OFF, msg("option_off"), rules(msg("option_off_alone") if multi else "",
                                                      msg("option_off_note", off.get("reason", "-"), off.get("replacement", "-"))))] \
        * (OFF in values)
    lines = own + [line for extension in found for line in evidence[extension]]
    detected = bool(found or own and ON in values)
    question = Question(group.id, "multi" if multi else "single",
                        spec["question"] if first else group.bounds.get("question") or msg("q_slot", spec["title"], group.slot),
                        options, default, evidence_text(lines),
                        parse=module_parser(values, group.bounds.get("min", 0), group.bounds.get("max"), multi),
                        decision=True, detected=detected,
                        batch=DEFAULTS if catalog_default is not None else STACK if detected else "",
                        hint=lines[0] if lines else "", catalog=from_catalog, catalog_value=catalog_value)
    return question


def extension_note(meta: dict, evidence: list[str]) -> str:
    return rules(meta.get("about", ""), msg("option_found", evidence[0]) if evidence else "",
                 msg("option_secrets", ", ".join(meta["secrets"])) if meta.get("secrets") else "")


def setting_parser(where: str, key: str, meta: dict):
    def parse(raw):
        value = raw
        if isinstance(raw, str):
            text = raw.strip()
            referenced = set(meta.get("sources", ["literal"])) - {"literal"}
            if text.startswith("{") and (referenced or meta["shape"] == "object"):
                value = json_value(text)
            elif meta["shape"] in ("texts", "choices"):
                value = listed(text)
            elif meta["shape"] in ("records", "object"):
                value = json_value(text or "null")
            elif meta["shape"] == "flag":
                value = {"true": True, "false": False}.get(text.lower(), text)
            else:
                value = text
        if value == "" and meta["shape"] == "text" and not meta.get("required"):
            return value
        problems = setting_problems(where, {key: meta}, {key: value})
        if problems:
            raise ValueError(problems[0])
        return value
    return parse


def proposal(s: Session, where: str, key: str, meta: dict) -> tuple[object, str]:
    values, evidence = s.tree.candidates(meta.get("detect", {})) if meta.get("detect") else ([], [])
    if len(values) == 1:
        try:
            return setting_parser(where, key, meta)(values[0]), evidence[0]
        except ValueError:
            pass
    return meta["default"], evidence_text(evidence) if evidence else ""


def exceptions_parser(ids: list[str]):
    def parse(raw) -> list[str]:
        picked = listed(raw)
        if picked == [KEEP]:
            return []
        if not picked or CHANGE in picked:
            raise ValueError(msg("answer_exceptions"))
        unknown = [item for item in picked if item not in ids]
        if unknown:
            raise ValueError(msg("answer_not_option", ", ".join(map(str, unknown)), ", ".join(ids)))
        return picked
    return parse


def summary_question(group: str, items: list[tuple[Question, object]]) -> Question:
    stack = group == STACK
    keep = msg("option_keep_note") if stack else msg("option_keep_note") + "; " + msg("option_keep_note_update")
    change = msg("option_change_note") if stack else msg("option_change_note") + ", " + msg("option_change_note_setting")
    return Question(group, "single", msg("q_stack") if stack else msg("q_defaults"),
                    [option(KEEP, msg("option_keep"), keep), option(CHANGE, msg("option_change"), change)],
                    KEEP, msg("why_stack") if stack else msg("why_defaults"),
                    parse=exceptions_parser([question.id for question, _ in items]), decision=True,
                    items=[summary_item(question, value) for question, value in items])


def summary_item(question: Question, value) -> dict:
    found = {"id": question.id, "value": value, "why": question.hint}
    if question.proposed is not None and question.proposed != value:
        found["default"] = question.proposed
    return found


def rules(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def listed_question(entries: dict, key: str, where: str, meta: dict, parse) -> Question:
    names = listed_names({"modules": entries}, meta["in"])

    def member(raw):
        value = parse(raw)
        if value not in names:
            raise ValueError(msg("answer_not_listed", value, meta["in"], ", ".join(names) or "-"))
        return value

    text = msg("q_setting", meta.get("title", key), where)
    rule = rules(meta.get("rule", ""), msg("rule_listed", meta["in"], ", ".join(names) or "-"))
    if names:
        return Question(key, "single", text, [option(name, name) for name in names], names[0] if len(names) == 1 else None,
                        rule=rule, parse=member, decision=True)
    return Question(key, "text", text, rule=rule, parse=member, decision=True)


def decision_question(key: str, where: str, meta: dict, parse, default=None, why: str = "") -> Question:
    values = meta.get("values", dict.fromkeys(("true", "false"), "") if meta["shape"] == "flag" else {})
    text, rule = msg("q_setting", meta.get("title", key), where), meta.get("rule", "")
    options = [option(value, value, about) for value, about in values.items()]
    if meta["shape"] in ("choice", "flag"):
        question = Question(key, "single", text, options, str(default).lower() if isinstance(default, bool) else default,
                            why, rule, parse=parse)
    elif meta["shape"] == "choices":
        question = Question(key, "multi", text, options, default, why, rule, parse=parse)
    else:
        question = Question(key, "text", text, [], default, why, rule, parse=parse)
    question.decision, question.static, question.hint = True, bool(meta.get("static")), why
    question.catalog = "default" in meta and default == meta["default"]
    if "default" in meta:
        try:
            question.catalog_value = parse(meta["default"])
        except ValueError:
            question.catalog_value = meta["default"]
    question.batch = DEFAULTS if "default" in meta else ""
    return question


def fact_question(s: Session, key: str, where: str, meta: dict, parse) -> Question:
    values, evidence = s.tree.candidates(meta.get("detect", {}))
    text, rule = msg("q_setting", meta.get("title", key), where), meta.get("rule", "")
    why = evidence_text(evidence) if evidence else msg("why_nothing") if meta.get("detect") else ""
    optional = not meta.get("required")
    if meta["shape"] == "texts" and values:
        return Question(key, "multi", text, [option(value, value, line) for value, line in zip(values, evidence)],
                        values, why, rule, parse, detected=True)
    if len(values) == 1:
        return Question(key, "text", text, [], values[0], why, rule, parse, detected=True)
    fallback = meta["default"] if "default" in meta else "" if optional and meta["shape"] == "text" else None
    default = None if values else fallback
    if values:
        options = [option(value, value, line) for value, line in zip(values, evidence)]
        options += [option("", msg("option_absent"))] * optional
        return Question(key, "single", text, options, default, why, rule, parse)
    return Question(key, "text", text, [], default, why, rule, parse)


def app_check(stack: str, scripts: dict | None = None):
    def parse(raw) -> str:
        text = present(raw)
        if not full_check(text):
            raise ValueError(msg("answer_check"))
        problem = check_problem(stack, text, scripts)
        if problem:
            raise ValueError(problem)
        return text
    return parse


def choice_answers(name: str, spec: dict, chosen: list[str]) -> dict:
    found = {}
    for group in choice_groups(name, spec):
        value = [extension for extension in chosen if group.slot is None or extension in group.members] \
            or [NONE if group.members else ON]
        found[group.id] = value if group.multi else value[0]
    return found
