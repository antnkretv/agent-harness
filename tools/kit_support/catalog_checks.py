from __future__ import annotations

import json
import re

from installer.catalog import SETTING_SHAPES, enabled, reference_parts, setting_problems
from installer.detect import DETECT_KEYS
from installer.resources import SIDES, SOURCES

from .context import KIT, msg

CHOICE_SHAPES = ("choice", "choices")
NAME_WORDS, SETTING_NAME_WORDS, QUESTION_WORDS = 5, 8, 12
REASON_WORDS, REPLACEMENT_WORDS, ABOUT_WORDS, RULE_WORDS, VALUE_WORDS = 14, 30, 18, 45, 16
STANDARD_ROWS = {
    "`modules.<м>.title`": NAME_WORDS, "`modules.<м>.question`": QUESTION_WORDS, "`off.reason`": REASON_WORDS,
    "`off.replacement`": REPLACEMENT_WORDS, "`extensions.<р>.title`": NAME_WORDS, "`extensions.<р>.about`": ABOUT_WORDS,
    "`settings.<н>.title`": SETTING_NAME_WORDS, "`settings.<н>.rule`": RULE_WORDS, "`settings.<н>.values`": VALUE_WORDS,
}
SETTING_NAME_MARKS = (":", ";", "(", ")", " - ")
NAME_MARKS = (*SETTING_NAME_MARKS, ",")
NAME_START = re.compile(r"[a-z.\d]")
PHRASE_CAPITAL = re.compile(r"[А-ЯЁ](?![А-ЯЁ])")
OFF_ADVICE = re.compile(r"\bconfigure\b|\bah (?:init|change)\b|ah\.py|<комплект>|install\.py|--reopen|включите|подключите", re.I)
TYPOGRAPHY = re.compile(r"[—–«»…→]|кэш", re.I)
LOOSE_EXAMPLE = re.compile(r"(?<!; )например")


def catalog_paths(modules: dict) -> frozenset[str]:
    """Пути, которые может назвать условие файла: включенные пути проекта со всеми модулями каталога."""
    return enabled(every_module(modules))


def setting_holders(modules: dict):
    """Носители настроек каталога: модуль и каждое его расширение - путь и схемы настроек."""
    for name, spec in modules["modules"].items():
        settings = spec.get("settings", {})
        yield name, {**settings, **modules.get("agents", {})} if name == "agents" else settings
        for extension, meta in spec.get("extensions", {}).items():
            yield f"{name}/{extension}", meta.get("settings", {})


def catalog_problems(modules: dict) -> list[str]:
    """Каталог сам себе не противоречит: порядок вопросов называет каждый модуль, зависимости и слоты существуют."""
    problems = []
    names = modules["modules"]
    if sorted(modules["order"]) != sorted(names) or len(set(modules["order"])) != len(modules["order"]):
        problems.append(msg("kit_catalog_order"))
    paths = catalog_paths(modules)
    for name, spec in names.items():
        extensions = spec.get("extensions", {})
        problems += text_breaks(f"{name}.title", spec.get("title"), NAME_WORDS, NAME_MARKS)
        problems += question_breaks(f"{name}.question", spec.get("question"))
        for required in spec.get("requires", []) + [r for ext in extensions.values() for r in ext.get("requires", [])]:
            if required not in paths:
                problems.append(msg("kit_catalog_requires", name, required))
        for side in [spec["side"]] if spec.get("side") else []:
            if side not in SIDES:
                problems.append(msg("kit_catalog_side", name, side))
        slots = spec.get("slots", {})
        for slot, bounds in slots.items():
            if "question" in bounds:
                problems += question_breaks(f"{name}.slots.{slot}.question", bounds["question"])
        for extension, meta in extensions.items():
            if bool(slots) != ("slot" in meta) or meta.get("slot") and meta["slot"] not in slots:
                problems.append(msg("kit_catalog_slot", f"{name}/{extension}"))
            problems += text_breaks(f"{name}/{extension}.title", meta.get("title"), NAME_WORDS, NAME_MARKS)
            if "about" in meta:
                problems += text_breaks(f"{name}/{extension}.about", meta["about"], ABOUT_WORDS)
        for where, meta in [(name, spec), *((f"{name}/{extension}", meta) for extension, meta in extensions.items())]:
            if set(meta.get("detect", {})) - DETECT_KEYS:
                problems.append(msg("kit_catalog_detect", where, ", ".join(sorted(DETECT_KEYS))))
        if not spec.get("required"):
            off = spec.get("off", {})
            problems += text_breaks(f"{name}.off.reason", off.get("reason"), REASON_WORDS)
            problems += text_breaks(f"{name}.off.replacement", off.get("replacement"), REPLACEMENT_WORDS)
            if OFF_ADVICE.search(str(off.get("replacement", ""))):
                problems.append(broken(f"{name}.off.replacement", off["replacement"], msg("kit_text_advice")))
        default = spec.get("default")
        if default is not None and not set(default if isinstance(default, list) else [default]) <= set(extensions):
            problems.append(msg("kit_catalog_module_default", name))
    for where, settings in setting_holders(modules):
        for key, setting in settings.items():
            place = f"{where}.{key}"
            problems += setting_breaks(place, setting)
            if setting.get("shape") not in SETTING_SHAPES or setting.get("kind") not in ("decision", "fact"):
                problems.append(msg("kit_catalog_setting", place))
            if (setting.get("shape") in CHOICE_SHAPES) != isinstance(setting.get("values"), dict) or setting.get("values") == {}:
                problems.append(msg("kit_catalog_values", place))
            if not set(setting.get("sources", ["literal"])) <= set(SOURCES):
                problems.append(msg("kit_catalog_setting", place))
            if set(setting.get("detect", {})) - DETECT_KEYS:
                problems.append(msg("kit_catalog_detect", place, ", ".join(sorted(DETECT_KEYS))))
            if setting.get("static") and (setting.get("kind") != "decision" or "default" not in setting):
                problems.append(msg("kit_catalog_static", place))
            if "default" in setting and setting_problems(where, {key: setting}, {key: setting["default"]}):
                problems.append(msg("kit_catalog_default", place))
            if "in" in setting and not listed_reference(modules, where, setting["in"]):
                problems.append(msg("kit_catalog_in", place, setting["in"]))
            order = modules["order"]
            for required in setting.get("requires", []):
                owner, module = required.partition("/")[0], where.partition("/")[0]
                if required not in paths or owner not in order or order.index(owner) >= order.index(module):
                    problems.append(msg("kit_catalog_setting_requires", place, required))
            chosen = setting.get("secrets")
            if chosen is not None and (setting.get("shape") != "choice" or not isinstance(chosen, dict)
                                       or not set(chosen) <= set(setting.get("values", {}))):
                problems.append(msg("kit_catalog_setting", place))
    return problems + standard_problems()


def text_breaks(place: str, text, limit: int, marks: tuple[str, ...] | None = None) -> list[str]:
    """Нарушенные границы стандарта текстов: имя - текст со знаками marks, которых в нем нет, фраза - без marks."""
    if not isinstance(text, str) or not text.strip():
        return [broken(place, text, msg("kit_text_empty"))]
    bounds = [msg("kit_text_words", len(text.split()), limit)] if len(text.split()) > limit else []
    bounds += [msg("kit_text_dot")] if text.rstrip().endswith(".") else []
    if marks is None:
        bounds += [msg("kit_text_lower")] if PHRASE_CAPITAL.match(text) else []
    else:
        used = [f'"{mark}"' for mark in marks if mark in text]
        bounds += [msg("kit_text_marks", " ".join(used))] if used else []
        bounds += [] if text[0].isupper() or NAME_START.match(text) else [msg("kit_text_capital")]
    return [broken(place, text, bound) for bound in bounds + typography(text)]


def question_breaks(place: str, text) -> list[str]:
    if not isinstance(text, str) or not text.endswith("?") or text.count("?") != 1 or len(text.split()) > QUESTION_WORDS \
            or not text[0].isupper():
        return [broken(place, text, msg("kit_text_question", QUESTION_WORDS))]
    return [broken(place, text, bound) for bound in typography(text)]


def setting_breaks(place: str, setting: dict) -> list[str]:
    found = text_breaks(f"{place}.title", setting.get("title"), SETTING_NAME_WORDS, SETTING_NAME_MARKS)
    choice = setting.get("shape") in CHOICE_SHAPES
    if not choice or "rule" in setting:
        found += text_breaks(f"{place}.rule", setting.get("rule"), RULE_WORDS)
        if LOOSE_EXAMPLE.search(str(setting.get("rule", ""))):
            found.append(broken(f"{place}.rule", setting["rule"], msg("kit_text_example")))
    values = setting.get("values")
    for value, text in values.items() if choice and isinstance(values, dict) else ():
        found += text_breaks(f"{place}.values.{value}", text, VALUE_WORDS)
    return found


def typography(text: str) -> list[str]:
    used = sorted(set(TYPOGRAPHY.findall(text)))
    return [msg("kit_text_typography", " ".join(used))] if used else []


def broken(place: str, text, bound: str) -> str:
    return msg("kit_text", place, bound, json.dumps(text, ensure_ascii=False))


def standard_problems() -> list[str]:
    """Таблица стандарта текстов PARAMETERS.md называет те же числа слов, что границы проверки: по ней пишут каталог."""
    rows = {line.split("|")[1].split(",")[0].strip(): line
            for line in (KIT / "PARAMETERS.md").read_text(encoding="utf-8").splitlines() if line.startswith("| `")}
    return [msg("kit_text_standard", field, msg("kit_text_limit", limit)) for field, limit in STANDARD_ROWS.items()
            if msg("kit_text_limit", limit) not in rows.get(field, "")]


def listed_reference(modules: dict, where: str, reference: str) -> bool:
    """Ссылка in называет настройку-список записей каталога и его поле, а модуль списка спрашивается раньше
    модуля ссылки: настройка предлагает имена уже собранного списка."""
    path, key, field = reference_parts(reference)
    spec = dict(setting_holders(modules)).get(path, {}).get(key, {})
    fields = spec.get("fields", {})
    order = modules["order"]
    listed, owner = path.split("/")[0], where.split("/")[0]
    return (spec.get("shape") == "records" and field in fields.get("required", []) + fields.get("optional", [])
            and listed in order and owner in order and order.index(listed) < order.index(owner))


def every_module(modules: dict) -> dict:
    """Проект со всеми модулями и расширениями каталога: под него рендерится AGENT.md полного комплекта."""
    return {"modules": {name: {"extensions": {extension: {} for extension in spec.get("extensions", {})}}
                        for name, spec in modules["modules"].items()}}


