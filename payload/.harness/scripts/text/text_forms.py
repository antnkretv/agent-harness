from __future__ import annotations

import hashlib
import os
import re

from ci_common import ROOT, msg
import manifest

FILL = "<!-- fill -->"
RULE = re.compile(r"^<!-- (optional|pick: (?:some|any|all)) -->$")
WHEN = re.compile(r" *<!-- when: ([\w ]+) -->")
STAMP = re.compile(r"\A<!-- form ([0-9a-f]{64}) -->\n?")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
BOX = "- [ ] "
CHECKED = "- [x] "
UNMET = msg("template_form_unmet")
FOLDER = ".harness/templates/"
FORMS = "forms/"
TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "templates")


def kit_form(spec: str) -> bool:
    return spec.startswith(FORMS)


def located(spec: str) -> tuple[str, str, str]:
    if kit_form(spec):
        path, full = FOLDER + spec + ".md", os.path.join(TEMPLATES, *spec.split("/")) + ".md"
    else:
        module, _, field = spec.partition(".")
        path = manifest.extension_value(module, field)
        full = os.path.join(ROOT, path)
    try:
        with open(full, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise ValueError(msg("template_form_missing", f"{spec}: {error}")) from None
    sha = hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8")
    if FILL not in text:
        raise ValueError(msg("template_form_unmarked", path))
    return path, text, sha


def skeleton(spec: str) -> str:
    _, text, sha = located(spec)
    return f"<!-- form {sha} -->\n{text}"


def fenced(lines: list[str]) -> list[tuple[str, bool]]:
    found, fence = [], ""
    for line in lines:
        mark = FENCE.match(line)
        inside = bool(fence)
        if mark and (not fence or mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence)):
            fence = "" if fence else mark.group(1)
            inside = True
        found.append((line, inside))
    return found


def sections(text: str) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    title, lines = "-", []
    for line, inside in fenced(text.splitlines()):
        if not inside and line.startswith("## "):
            found.append((title, lines))
            title, lines = line.rstrip(), []
        else:
            lines.append(line if inside else line.rstrip())
    found.append((title, lines))
    return found


def block(lines: list[str]) -> str:
    kept, blank = [], 0
    for line, inside in fenced(lines):
        if not inside and RULE.match(line.strip()):
            continue
        blank = blank + 1 if not inside and not line.strip() else 0
        if blank < 2:
            kept.append(line)
    return "\n".join(kept).strip("\n")


def pattern(text: str) -> str:
    return "".join(r"([\s\S]+?)" if part == FILL else re.escape(part) for part in re.split(f"({re.escape(FILL)})", text))


def items(text: str) -> list[str]:
    found: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith(" ") and found:
            found[-1].append(line)
        elif line.strip():
            found.append([line])
    return ["\n".join(item) for item in found]


def item_pattern(item: str, tail: str = "") -> re.Pattern:
    if item.startswith(BOX):
        return re.compile(r"- \[[ x]\] " + pattern(item[len(BOX):]) + tail)
    return re.compile(pattern(item) + tail)


def filled(groups: tuple) -> bool:
    return all(group.strip() and FILL not in group for group in groups)


def applies(item: str, chosen: set[str]) -> bool:
    found = WHEN.search(item)
    return not found or not chosen.isdisjoint(found.group(1).split())


def picked(title: str, rule: str, form: str, given: str, chosen: set[str]) -> list[str]:
    tail = rf"(?:{re.escape(UNMET)} +(\S[\s\S]*))?" if rule == "pick: all" else ""
    listed = [(WHEN.sub("", item), applies(item, chosen)) for item in items(form)]
    wanted = listed if tail else [entry for entry in listed if entry[1]]
    last, marks, answered, kept, problems = -1, 0, set(), set(), []
    choices = [item_pattern(item, tail) for item, _ in wanted]
    for item in items(given):
        index, found = next(((index, found) for index in range(last + 1, len(choices))
                             if (found := choices[index].fullmatch(item))), (None, None))
        if found is None:
            problems.append(msg("template_form_item", title, item.splitlines()[0]))
            continue
        slots, checked = found.groups(), item.startswith(CHECKED)
        if tail:
            slots, reason = slots[:-1], slots[-1]
            if checked != (reason is not None):
                answered.add(index)
        if not filled(slots):
            problems.append(msg("template_form_fill", title))
        marks += checked
        last = index
        kept.add(index)
        if checked and not tail:
            chosen.add(item[len(CHECKED):].split()[0])
    if rule == "pick: some" and not marks:
        problems.append(msg("template_form_pick_some", title))
    if tail:
        problems += [msg("template_form_check", title, UNMET, want.splitlines()[0])
                     for index, (want, needed) in enumerate(wanted)
                     if (needed or index in kept) and index not in answered]
    return problems


def compared(title: str, rules: set[str], form: list[str], given: list[str], chosen: set[str]) -> list[str]:
    want, got = block(form), block(given)
    pick = next((rule for rule in rules if rule.startswith("pick")), "")
    if pick:
        return picked(title, pick, want, got, chosen)
    found = re.fullmatch(pattern(want), got)
    if not found:
        return [msg("template_form_text", title)]
    return [] if filled(found.groups()) else [msg("template_form_fill", title)]


def marked(lines: list[str]) -> set[str]:
    return {found.group(1) for line in lines if (found := RULE.match(line.strip()))}


def unknown_keys(text: str) -> set[str]:
    keys, tagged = set(), set()
    for _, lines in sections(text):
        found = block(lines)
        tagged |= {key for tag in WHEN.findall(found) for key in tag.split()}
        if marked(lines) & {"pick: some", "pick: any"}:
            keys |= {item[len(BOX):].split()[0] for item in items(found) if item.startswith(BOX)}
    return tagged - keys


def conform(name: str, spec: str, value: str) -> str:
    path, text, sha = located(spec)
    stamp = STAMP.match(value)
    if not stamp or stamp.group(1) != sha:
        raise ValueError(msg("template_form_stale", path, name))
    if unknown := unknown_keys(text):
        raise ValueError(msg("template_form_when_unknown", path, ", ".join(sorted(unknown))))
    body, problems, position, chosen = WHEN.sub("", value[stamp.end():]), [], 0, set()
    given = sections(body)
    for title, lines in sections(text):
        rules = marked(lines)
        if position < len(given) and given[position][0] == title:
            problems += compared(title, rules, lines, given[position][1], chosen)
            position += 1
        elif "optional" not in rules:
            problems.append(msg("template_form_section_missing", title))
    problems += [msg("template_form_section_extra", title) for title, _ in given[position:]]
    if problems:
        raise ValueError(msg("template_form_mismatch", path, "; ".join(dict.fromkeys(problems))))
    return block(body.splitlines()).strip()
