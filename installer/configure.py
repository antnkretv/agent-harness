from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from .catalog import (
    branch_problems,
    enabled,
    entries_shape,
    inapplicable,
    is_on,
    load_catalog,
    lookup,
    modules_of,
    own_reason,
    package_scripts,
    pack_skills,
    shared_extensions,
    shipped_skills,
    unmet,
    validate,
)
from .detect import GENERATED_FOLDERS, HISTORICAL_FOLDERS, REMOTE_REPO, STACK_MARKERS, Tree
from .documents import json_text
from .questions import (
    PACKED,
    DEFAULTS,
    MISSING,
    STACK,
    SUMMARIES,
    Pending,
    Question,
    Session,
    app_check,
    choice_answers,
    choice_groups,
    choice_question,
    decision_question,
    docs_list,
    evidence_text,
    fact_question,
    listed,
    listed_question,
    matching,
    object_parser,
    one_of,
    option,
    optional_text,
    present,
    proposal,
    setting_parser,
    summary_question,
)
from .resources import (
    ANSWERS,
    APP_FIELDS,
    BRANCH,
    BRANCH_CONVENTIONS,
    CONFIG,
    DOCS,
    DOCS_ASKED,
    GIT_PREFIX,
    HERE,
    IDENTITY,
    KNOWN_STACKS,
    NAME,
    NONE,
    OFF,
    ON,
    OWN_SECTIONS,
    PROJECT,
    PROJECT_NAME,
    Refusal,
    SERVICE_AREAS,
    SHAPES,
    SIDES,
    STACKS_WITH_ADAPTER,
    app_roles,
    load_json,
    msg,
    sha256,
    slug,
)

NODE_ROLES = {"format": ("format:check",), "lint": ("lint",), "analyze": ("typecheck", "type-check"),
              "architecture": ("lint:architecture",), "test": ("test", "test:unit")}
CONFIG_NAMES = ("Directory.Build.props", "Directory.Build.targets", ".editorconfig", ".vale.ini",
                "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts")


def module_evidence(s: Session, name: str) -> list[str]:
    shared = shared_extensions(s.catalog) if name in SIDES else set()
    return s.evidence(name, "") + [line for extension in s.catalog["modules"][name].get("extensions", {})
                                   if extension not in shared for line in s.evidence(name, extension)]


def configured(s: Session) -> dict:
    shape = s.ask(shape_question(s))
    identity = None if s.decisions else identity_of(s)
    modules = module_entries(s, SHAPES[shape])
    agents = agent_entries(s)
    if s.decisions:
        return {"modules": modules, "agents": agents}
    own = {key: s.value(key, object_parser, {}) for key in OWN_SECTIONS}
    return {"_": [msg("project_note_1"), msg("project_note_2")], **identity,
            "docs": docs_entries(s, modules), "agents": agents, "apps": app_entries(s, modules), "modules": modules,
            **{key: value for key, value in own.items() if value}}


def agent_entries(s: Session) -> dict:
    return {key: s.ask(decision_question(f"agents.{key}", "agents", meta,
                                        setting_parser("agents", key, meta), meta["default"]))
            for key, meta in s.catalog["agents"].items()}


def docs_entries(s: Session, modules: dict) -> dict:
    docs = {**json.loads(json.dumps(DOCS)), **s.value("docs", object_parser, {})}
    shipped = shipped_skills(s.catalog)
    for key, texts in DOCS_ASKED.items():
        current = own_skills(docs.get(key, []), shipped) if key == "vendoredSkills" else docs.get(key, [])
        default, evidence = docs_proposal(s.tree, key, current, shipped)
        text, rule = texts()
        docs[key] = s.ask(Question(f"docs.{key}", "text", text, [], default, evidence_text(evidence), rule, docs_list,
                                   detected=bool(evidence)))
    paths = enabled({"modules": modules})
    docs["vendoredSkills"] = own_skills(docs["vendoredSkills"], shipped) + sorted(
        name for path, names in pack_skills(s.catalog).items() if path in paths for name in names)
    return docs


def own_skills(names, shipped: frozenset[str]):
    return [name for name in names if name not in shipped] if isinstance(names, list) else names


def docs_proposal(tree: Tree, key: str, current: list, shipped: frozenset[str] = frozenset()) -> tuple[list, list[str]]:
    current = list(current) if isinstance(current, list) else []
    found: dict[str, str] = {}
    if key == "generatedSegments":
        for path in tree.files:
            for folder in posixpath.dirname(path).split("/"):
                segment = f"/{folder}/"
                if folder in GENERATED_FOLDERS and segment not in current:
                    found.setdefault(segment, path)
    elif key == "historicalPrefixes":
        for path in tree.files:
            parts = path.split("/")[:-1]
            hit = next((index for index, folder in enumerate(parts) if folder in HISTORICAL_FOLDERS), None)
            prefix = "/".join(parts[:hit + 1]) + "/" if hit is not None else ""
            if prefix and path.endswith(".md") and not any(prefix.startswith(known) for known in current):
                found.setdefault(prefix, path)
    elif key == "vendoredSkills":
        for path in tree.glob((".agents/skills/*/LICENSE*",)):
            name = path.split("/")[2]
            if name not in current and name not in shipped:
                found.setdefault(name, path)
    return current + sorted(found), [msg("why_docs_found", value, path) for value, path in sorted(found.items())]


def shape_question(s: Session) -> Question:
    backend = module_evidence(s, "backend") + [msg("why_file", path) for path in s.tree.glob(tuple(STACK_MARKERS))]
    frontend = [line for extension in s.catalog["modules"]["frontend"].get("extensions", {})
                for line in s.evidence("frontend", extension)]
    default = "fullstack" if backend and frontend else "backend" if backend else "frontend" if frontend else None
    options = [option("fullstack", msg("shape_fullstack"), msg("shape_fullstack_note")),
               option("backend", msg("shape_backend"), msg("shape_backend_note")),
               option("frontend", msg("shape_frontend"), msg("shape_frontend_note"))]
    return Question("shape", "single", msg("q_shape"), options, default, evidence_text(backend + frontend),
                    parse=one_of(list(SHAPES)), decision=True, detected=default is not None,
                    batch=STACK if default else "", hint=(backend + frontend or [""])[0])


def identity_of(s: Session) -> dict:
    repo = REMOTE_REPO.search(s.tree.remote) if s.tree.remote else None
    name = s.ask(Question("project.name", "text", msg("q_project_name"), [], repo.group(1).rsplit("/", 1)[-1] if repo else s.tree.name,
                          msg("why_remote", s.tree.remote) if repo else msg("why_folder", s.tree.name),
                          parse=matching(PROJECT_NAME), detected=True))
    prefix = s.ask(Question("project.gitConfigPrefix", "text", msg("q_git_prefix"), [], slug(name).strip("-"),
                            msg("why_slug", name), parse=matching(GIT_PREFIX), detected=True))
    branches = msg("why_branches", ", ".join(sorted(s.tree.branches)) or "-")
    found = s.tree.branch("develop", "dev")
    base = s.ask(Question("git.baseBranch", "text", msg("q_base_branch"), [], found or "develop", branches,
                          parse=matching(BRANCH), detected=found is not None))

    def release_branch(raw) -> str:
        value = matching(BRANCH)(raw)
        problems = branch_problems(base, value)
        if problems:
            raise ValueError(problems[0])
        return value

    found = s.tree.branch("master", "main")
    release = s.ask(Question("git.releaseBranch", "text", msg("q_release_branch"), [], found or "master",
                             branches, parse=release_branch, detected=found is not None))
    convention = s.ask(Question("git.branchConvention", "single", msg("q_branch_convention"),
                                [option("conventional", msg("convention_conventional"), msg("convention_conventional_note")),
                                 option("gitflow", msg("convention_gitflow"), msg("convention_gitflow_note"))],
                                BRANCH_CONVENTIONS[0], parse=one_of(list(BRANCH_CONVENTIONS)), decision=True))
    git = {"baseBranch": base, "releaseBranch": release, "branchConvention": convention}
    return {"project": {"name": name, "gitConfigPrefix": prefix}, "git": git}


def module_entries(s: Session, sides: tuple[str, ...]) -> dict:
    entries: dict = {}
    for name in s.catalog["order"]:
        spec = s.catalog["modules"][name]
        paths = enabled({"modules": entries})
        if unmet(spec, paths):
            entries[name] = {"off": inapplicable(spec)}
            continue
        chosen = choice(s, name, spec, sides, paths)
        entries[name] = {"off": off_reason(s, name, spec)} if chosen is None else module_settings(s, name, spec, chosen, entries)
    return entries


def choice(s: Session, name: str, spec: dict, sides: tuple[str, ...], paths: frozenset[str]) -> list[str] | None:
    if name in SIDES and name not in sides:
        return None
    extensions = {extension: meta for extension, meta in spec.get("extensions", {}).items() if not unmet(meta, paths)}
    evidence = {extension: s.evidence(name, extension) for extension in extensions}
    ignored = shared_extensions(s.catalog) if name == "backend" and "frontend" in sides else set()
    found = [extension for extension in extensions if evidence[extension] and extension not in ignored]
    chosen = []
    for index, group in enumerate(choice_groups(name, spec)):
        members = [extension for extension in group.members if extension in extensions]
        first = index == 0
        values = members + ([NONE if members else ON] if group.bounds.get("min", 0) == 0 else []) \
            + ([OFF] if first and not spec.get("required") and name not in SIDES else [])
        if len(values) == 1:
            picked = values
        else:
            question = choice_question(name, spec, group, first, values, extensions, evidence,
                                       [extension for extension in found if extension in members],
                                       s.evidence(name, "") if first else [])
            value = s.ask(question)
            picked = value if isinstance(value, list) else [value]
        if OFF in picked:
            return None
        chosen += [extension for extension in members if extension in picked]
    return chosen


def off_reason(s: Session, name: str, spec: dict) -> str:
    reason = spec.get("off", {}).get("reason", msg("off_module", name))
    evidence, key = module_evidence(s, name), f"modules.{name}.off"
    if not evidence and not s.unowned(key):
        return s.value(key, present, reason, decision=True)
    why = msg("why_found", evidence_text(evidence)) if evidence else ""
    return s.ask(Question(key, "text", msg("q_off_reason", spec["title"]), [], reason, why, reason, parse=present,
                          decision=True))


def module_settings(s: Session, name: str, spec: dict, chosen: list[str], entries: dict) -> dict:
    holders = [(f"modules.{name}.settings", name, spec.get("settings", {}))]
    holders += [(f"modules.{name}.extensions.{extension}", f"{name}/{extension}",
                 spec["extensions"][extension].get("settings", {})) for extension in chosen]
    values: dict[str, dict] = {prefix: {} for prefix, _, _ in holders}
    paths = enabled({"modules": entries})
    for prefix, where, specs in holders:
        for key, meta in specs.items():
            if meta["kind"] != "decision" or unmet(meta, paths):
                continue
            if "default" in meta:
                value, why = proposal(s, where, key, meta)
                found = s.ask(decision_question(f"{prefix}.{key}", where, meta, setting_parser(where, key, meta), value, why))
            else:
                found = setting_value(s, f"{prefix}.{key}", where, key, meta, entries)
            if found is not MISSING:
                values[prefix][key] = found
    for prefix, where, specs in holders if not s.decisions else []:
        for key, meta in specs.items():
            if meta["kind"] == "fact":
                found = setting_value(s, f"{prefix}.{key}", where, key, meta, entries)
                if found is not MISSING:
                    values[prefix][key] = found
    ordered = [{key: values[prefix][key] for key in specs if key in values[prefix]} for prefix, _, specs in holders]
    entry = {"settings": ordered[0]} if spec.get("settings") else {}
    entry["extensions"] = dict(zip(chosen, ordered[1:]))
    return entry


def setting_value(s: Session, key: str, where: str, name: str, meta: dict, entries: dict):
    parse = setting_parser(where, name, meta)
    if meta["kind"] == "decision":
        if "in" in meta:
            return s.ask(listed_question(entries, key, where, meta, parse))
        return s.ask(decision_question(key, where, meta, parse))
    value = s.value(key, parse, MISSING)
    if value is MISSING:
        value = s.ask(fact_question(s, key, where, meta, parse))
    return MISSING if value == "" else value


def app_sides(stack: str, paths: frozenset[str]) -> list[str]:
    if stack in STACKS_WITH_ADAPTER:
        return [side for side in SIDES if f"{side}/{stack}" in paths]
    if stack == "other":
        return [side for side in SIDES if side in paths]
    return ["backend"] * ("backend" in paths)


APP_FACTS = frozenset({"path", "stack", "doc", "skill"})


def app_candidates(s: Session, modules: dict, paths: frozenset[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}

    def add(raw: str, record: dict) -> None:
        name = slug(raw).strip("-") or "app"
        while name in SERVICE_AREAS or name in found:
            name += "-app"
        if app_sides(record["stack"], paths):
            found[name] = record

    nx_roots = {posixpath.dirname(path) or "." for path in s.tree.glob(("nx.json",))}

    def under_nx(path: str) -> bool:
        folder = posixpath.dirname(path) or "."
        return any(root != folder and (root == "." or folder.startswith(root + "/")) for root in nx_roots)

    packages = [path for path in s.tree.glob(("package.json",)) if not under_nx(path)]
    nested = any("/" in path for path in packages)
    for path in packages:
        folder = posixpath.dirname(path) or "."
        if folder == "." and nested:
            continue
        scripts = s.tree.package(path).get("scripts") or {}
        entry = "check" if "check" in scripts else "lint" if "lint" in scripts else None
        roles = {role: {"command": f"npm run {name}"} for role, names in NODE_ROLES.items()
                 if (name := next((name for name in names if name in scripts), None))}
        if "lint" in roles:
            roles.setdefault("architecture", roles["lint"])
        add(posixpath.basename(folder) if folder != "." else s.tree.name,
            {"path": folder, "stack": "node", "check": entry and f"npm run {entry}", "entries": roles,
             "why": msg("why_file", path)})
    dotnet = (modules.get("backend", {}).get("extensions") or {}).get("dotnet") or {}
    if dotnet.get("solution"):
        solution = posixpath.basename(dotnet["solution"])
        configuration = dotnet["configuration"]
        build, test = f"dotnet build {solution} -c {configuration}", f"dotnet test {solution} -c {configuration} --no-build"
        add("dotnet", {"path": posixpath.dirname(dotnet["solution"]) or ".", "stack": "dotnet",
                       "check": f"{build} && {test}", "entries": {"analyze": {"command": build}, "test": {"command": test}},
                       "why": msg("why_file", dotnet["solution"])})
    for path in s.tree.glob(tuple(STACK_MARKERS)):
        folder = posixpath.dirname(path) or "."
        stack = STACK_MARKERS[posixpath.basename(path)]
        add(stack if folder == "." else folder, {"path": folder, "stack": stack, "check": None, "entries": {},
                                                 "why": msg("why_file", path)})
    configs = s.tree.glob(CONFIG_NAMES)
    for record in found.values():
        record["configs"] = [path for path in configs if (posixpath.dirname(path) or ".") in ancestors(record["path"])]
    return found


def ancestors(folder: str) -> set[str]:
    parts = [] if folder == "." else folder.split("/")
    return {"/".join(parts[:size]) or "." for size in range(len(parts) + 1)}


def entries_parser(roles: tuple[str, ...]):
    def parse(raw) -> dict:
        value = {} if raw in ("", None) else object_parser(raw)
        if not entries_shape(value, roles):
            raise ValueError(msg("answer_entries", ", ".join(roles)))
        return value
    return parse


def app_entries(s: Session, modules: dict) -> list[dict]:
    paths = enabled({"modules": modules})
    candidates = app_candidates(s, modules, paths)
    custom = sorted({key.split(".")[1] for key in s.answers
                     if key.startswith("apps.") and key.endswith(".path") and key.split(".")[1] not in candidates})
    ids = list(candidates) + custom
    if not ids:
        return []
    def chosen(raw) -> list[str]:
        picked = listed(raw)
        unknown = [name for name in picked if name not in ids]
        if unknown:
            raise ValueError(msg("answer_not_option", ", ".join(map(str, unknown)), ", ".join(ids)))
        return [name for name in ids if name in picked]

    labels = {name: f"{record['path']} ({record['stack']})" for name, record in candidates.items()}
    question = Question("apps", "multi", msg("q_apps"),
                        [option(name, labels.get(name, name), (candidates.get(name) or {}).get("check") or "") for name in ids],
                        list(candidates), evidence_text([record["why"] for record in candidates.values()]),
                        parse=chosen, detected=True)
    apps = []
    for name in s.ask(question):
        base = candidates.get(name, {})
        where = f"apps.{name}"
        path = s.value(f"{where}.path", matching(r"[^\s/][^\s]*"), base.get("path"))
        stack = s.value(f"{where}.stack", one_of(list(KNOWN_STACKS)), base.get("stack")) if base else \
            s.ask(Question(f"{where}.stack", "text", msg("q_app_stack", name), [], None, rule=", ".join(KNOWN_STACKS),
                           parse=one_of(list(KNOWN_STACKS))))
        sides = app_sides(stack, paths) or list(SIDES)
        side = sides[0] if len(sides) == 1 else s.ask(Question(
            f"{where}.side", "single", msg("q_app_side", name, path),
            [option(item, item) for item in sides], "frontend" if "frontend" in sides else sides[0], parse=one_of(sides)))
        app = {"id": name, "path": path, "side": side, "stack": stack}
        scripts = package_scripts(s.tree.root / path / "package.json") if stack == "node" and s.tree.root else None
        app["check"] = s.ask(Question(f"{where}.check", "text", msg("q_app_check", name), [], base.get("check"),
                                      base.get("why", ""), msg("rule_app_check"), parse=app_check(stack, scripts), detected=bool(base)))
        roles = app_roles()
        entries = s.ask(Question(f"{where}.entries", "text", msg("q_app_entries", name), [], base.get("entries") or {},
                                 base.get("why", ""), msg("rule_app_entries", ", ".join(roles)), parse=entries_parser(roles),
                                 detected=bool(base.get("entries"))))
        if entries:
            app["entries"] = entries
        configs = s.ask(Question(f"{where}.configs", "text", msg("q_app_configs", name), [], base.get("configs") or [],
                                 base.get("why", ""), msg("rule_app_configs"), parse=docs_list, detected=bool(base.get("configs"))))
        if configs:
            app["configs"] = configs
        if stack not in STACKS_WITH_ADAPTER and "architecture" not in entries:
            app["architectureWaiver"] = s.ask(Question(f"{where}.architectureWaiver", "text", msg("q_app_waiver", name),
                                                       rule=msg("rule_app_waiver"), parse=present))
        if stack == "node":
            waiver = s.ask(Question(f"{where}.architectureWaiver", "text", msg("q_app_layers", name), [], "",
                                    rule=msg("rule_app_layers"), parse=optional_text))
            if waiver:
                app["architectureWaiver"] = waiver
                if entries.get("architecture") == entries.get("lint"):
                    entries.pop("architecture", None)
                    if not entries:
                        app.pop("entries", None)
        for extra in ("doc", "skill"):
            found = s.value(f"{where}.{extra}", optional_text, "")
            if found:
                app[extra] = found
        apps.append(app)
    return apps


def answers_from(project: dict, catalog: dict) -> dict:
    found: dict = {}
    modules = modules_of(project)
    sides = tuple(side for side in SIDES if is_on(modules.get(side)))
    found.update({"shape": name for name, shape in SHAPES.items() if shape == sides})
    found.update({key: lookup(project, key) for key in IDENTITY if lookup(project, key) is not None})
    found.update({f"agents.{key}": project["agents"][key] for key in catalog["agents"]
                  if isinstance(project.get("agents"), dict) and key in project["agents"]})
    docs = project.get("docs") if isinstance(project.get("docs"), dict) else {}
    found.update({f"docs.{key}": docs[key] for key in DOCS_ASKED if key in docs})
    if "docs.vendoredSkills" in found:
        found["docs.vendoredSkills"] = own_skills(found["docs.vendoredSkills"], shipped_skills(catalog))
    if changed := {key: value for key, value in docs.items() if key not in DOCS_ASKED and DOCS.get(key) != value}:
        found["docs"] = changed
    found.update({key: project[key] for key in OWN_SECTIONS if isinstance(project.get(key), dict) and project[key]})
    for name, spec in catalog["modules"].items():
        entry = modules.get(name)
        if entry is None:
            continue
        if not is_on(entry):
            reason = entry.get("off") if isinstance(entry, dict) else None
            if reason != inapplicable(spec):
                found[f"modules.{name}"] = OFF
                if isinstance(reason, str) and not reason.strip():
                    raise Refusal(msg("configure_bad_answer", f"modules.{name}.off", msg("answer_empty")))
                if own_reason(spec, reason):
                    found[f"modules.{name}.off"] = reason
            continue
        extensions = entry.get("extensions") if isinstance(entry.get("extensions"), dict) else {}
        found.update(choice_answers(name, spec, list(extensions)))
        for key, value in (entry.get("settings") or {}).items():
            found[f"modules.{name}.settings.{key}"] = value
        for extension, values in extensions.items():
            for key, value in (values or {}).items():
                found[f"modules.{name}.extensions.{extension}.{key}"] = value
    apps = [app for app in project.get("apps", []) if isinstance(app, dict) and isinstance(app.get("id"), str)]
    if isinstance(project.get("apps"), list):
        found["apps"] = [app["id"] for app in apps]
    for app in apps:
        found.update({f"apps.{app['id']}.{key}": app[key] for key in APP_FIELDS if key in app})
    return found


def known_id(key: str, catalog: dict) -> None:
    parts = key.split(".")
    if len(parts) == 2 and parts[0] == "agents" and parts[1] in catalog["agents"]:
        return
    if key in ("shape", "apps", "docs", *OWN_SECTIONS, *SUMMARIES) or key in IDENTITY or key.removeprefix("docs.") in DOCS_ASKED:
        return
    if parts[0] == "apps" and len(parts) == 3 and NAME.match(parts[1]) and parts[2] in APP_FIELDS:
        return
    if parts[0] == "apps" and parts[-1] == "architectureGate":
        raise Refusal(msg("configure_architecture_gate", key, parts[1]))
    head, _, tail = key.removeprefix("modules.").partition(".")
    name, _, slot = head.partition(":")
    spec = catalog["modules"].get(name) if key.startswith("modules.") else None
    if spec is not None:
        if any(key == question.id for question in choice_groups(name, spec)) or tail == "off" and not slot:
            return
        group, _, rest = tail.partition(".")
        if group == "extensions":
            extension, _, rest = rest.partition(".")
            specs = spec.get("extensions", {}).get(extension, {}).get("settings", {})
        else:
            specs = spec.get("settings", {}) if group == "settings" else {}
        setting_name, _, sub = rest.partition(".")
        meta = specs.get(setting_name)
        if not slot and meta and not sub:
            return
    raise Refusal(msg("configure_unknown_id", key))


def read_answers(root: Path) -> dict:
    path = root / ANSWERS
    return load_json(path, msg("answers_broken", path)) if path.exists() else {"answers": {}}


def manifest_stale(root: Path, state: dict) -> bool:
    target = root / PROJECT
    return target.is_file() and sha256(target.read_bytes()) != state.get("manifest")


def write_answers(root: Path, state: dict) -> None:
    data = {"_": [msg("answers_note_1"), msg("answers_note_2")]}
    if state.get("manifest"):
        data["manifest"], data["manifestKeys"] = state["manifest"], state.get("manifestKeys", [])
    data["answers"] = dict(sorted(state["answers"].items()))
    if state.get("reopened"):
        data["reopened"] = dict(sorted(state["reopened"].items()))
    if state.get("reopened"):
        data["alone"] = state.get("alone", [])
    (root / ANSWERS).parent.mkdir(parents=True, exist_ok=True)
    (root / ANSWERS).write_text(json_text(data), encoding="utf-8", newline="\n")


def packed_decisions() -> dict:
    path = HERE / CONFIG
    return load_json(path, msg("json_broken", path, "-")).get("decisions", {}) if path.is_file() else {}


MODULE_CHOICE = re.compile(r"modules\.[a-z0-9-]+(:[a-z0-9-]+)?")


def removed(item: Question, value):
    if not MODULE_CHOICE.fullmatch(item.id):
        return value
    offered = {option["value"] for option in item.options}
    for empty in (OFF, NONE):
        if empty in offered:
            return [empty] if item.kind == "multi" else empty
    return value


def owner_question(catalog: dict, tree: Tree, answers: dict, incoming: set, reopened: dict,
                   summaries: dict, alone: set, strict: bool, defaults: bool) -> Question | None:
    while True:
        session = Session(catalog, tree, answers, incoming, defaults=defaults, reopened=reopened, decisions=True,
                          batching=True, alone=alone, strict=strict)
        try:
            configured(session)
        except Pending:
            pass
        group = STACK if session.batch[STACK] else None if session.single else DEFAULTS if session.batch[DEFAULTS] else None
        if group is None:
            return session.single
        question = summary_question(group, session.batch[group])
        if group not in summaries:
            return question
        try:
            revised = question.parse(summaries.pop(group))
        except ValueError as error:
            raise Refusal(msg("configure_bad_answer", group, error)) from None
        for item, value in session.batch[group]:
            if item.id in revised:
                answers.pop(item.id, None)
                reopened[item.id] = removed(item, value)
                alone.add(item.id)
            else:
                listed = item.catalog_value is not None and value == item.catalog_value
                answers[item.id] = {"value": value, "by": "default" if listed else "owner"}


def migrate(answers: dict, reopened: dict, catalog: dict, alone: set) -> None:
    runners = answers.get("agents.runners", {})
    if isinstance(runners.get("value"), dict) and "agents.tools" not in answers:
        answers["agents.tools"] = {"by": runners.get("by", "owner"), "value": {
            name: {key: record[key] for key in ("allowedTools", "disallowedTools") if key in record}
            for name, record in runners["value"].items() if name in catalog["agents"]["tools"]["default"]
            and isinstance(record, dict)}}
    kinds = catalog["modules"]["ci-agent"]["extensions"]
    found = set()
    for module in (name for name, spec in catalog["modules"].items() if "ci-agent" in spec.get("requires", ())):
        entry = answers.get(f"modules.{module}", {})
        kind = entry.get("value")
        if kind not in kinds:
            continue
        found.add(kind)
        answers.setdefault("modules.ci-agent", {"value": kind, "by": entry.get("by", "owner")})
        entry["value"] = "on"
        auth = answers.pop(f"modules.{module}.extensions.{kind}.auth", None)
        if auth:
            answers.setdefault(f"modules.ci-agent.extensions.{kind}.auth", auth)
    if len(found) > 1:
        reopened["modules.ci-agent"] = answers.pop("modules.ci-agent")["value"]
        alone.add("modules.ci-agent")


def reopen_all(answers: dict, reopened: dict) -> None:
    for key in list(answers):
        by = answers[key].get("by")
        if by in ("detected", "default"):
            del answers[key]
        elif by != PACKED:
            entry = answers.pop(key)
            reopened[key] = entry.get("value", entry.get("raw"))


def settle(root: Path, given: dict | None = None, change: bool = False, strict: bool = False,
           defaults: bool = False) -> tuple[Question | None, list[str], list[dict]]:
    given = dict(given or {})
    catalog = load_catalog()
    state = read_answers(root)
    answers, reopened = state.setdefault("answers", {}), state.setdefault("reopened", {})
    alone = set(state.get("alone", reopened))
    migrate(answers, reopened, catalog, alone)
    for entries in (answers, reopened):
        for key in list(entries):
            try:
                known_id(key, catalog)
            except Refusal:
                del entries[key]
    target = root / PROJECT
    if manifest_stale(root, state):
        imported = answers_from(load_json(target, msg("no_project", target)), catalog)
        for key in set(state.get("manifestKeys", [])) - set(imported):
            answers.pop(key, None)
        for key, value in imported.items():
            if answers.get(key, {}).get("value") != value:
                answers[key] = {"value": value, "by": "owner"}
    for key, value in packed_decisions().items():
        if key not in answers and key not in reopened:
            answers[key] = {"value": value, "by": PACKED}
    if change:
        reopen_all(answers, reopened)
        alone.clear()
    if defaults:
        for key in list(reopened):
            value = reopened.pop(key)
            if key not in alone:
                answers.setdefault(key, {"value": value, "by": "owner"})
        alone.clear()
    summaries = {key: given.pop(key) for key in SUMMARIES if key in given}
    for key, value in given.items():
        known_id(key, catalog)
        answers[key] = {"raw": value, "by": "owner"}
        reopened.pop(key, None)
        alone.discard(key)
    tree = Tree(root)
    pending = owner_question(catalog, tree, answers, set(given), reopened, summaries, alone, strict, defaults)
    if summaries:
        raise Refusal(msg("configure_summary_unused", ", ".join(sorted(summaries))))
    session = Session(catalog, tree, answers, set(given), defaults=defaults, reopened=reopened, strict=strict)
    project = None
    if pending is None:
        try:
            project = configured(session)
        except Pending as stop:
            pending = stop.question
    problems = []
    if project is not None:
        text = json_text(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8", newline="\n")
        state["manifest"] = sha256(text.encode("utf-8"))
        state["manifestKeys"] = sorted(answers_from(project, catalog))
        problems = validate(project, root, catalog)
    state["alone"] = sorted(alone & set(reopened))
    write_answers(root, state)
    return pending, problems, session.answered
