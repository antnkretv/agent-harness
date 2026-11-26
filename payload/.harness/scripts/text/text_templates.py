#!/usr/bin/env python3
"""Публикуемые тексты из файлов-шаблонов: рендер, справка и гейт реестра.

Шаблон - файл templates/<класс>/<имя>.md, его идентификатор - путь без расширения.
Переменная - ${Name} с записью в templates/variables.json. Устройство и порядок
добавления шаблона - .harness/docs/templates.md.

    ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py list
    ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py show <шаблон>
    ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render <шаблон> --set Имя=текст --set-file Имя=файл [--change N] [--commit SHA] [--issue задача] [--refs задача] [--session id] [--out файл]
    ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py form <ИМЯ> [--out файл]
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import string
import subprocess
import sys
from collections.abc import Mapping

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import BASE_BRANCH, ROOT, git, msg  # noqa: E402
import text_data  # noqa: E402
import text_forms  # noqa: E402
from text_primitives import Violation, line_of  # noqa: E402

FOLDER = text_forms.FOLDER
REGISTRY = FOLDER + "variables.json"
PROMPTS = ".harness/prompts/"
METHODOLOGY = ".harness/methodology.json"
OLD = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
PRINTF = re.compile(r"%s")
INLINE = re.compile(r"`[^`\n]*`")
INLINE_SPLIT = re.compile(f"({INLINE.pattern})")
PROMPT_CALL = re.compile(r"""\btext_templates\.prompt\(\s*["']([^"']+)["']""")
CALLED_PROMPTS = PROMPTS + "session/"
TEMPLATES = text_forms.TEMPLATES
NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
FIELDS = {"about", "form", "part", "prompt", "required", "values"}
PARTS = "parts/"
FORMS = text_forms.FORMS
CALL = re.compile(r"""\btext_templates\.render\(\s*["']([^"']+)["']""")
COMMAND = re.compile(r"\btext_templates\.py render ([a-z0-9-]+/[a-z0-9-]+)")
RUNTIME = {"ChangeLink", "SHA", "ChangeIssues", "ChangeFixes", "ChangeStats", "SessionLink", "SessionModel"}
PUBLISHED = ("comment/", "issue/")
DESCRIPTION = "change/description"
COMMIT = "commit/message"
FORBIDDEN = ("%s", "{{", "<!--", "](${")

ROOTED = re.compile(r"\]\(/([^)\s#]+)")


class Text(string.Template):
    flags = 0
    pattern = (r"\$\{(?P<braced>" + NAME.pattern + r")\}"
               r"|(?P<escaped>(?!))|(?P<named>(?!))|(?P<invalid>(?!))")


class Prompt(string.Template):
    flags = 0
    pattern = (r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}"
               r"|(?P<escaped>(?!))|(?P<named>(?!))|(?P<invalid>(?!))")


def fill(text: str, values: Mapping[str, object], env: Mapping[str, str] | None = None) -> str:
    def value(found: re.Match) -> str:
        name = found.group("braced")
        if name in values:
            return str(values[name])
        if env is not None and name not in variables() and name in env:
            return env[name]
        raise ValueError(msg("prompt_value_missing", name))

    return Prompt.pattern.sub(value, text)


def prompt(name: str, values: Mapping[str, object] | None = None) -> str:
    path = os.path.join(os.path.dirname(TEMPLATES), "prompts", name + ".md")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().rstrip("\n")
    except OSError:
        raise ValueError(msg("prompt_missing", PROMPTS + name + ".md")) from None
    return fill(text, values or {})


def names(text: str) -> list[str]:
    return [found.group("braced") for found in Text.pattern.finditer(text)]


@functools.lru_cache(maxsize=None)
def source(template: str) -> str:
    try:
        with open(os.path.join(TEMPLATES, *template.split("/")) + ".md", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        raise ValueError(msg("template_missing", template)) from None
    return text[:-1] if text.endswith("\n") else text


@functools.lru_cache(maxsize=None)
def catalog() -> dict:
    with open(os.path.join(TEMPLATES, os.path.basename(REGISTRY)), encoding="utf-8") as handle:
        return registry(handle.read())


def variables() -> dict[str, dict]:
    return catalog().get("variables", {})


def render(template: str, values: Mapping[str, object]) -> str:
    lines, dropped, kept, lost = [], False, False, False
    for line in source(template).split("\n"):
        used = names(line)
        current = {}
        for name in used:
            part = variables().get(name, {}).get("part")
            if name not in values and not part:
                raise ValueError(msg("template_value_missing", template, name))
            current[name] = str(values[name]) if name in values else render(part, values)
        if used and not any(current.values()):
            dropped = lost = True
            continue
        if dropped and not line and lines and not lines[-1]:
            continue
        dropped = False
        lines.append(Text(line).substitute(current))
        kept = kept or bool(used)
    if template.startswith(PARTS) and lost and not kept:
        return ""
    while dropped and lines and lines[-1] in ("", "---"):
        lines.pop()
    return "\n".join(lines)


def choice(name: str, key: str) -> str:
    found = variables().get(name, {}).get("values", {})
    if key not in found:
        raise ValueError(msg("template_value_choice", name, key, ", ".join(found)))
    return found[key]


def mark(template: str, values: Mapping[str, object]) -> str:
    try:
        return text_data.mark(template, values)
    except ValueError:
        raise ValueError(msg("template_mark_value", template)) from None


def published(template: str, text: str, data: Mapping[str, object] | None = None, *after: str) -> str:
    from authorship import sign

    return sign("\n\n".join([text, mark(template, {**(data or {}), "Digest": text_data.digest(text)}), *after]) + "\n")


def registry(text: str) -> dict:
    return json.loads(text) if text else {}


def calls(files: Mapping[str, str], code: re.Pattern = CALL, docs: re.Pattern | None = COMMAND
          ) -> dict[str, list[tuple[str, int]]]:
    found: dict[str, list[tuple[str, int]]] = {}
    for path, text in files.items():
        pattern = code if path.endswith(".py") else docs if path.endswith(".md") else None
        if pattern and "text_templates" in text:
            for call in pattern.finditer(text):
                found.setdefault(call.group(1), []).append((path, line_of(text, call.start(1))))
    return found


def templates(files: Mapping[str, str], forms: bool = False) -> dict[str, tuple[str, str]]:
    return {path[len(FOLDER):-len(".md")]: (path, text) for path, text in files.items()
            if path.startswith(FOLDER) and path.endswith(".md") and path.startswith(FOLDER + FORMS) == forms}


def surface(files: Mapping[str, str]) -> list[Violation]:
    found = []
    shipped = templates(files)
    text = files.get(REGISTRY, "")
    try:
        data = registry(text)
    except ValueError as error:
        return [Violation(REGISTRY, 1, msg("template_registry_unread"), str(error))]
    declared = data.get("variables", {})
    kit_forms = templates(files, forms=True)
    style = data.get("style")
    if style is not None and not requirement(files, str(style)):
        found.append(Violation(REGISTRY, line_of(text, text.find('"style"')), msg("template_style_missing"),
                               str(style)))
    called = calls(files)
    for template, places in called.items():
        if template not in shipped:
            found += [Violation(path, line, msg("template_call_missing"), template) for path, line in places]
    used = set()
    parts = {entry.get("part") for entry in declared.values() if isinstance(entry, dict)}
    for template, (path, body) in shipped.items():
        if template not in (parts if template.startswith(PARTS) else called):
            found.append(Violation(path, 1, msg("template_uncalled"), template))
        for number, line in enumerate(body.split("\n"), 1):
            for name in names(line):
                used.add(name)
                if name not in declared:
                    found.append(Violation(path, number, msg("template_variable_undeclared"), name))
            for token in FORBIDDEN:
                if token in line:
                    found.append(Violation(path, number, msg("template_forbidden"), token))
    asked = calls(files, PROMPT_CALL, None)
    for name, places in asked.items():
        if PROMPTS + name + ".md" not in files:
            found += [Violation(path, line, msg("prompt_call_missing"), name) for path, line in places]
    for path in files:
        if path.startswith(CALLED_PROMPTS) and path.endswith(".md") and path[len(PROMPTS):-len(".md")] not in asked:
            found.append(Violation(path, 1, msg("prompt_uncalled"), path))
    for path, body in prompts(files).items():
        used.update(names(body))
        for number, (line, inside) in enumerate(text_forms.fenced(body.split("\n")), 1):
            found.extend(Violation(path, number, msg("template_variable_undeclared"), name)
                         for name in names(line) if not name.isupper() and name not in declared)
            prose = "" if inside else INLINE.sub("", line)
            found.extend(Violation(path, number, msg("prompt_placeholder_old"), token.group(0))
                         for token in [*OLD.finditer(line), *PRINTF.finditer(prose)])
    for name, entry in declared.items():
        number = line_of(text, text.find(f'"{name}"'))
        if not NAME.fullmatch(name):
            found.append(Violation(REGISTRY, number, msg("template_variable_name"), name))
        if not isinstance(entry, dict) or not entry.get("about") or set(entry) - FIELDS or "values" in entry and not (
                isinstance(entry["values"], dict) and entry["values"]
                and all(isinstance(text, str) and text for text in entry["values"].values())) or "required" in entry and not (
                entry["required"] is True and entry.get("prompt")):
            found.append(Violation(REGISTRY, number, msg("template_variable_entry"), name))
        if name not in used:
            found.append(Violation(REGISTRY, number, msg("template_variable_unused"), name))
        part = entry.get("part") if isinstance(entry, dict) else None
        if part is not None and not (str(part).startswith(PARTS) and part in shipped):
            found.append(Violation(REGISTRY, number, msg("template_part_missing"), str(part)))
        prompt = entry.get("prompt") if isinstance(entry, dict) else None
        if prompt is not None and not requirement(files, str(prompt)):
            found.append(Violation(REGISTRY, number, msg("template_prompt_missing"), str(prompt)))
        form = str(entry.get("form", "")) if isinstance(entry, dict) else ""
        if "/" in form and text_forms.FILL not in kit_forms.get(form, ("", ""))[1]:
            found.append(Violation(REGISTRY, number, msg("template_form_kit_missing"), form))
    named = {str(entry.get("form")) for entry in declared.values() if isinstance(entry, dict)}
    found += [Violation(path, 1, msg("template_form_unnamed"), name)
              for name, (path, _) in kit_forms.items() if name not in named]
    return found


def prompts(files: Mapping[str, str]) -> dict[str, str]:
    try:
        phrases = json.loads(files.get(METHODOLOGY, "") or "{}").get("prompts", {}).get("phrases", [])
    except (ValueError, AttributeError):
        phrases = []
    found = {path: text for path, text in files.items() if path.endswith(".md") and path.startswith(PROMPTS)}
    phrase = "\n".join(str(item.get("text", "")) for item in phrases if isinstance(item, dict))
    if phrase:
        found[METHODOLOGY] = phrase
    return found


def headings(lines: list[str]):
    from process_validation import anchor

    fenced = False
    for number, line in enumerate(lines):
        fenced ^= line.startswith("```")
        if not fenced and line.startswith("#"):
            yield number, len(line) - len(line.lstrip("#")), anchor(line)


def requirement(files: Mapping[str, str], link: str) -> bool:
    path, _, target = link.partition("#")
    return bool(target) and any(name == target for _, _, name in headings(files.get(path, "").split("\n")))


def section(link: str) -> str:
    path, _, target = link.partition("#")
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(TEMPLATES)), path), encoding="utf-8") as handle:
            lines = handle.read().split("\n")
    except OSError:
        return ""
    found = list(headings(lines))
    start = next(((number, depth) for number, depth, name in found if name == target), None)
    if start is None:
        return ""
    end = next((number for number, depth, _ in found if number > start[0] and depth <= start[1]), len(lines))
    return "\n".join(lines[start[0] + 1:end]).strip()


def tree() -> dict[str, str]:
    from text_scan import read, tracked

    return {path: read(path) for path in tracked() if path.endswith((".py", ".md"))}


def command_list(_args: argparse.Namespace) -> int:
    files = tree()
    called = calls(files)
    for template, (_path, text) in sorted(templates(files).items()):
        places = ", ".join(f"{path}:{line}" for path, line in called.get(template, []))
        print(msg("template_list_line", template, ", ".join(dict.fromkeys(names(text))) or msg("value_absent"),
                  places or msg("value_absent")))
    return 0


def command_show(args: argparse.Namespace) -> int:
    text = source(args.template)
    declared = variables()
    print(text)
    print()
    shown: set[str] = set()
    for name in dict.fromkeys(names(text)):
        entry = declared.get(name, {})
        about = entry.get("about", msg("value_absent"))
        if entry.get("values"):
            print(msg("template_show_values", name, about,
                      ", ".join(f"{key} - {text}" for key, text in entry["values"].items())))
        else:
            print(requirement_text(name, entry.get("prompt") not in shown))
            shown.add(entry.get("prompt"))
    if any(declared.get(name, {}).get("prompt") for name in names(text)):
        print("\n" + manner())
    return 0


def requirement_text(name: str, full: bool = True) -> str:
    entry = variables().get(name, {})
    about = entry.get("about", msg("value_absent"))
    if not entry.get("prompt"):
        return msg("template_show_variable", name, about)
    if not full:
        return msg("template_show_agent_seen", name, about, entry["prompt"])
    return msg("template_show_agent", name, about, entry["prompt"]) + "\n" + section(entry["prompt"]) + "\n"


def manner() -> str:
    link = catalog().get("style")
    return msg("template_show_style", link) + "\n" + section(link) + "\n" if link else ""


def command_form(args: argparse.Namespace) -> int:
    spec = variables().get(args.name, {}).get("form")
    if not spec:
        raise ValueError(msg("template_form_unknown", args.name))
    emit(args.out, text_forms.skeleton(spec))
    print(requirement_text(args.name) + "\n" + manner(), file=sys.stderr)
    return 0


def emit(out: str | None, text: str) -> None:
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def pairs(items: list[str], read: bool) -> dict[str, str]:
    found = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not NAME.fullmatch(name):
            raise ValueError(msg("template_value_pair", item))
        if read:
            try:
                with open(value, encoding="utf-8") as handle:
                    value = handle.read()
            except OSError as error:
                raise ValueError(msg("template_value_file", name, error)) from None
        found[name] = value.strip()
    return found


def runtime(args: argparse.Namespace, used: set[str]) -> tuple[dict[str, str], dict[str, str], str]:
    found = {}
    if args.change is not None:
        import forge

        found["ChangeLink"] = forge.change_link(args.change)
    if args.commit:
        found["SHA"] = git("rev-parse", "--verify", "--quiet", args.commit + "^{commit}")
        if not found["SHA"]:
            raise ValueError(msg("template_commit_unknown", args.commit))
    if used & {"SessionLink", "SessionModel"}:
        from agent_provider import session_link, session_model

        try:
            found["SessionLink"], found["SessionModel"] = session_link(), session_model(args.session or "")
        except RuntimeError as error:
            raise ValueError(str(error)) from None
    data = {key: value for key, value in found.items() if key not in ("SessionLink", "SessionModel")}
    published = ""
    if args.change is not None and used & {"ChangeIssues", "ChangeStats"}:
        try:
            published = forge.change_body(args.change) or ""
        except RuntimeError as error:
            raise ValueError(str(error)) from None
    import change_cost

    head, tail = change_cost.parts(published)
    if "ChangeIssues" in used:
        import tracker

        links: dict[str, bool] = {}
        if head and not (args.issue or args.refs):
            from markdown_text import without_code

            for line in without_code(head).splitlines():
                found_refs = tracker.references(line)
                item = found_refs[0] if len(found_refs) == 1 else None
                if item and line.strip() in (item.phrase, tracker.link_phrase(item.number, item.closing)):
                    links[item.number] = links.get(item.number, False) or item.closing
        links.update(dict.fromkeys(args.refs, False))
        links.update(dict.fromkeys(args.issue, True))
        if links:
            found["ChangeIssues"] = "\n".join(tracker.link_phrase(key, closing)
                                              for closing in (True, False) for key in links if links[key] is closing)
    if "ChangeFixes" in used and args.change is not None:
        import post_approval

        try:
            fixes = post_approval.accepted(args.change)
        except RuntimeError as error:
            raise ValueError(str(error)) from None
        if fixes:
            found["ChangeFixes"] = render("parts/change-fixes", {
                "FixCommits": "\n".join(f"- {sha[:9]} {subject}" for sha, subject in fixes)})
    if "ChangeStats" in used and (args.session or published):
        try:
            kept = change_cost.kept(published) if published else None
            if args.session and (not kept or kept[0] == args.session and change_cost.missing(kept[1])):
                from agent_provider import session_link

                kept = args.session, {**change_cost.dev(args.session, args.change), "link": session_link()}
            elif args.session and kept[0] == args.session:
                kept = args.session, change_cost.reread(args.session, kept[1])
        except RuntimeError as error:
            raise ValueError(str(error)) from None
        if kept:
            session, item = kept
            found["ChangeStats"] = change_cost.stats(item)
            data.update(Session=session, Dev=json.dumps(item, sort_keys=True))
    return found, data, tail if args.template == DESCRIPTION else ""


def linked(text: str) -> str:
    import forge

    base = BASE_BRANCH.removeprefix("origin/")

    def absolute(found: re.Match) -> str:
        if not os.path.exists(os.path.join(ROOT, found.group(1))):
            return found.group(0)
        url = forge.file_url(base, found.group(1))
        return f"]({url}" if url else found.group(0)

    def prose(line: str) -> str:
        parts = INLINE_SPLIT.split(line)
        return "".join(part if index % 2 else ROOTED.sub(absolute, part) for index, part in enumerate(parts))

    lines = text_forms.fenced(text.splitlines())
    return "\n".join(line if inside else prose(line) for line, inside in lines)


def trailed(text: str, key: str, value: str) -> str:
    return subprocess.run(["git", "interpret-trailers", "--if-exists", "doNothing", "--trailer", f"{key}: {value}"],
                          input=text + "\n", capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout.rstrip("\n")


def command_render(args: argparse.Namespace) -> int:
    used = set(names(source(args.template)))
    declared = variables()
    given = {**pairs(args.set, False), **pairs(args.set_file, True)}
    for name in given:
        entry = declared.get(name, {})
        if name not in used or not (entry.get("prompt") or entry.get("values")):
            raise ValueError(msg("template_value_agent", args.template, name))
        if entry.get("required") and not given[name]:
            raise ValueError(msg("template_value_empty", args.template, name))
        if entry.get("values"):
            given[name] = choice(name, given[name])
        elif entry.get("form"):
            given[name] = text_forms.conform(name, entry["form"], given[name])
        given[name] = text_data.inert(given[name])
    known, data, tail = runtime(args, used)
    values = {**{name: "" for name in used & RUNTIME}, **known, **given}
    text = render(args.template, values)
    if args.template == DESCRIPTION or args.template.startswith(PUBLISHED):
        text = linked(text)
    if args.template == COMMIT and args.change is not None:
        import forge

        if url := forge.change_url(args.change):
            text = trailed(text, "PR", url)
    body = text + "\n"
    if args.template.startswith(PUBLISHED):
        body = published(args.template, text, data)
    elif args.template == DESCRIPTION:
        record = mark(args.template, {**data, "Digest": text_data.digest(text)})
        body = text + "\n\n" + record + ("\n\n" + tail if tail else "") + "\n"
    emit(args.out, body)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list").set_defaults(run=command_list)
    show = commands.add_parser("show")
    show.add_argument("template")
    show.set_defaults(run=command_show)
    blank = commands.add_parser("form", help="заготовка формы для переменной с полем form: форма комплекта из templates/forms/ или форма площадки по пути включенного расширения, первой строкой ее sha; требование к переменной и правила языка - в stderr")
    blank.add_argument("name", metavar="ИМЯ")
    blank.add_argument("--out", help="файл для заготовки; без него она печатается")
    blank.set_defaults(run=command_form)
    draft = commands.add_parser("render", help="текст агента по шаблону; комментарию и задаче - с маркером данных и меткой автора, описанию PR - с маркером данных")
    draft.add_argument("template")
    draft.add_argument("--set", action="append", default=[], metavar="ИМЯ=ТЕКСТ")
    draft.add_argument("--set-file", action="append", default=[], metavar="ИМЯ=ФАЙЛ")
    draft.add_argument("--change", type=int, help="номер PR: адрес идет в ${ChangeLink}, у commit/message - трейлером PR")
    draft.add_argument("--commit", help="коммит: полный SHA идет в ${SHA}")
    draft.add_argument("--issue", action="append", default=[], metavar="ЗАДАЧА", help="закрывающая связь с задачей в ${ChangeIssues}")
    draft.add_argument("--refs", action="append", default=[], metavar="ЗАДАЧА", help="ссылочная связь с задачей в ${ChangeIssues}")
    draft.add_argument("--session", help="сессия агента: ссылка на нее и затраты разработки идут в ${ChangeStats} и в маркер описания PR, с --change их берут из опубликованного описания; модель сессии идет в ${SessionModel}")
    draft.add_argument("--out", help="файл для текста; без него текст печатается")
    draft.set_defaults(run=command_render)
    args = parser.parse_args()
    try:
        return args.run(args)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
