from __future__ import annotations

import functools
import json
import posixpath
import re
from pathlib import Path

from .resources import (
    BRANCH,
    BRANCH_CONVENTIONS,
    CATALOG,
    ENV_NAME,
    GIT_PREFIX,
    HERE,
    KNOWN_STACKS,
    NAME,
    PROJECT_NAME,
    SERVICE_AREAS,
    SIDES,
    STACKS_WITH_ADAPTER,
    app_roles,
    load_json,
    msg,
)


@functools.cache
def load_catalog() -> dict:
    return load_json(HERE / CATALOG, msg("no_catalog", HERE / CATALOG))


def lookup(data: dict, path: str, missing=None):
    node = data
    for step in path.split("."):
        if not isinstance(node, dict) or step not in node:
            return missing
        node = node[step]
    return node


def modules_of(project: dict) -> dict:
    found = project.get("modules")
    return found if isinstance(found, dict) else {}


def is_on(entry) -> bool:
    return isinstance(entry, dict) and "off" not in entry


def enabled(project: dict) -> frozenset[str]:
    paths = {"core"}
    for name, entry in modules_of(project).items():
        if not is_on(entry):
            continue
        paths.add(name)
        extensions = entry.get("extensions")
        for extension in extensions if isinstance(extensions, dict) else {}:
            paths.add(f"{name}/{extension}")
            if name in SIDES:
                paths.add(f"stack/{extension}")
    return frozenset(paths)


def pack_skills(catalog: dict) -> dict[str, list[str]]:
    return {f"{name}/{extension}": meta["skills"] for name, spec in catalog["modules"].items()
            for extension, meta in spec.get("extensions", {}).items() if meta.get("skills")}


def shipped_skills(catalog: dict) -> frozenset[str]:
    return frozenset(name for names in pack_skills(catalog).values() for name in names)


def setting(project: dict, path: str, key: str, missing=None):
    name, _, extension = path.partition("/")
    entry = modules_of(project).get(name)
    if not is_on(entry):
        return missing
    holder = (entry.get("extensions") or {}).get(extension) if extension else entry.get("settings")
    return holder.get(key, missing) if isinstance(holder, dict) else missing


def catalog_setting(path: str, key: str) -> dict:
    name, _, extension = path.partition("/")
    spec = load_catalog()["modules"].get(name, {})
    holder = spec.get("extensions", {}).get(extension, {}) if extension else spec
    return holder.get("settings", {}).get(key, {})


def setting_or_default(project: dict, path: str, key: str):
    return setting(project, path, key, catalog_setting(path, key).get("default"))


def condition_path(kind: str, value: str) -> str | None:
    return {"module": value, "stack": f"stack/{value}"}.get(kind)


def setting_condition(value: str) -> tuple[str, str, str]:
    place, _, expected = value.partition("=")
    path, _, key = place.rpartition(".")
    return path, key, expected


def holds(condition: str, project: dict) -> bool:
    if condition == "always":
        return True
    kind, _, value = condition.partition(":")
    path = condition_path(kind, value)
    if path is not None:
        return path in enabled(project)
    if kind == "setting":
        path, key, expected = setting_condition(value)
        return str(setting_or_default(project, path, key)) == expected
    return bool(lookup(project, condition))


def missing_path(meta: dict, paths: frozenset[str]) -> str | None:
    choices = file_modules(meta)
    if not any(path in paths for path in choices):
        return choices[0]
    return next((path for path in meta.get("also", []) if path not in paths), None)


def wanted(meta: dict, paths: frozenset[str]) -> bool:
    return missing_path(meta, paths) is None


EXPLICIT_PLACES = ("shell", "reference")


def shipped_bytes(kit_dir: Path, path: str, meta: dict) -> bytes:
    return b"" if meta.get("compiled") else (kit_dir / "payload" / path).read_bytes()


def at_targets(meta: dict, catalog: dict) -> dict[str, str]:
    module = owner(meta)
    return {f"{module}/{name}": spec[meta["at"]] for name, spec in catalog["modules"][module]["extensions"].items()}


def installed_path(path: str, meta: dict, paths: frozenset[str], catalog: dict) -> str:
    if "at" not in meta:
        return path
    return next(target for extension, target in at_targets(meta, catalog).items() if extension in paths)


def owner(meta: dict) -> str:
    return file_modules(meta)[0]


def file_modules(meta: dict) -> list[str]:
    module = meta.get("module", "core")
    return [module] if isinstance(module, str) else list(module)


def reference(spec: dict, value):
    sources = spec.get("sources", ["literal"])
    if isinstance(value, dict) and "secret" in sources and set(value) == {"secret"}:
        return "secret"
    if isinstance(value, dict) and "env" in sources and "env" in value and set(value) <= {"env", "default"}:
        return "env"
    return None


SETTING_SHAPES = ("text", "texts", "records", "choice", "choices", "flag", "object")


def shape_problems(where: str, spec: dict, value) -> list[str]:
    shape = spec["shape"]
    if shape == "text":
        good = isinstance(value, str) and (not spec.get("pattern") or re.fullmatch(spec["pattern"], value))
    elif shape == "texts":
        good = isinstance(value, str) or isinstance(value, list) and all(isinstance(item, str) for item in value)
    elif shape == "records":
        fields = spec.get("fields", {})
        known = set(fields.get("required", [])) | set(fields.get("optional", []))
        good = isinstance(value, list) and all(
            isinstance(item, dict) and set(fields.get("required", [])) <= set(item) and set(item) <= known
            and all(isinstance(field, str) for field in item.values()) for item in value)
    elif shape == "choice":
        good = isinstance(value, str) and value in spec.get("values", {})
    elif shape == "choices":
        good = isinstance(value, list) and all(isinstance(item, str) and item in spec.get("values", {}) for item in value)
    elif shape == "flag":
        good = isinstance(value, bool)
    else:
        good = isinstance(value, dict)
    return [] if good else [msg("param_setting_shape", where, shape_name(shape), json.dumps(value, ensure_ascii=False))]


def shape_name(shape: str) -> str:
    return {"text": msg("shape_text"), "texts": msg("shape_texts"), "records": msg("shape_records"),
            "choice": msg("shape_choice"), "choices": msg("shape_choices"), "flag": msg("shape_flag"),
            "object": msg("shape_object")}[shape]


def setting_problems(where: str, specs: dict, values) -> list[str]:
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return [msg("param_settings_object", where)]
    problems = [msg("param_setting_unknown", f"{where}.{key}") for key in sorted(set(values) - set(specs))]
    module = re.split(r"[/.]", where, maxsplit=1)[0]
    for key, spec in specs.items():
        place = f"{where}.{key}"
        if key not in values:
            if spec.get("required"):
                problems.append(msg("param_fact_missing", place, spec.get("rule", spec.get("title", "")), module))
            continue
        value = values[key]
        kind = reference(spec, value)
        if kind == "secret":
            if not ENV_NAME.fullmatch(str(value["secret"])):
                problems.append(msg("param_secret_name", place, value["secret"]))
            continue
        if kind == "env":
            if not ENV_NAME.fullmatch(str(value["env"])):
                problems.append(msg("param_env_name", place, value["env"]))
            if "default" in value:
                problems += shape_problems(place, spec, value["default"])
            continue
        if "literal" not in spec.get("sources", ["literal"]):
            problems.append(msg("param_literal_forbidden", place, ", ".join(spec["sources"])))
            continue
        problems += shape_problems(place, spec, value)
        if spec.get("required") and value in ("", [], {}):
            problems.append(msg("param_fact_missing", place, spec.get("rule", spec.get("title", "")), module))
    return problems


def count_problems(where: str, bounds: dict, count: int) -> list[str]:
    low, high = bounds.get("min", 0), bounds.get("max")
    if count < low or high is not None and count > high:
        return [msg("param_extension_count", where, count, low, "-" if high is None else high)]
    return []


def module_problems(project: dict, catalog: dict) -> list[str]:
    problems = []
    specs = catalog.get("modules", {})
    found = project.get("modules", {})
    if not isinstance(found, dict):
        return [msg("param_modules_object")]
    paths = enabled(project)
    for name in sorted(set(found) - set(specs)):
        problems.append(msg("param_module_unknown", name, ", ".join(sorted(specs))))
    for name, spec in specs.items():
        entry = found.get(name)
        if entry is None or isinstance(entry, dict) and "off" in entry:
            if spec.get("required"):
                problems.append(msg("param_module_required", name))
            elif entry is not None and (set(entry) != {"off"} or not isinstance(entry["off"], str)):
                problems.append(msg("param_module_off", name))
            continue
        if not isinstance(entry, dict) or set(entry) - {"settings", "extensions"}:
            problems.append(msg("param_module_shape", name))
            continue
        extensions = entry.get("extensions", {})
        if not isinstance(extensions, dict):
            problems.append(msg("param_module_shape", name))
            continue
        known = spec.get("extensions", {})
        problems += [msg("param_extension_unknown", f"{name}/{extension}", ", ".join(sorted(known)) or "-")
                     for extension in sorted(set(extensions) - set(known))]
        if "slots" in spec:
            for slot, bounds in spec["slots"].items():
                chosen = sum(1 for extension in extensions if known.get(extension, {}).get("slot") == slot)
                problems += count_problems(f"{name}:{slot}", bounds, chosen)
        else:
            problems += count_problems(name, spec.get("select", {"max": 0}), len(extensions))
        for required in spec.get("requires", []):
            if required not in paths:
                problems.append(msg("param_module_requires", name, required))
        for wanted in spec.get("wants", []):
            owner = wanted.partition("/")[0]
            if wanted not in paths and not waived(project, catalog, owner):
                problems.append(msg("param_module_wants", name, wanted, owner))
        if spec.get("side") and spec["side"] not in paths:
            problems.append(msg("param_module_side", name, spec["side"]))
        if spec.get("stacks") and not any(f"stack/{stack}" in paths for stack in spec["stacks"]):
            problems.append(msg("param_module_stacks", name, ", ".join(spec["stacks"])))
        problems += setting_problems(name, spec.get("settings", {}), entry.get("settings"))
        problems += membership_problems(project, name, spec.get("settings", {}), entry.get("settings"))
        for extension, values in extensions.items():
            if extension not in known:
                continue
            for required in known[extension].get("requires", []):
                if required not in paths:
                    problems.append(msg("param_module_requires", f"{name}/{extension}", required))
            problems += setting_problems(f"{name}/{extension}", known[extension].get("settings", {}), values)
            problems += membership_problems(project, f"{name}/{extension}", known[extension].get("settings", {}), values)
    return problems


def waived(project: dict, catalog: dict, name: str) -> bool:
    entry = modules_of(project).get(name)
    return own_reason(catalog["modules"].get(name, {}), entry.get("off") if isinstance(entry, dict) else None)


def membership_problems(project: dict, where: str, specs: dict, values) -> list[str]:
    problems = []
    for key, spec in specs.items():
        value = values.get(key) if isinstance(values, dict) else None
        if "in" not in spec or not isinstance(value, str):
            continue
        names = listed_names(project, spec["in"])
        if value not in names:
            problems.append(msg("param_setting_not_listed", f"{where}.{key}", value, spec["in"], ", ".join(names) or "-"))
    return problems


def branch_problems(base: str, release: str) -> list[str]:
    return [msg("param_same_branches", base)] if base and base == release else []


def full_check(check) -> bool:
    return isinstance(check, str) and bool(check.strip()) and "TODO" not in check


def package_scripts(package: Path) -> dict | None:
    if not package.is_file():
        return None
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def check_problem(stack: str, check: str, scripts: dict | None) -> str:
    npm = check.startswith("npm run ")
    if stack != "node":
        return msg("answer_stack_check", stack) if npm else ""
    if not npm:
        return msg("answer_node_check")
    script = (check.split()[2:3] or [""])[0]
    return msg("answer_node_script", script) if scripts is not None and script not in scripts else ""


WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def windows_paths(node, where: str) -> list[str]:
    if isinstance(node, str):
        return [msg("param_windows_path", where, node)] if WINDOWS_PATH.match(node) else []
    if isinstance(node, dict):
        return [line for key, value in node.items() for line in windows_paths(value, f"{where}.{key}")]
    if isinstance(node, list):
        return [line for value in node for line in windows_paths(value, where)]
    return []


def validate(project: dict, root: Path, catalog: dict) -> list[str]:
    problems = [line for key in ("docs", "modules", "apps") for line in windows_paths(project.get(key), key)]

    def need(path: str, pattern: str = r".+") -> str:
        found = lookup(project, path)
        if not isinstance(found, str) or not re.fullmatch(pattern, found):
            problems.append(msg("param_invalid", path, pattern))
            return ""
        return found

    need("project.name", PROJECT_NAME)
    need("project.gitConfigPrefix", GIT_PREFIX)
    base, release = need("git.baseBranch", BRANCH), need("git.releaseBranch", BRANCH)
    problems += branch_problems(base, release)
    need("git.branchConvention", "|".join(BRANCH_CONVENTIONS))
    problems += module_problems(project, catalog)
    problems += setting_problems("agents", catalog["agents"], project.get("agents"))
    paths = enabled(project)
    named = lookup(project, "docs.vendoredSkills", [])
    for path, names in sorted(pack_skills(catalog).items()):
        missing = [name for name in names if name not in named] if path in paths and isinstance(named, list) else []
        if missing:
            problems.append(msg("param_vendored_skills", path, ", ".join(missing)))
    solution = setting(project, "backend/dotnet", "solution")
    if isinstance(solution, str) and solution and not (root / solution).is_file():
        problems.append(msg("param_solution", solution))
    apps = project.get("apps")
    if not isinstance(apps, list):
        problems.append(msg("param_apps"))
        apps = []
    seen = set()
    for app in apps:
        if not isinstance(app, dict):
            problems.append(msg("param_app_record", app))
            continue
        name, path, stack = app.get("id", ""), app.get("path", ""), app.get("stack", "")
        where = name or path or "?"
        if not isinstance(name, str) or not NAME.match(name) or name in seen:
            problems.append(msg("param_app_id", where))
        elif name in SERVICE_AREAS:
            problems.append(msg("param_app_id_reserved", where, ", ".join(SERVICE_AREAS)))
        seen.add(name)
        if not isinstance(path, str) or not path or not (root / path).is_dir():
            problems.append(msg("param_app_path", where, path))
        elif path != posixpath.normpath(path) or path.startswith("/"):
            problems.append(msg("param_app_path_form", where, path, posixpath.normpath(path).strip("/")))
        if stack not in KNOWN_STACKS:
            problems.append(msg("param_app_stack", where, stack, ", ".join(KNOWN_STACKS)))
        side = app.get("side", "")
        if side not in SIDES:
            problems.append(msg("param_app_side", where, side, ", ".join(SIDES)))
        elif side not in paths:
            problems.append(msg("param_app_side_off", where, side))
        elif stack in STACKS_WITH_ADAPTER and f"{side}/{stack}" not in paths:
            problems.append(msg("param_app_extension", where, f"{side}/{stack}"))
        check = app.get("check", "")
        package = root / path / "package.json" if isinstance(path, str) else None
        if not full_check(check):
            problems.append(msg("param_app_check", where))
        elif problem := check_problem(stack, check, package_scripts(package) if package else None):
            problems.append(msg("param_app_check_bad", where, problem))
        if stack == "node" and package:
            if not package.is_file():
                problems.append(msg("param_node_package", where, path))
            if app.get("architectureWaiver") is not None and not layerless(app):
                problems.append(msg("param_node_waiver", where))
        problems += entry_problems(app, where, root)
        entries = app.get("entries") if isinstance(app.get("entries"), dict) else {}
        if stack not in STACKS_WITH_ADAPTER and stack in KNOWN_STACKS and "architecture" not in entries and not layerless(app):
            problems.append(msg("param_architecture", where, stack))
    problems += [msg("param_section_unknown", key, ", ".join(SECTIONS)) for key in sorted(set(project) - set(SECTIONS))]
    rules = project.get("rules", {})
    if not isinstance(rules, dict) or set(rules) - {"disabled", "own"} or not all(
            isinstance(rules.get(key, []), list) and all(isinstance(entry, dict) for entry in rules.get(key, []))
            for key in ("disabled", "own")):
        problems.append(msg("param_rules_shape"))
    named = project.get("project") if isinstance(project.get("project"), dict) else {}
    problems += [msg("param_section_unknown", f"project.{key}", "name, gitConfigPrefix")
                 for key in sorted(set(named) - {"name", "gitConfigPrefix"})]
    return problems


SECTIONS = ("_", "project", "git", "docs", "agents", "apps", "modules", "rules")
def off_texts(catalog: dict, project: dict, path: str) -> tuple[str, str]:
    name, _, extension = path.partition("/")
    if name == "stack":
        return msg("off_stack", extension), msg("off_stack_replacement")
    off = catalog["modules"][name].get("off", {})
    entry = modules_of(project).get(name)
    if extension and is_on(entry):
        return msg("off_extension", extension, name), msg("off_extension_replacement")
    reason = entry["off"] if isinstance(entry, dict) and entry.get("off") else off.get("reason", msg("off_module", name))
    return reason, off["replacement"]


def inside(root: Path, value, present) -> bool:
    return isinstance(value, str) and bool(value) and value == posixpath.normpath(value) and not value.startswith("/") \
        and (root / value).resolve().is_relative_to(root.resolve()) and present(root / value)


def entries_shape(entries, roles: tuple[str, ...]) -> bool:
    return isinstance(entries, dict) and not set(entries) - set(roles) and all(
        isinstance(entry, dict) and set(entry) <= {"command", "cwd"} and full_check(entry.get("command"))
        for entry in entries.values())


def entry_problems(app: dict, where: str, root: Path) -> list[str]:
    entries, roles = app.get("entries", {}), app_roles()
    if not entries_shape(entries, roles):
        return [msg("param_app_entries", where, ", ".join(roles))]
    problems = []
    for role, entry in entries.items():
        cwd = entry.get("cwd", app.get("path"))
        if not inside(root, cwd, Path.is_dir):
            problems.append(msg("param_app_entry_cwd", where, role, cwd))
        elif entry["command"].startswith("npm run ") and (
                problem := check_problem("node", entry["command"], package_scripts(root / cwd / "package.json") or {})):
            problems.append(msg("param_app_entry_script", where, role, cwd, problem))
    if "architecture" in entries and layerless(app):
        problems.append(msg("param_app_entry_waiver", where))
    configs = app.get("configs", [])
    if not isinstance(configs, list) or not all(isinstance(path, str) and path for path in configs):
        problems.append(msg("param_app_configs", where))
    else:
        problems += [msg("param_app_config_missing", where, path) for path in configs if not inside(root, path, Path.is_file)]
    return problems


def layerless(app: dict) -> bool:
    waiver = app.get("architectureWaiver")
    return isinstance(waiver, str) and bool(waiver.strip())


def unmet(spec: dict, paths: frozenset[str]) -> list[str]:
    stacks = [f"stack/{stack}" for stack in spec.get("stacks", [])]
    needs = [spec["side"]] if spec.get("side") and spec["side"] not in paths else []
    needs += [] if any(stack in paths for stack in stacks) else stacks
    return needs + [required for required in spec.get("requires", []) if required not in paths]


def inapplicable(spec: dict) -> str:
    return msg("off_inapplicable", ", ".join(unmet(spec, frozenset())))


def own_reason(spec: dict, reason) -> bool:
    return isinstance(reason, str) and bool(reason.strip()) and reason not in (
        spec.get("off", {}).get("reason"), inapplicable(spec))


def shared_extensions(catalog: dict) -> set[str]:
    return set.intersection(*(set(catalog["modules"][side].get("extensions", {})) for side in SIDES))


def reference_parts(reference_path: str) -> tuple[str, str, str]:
    path, _, rest = reference_path.partition(".")
    key, _, field_name = rest.partition(".")
    return path, key, field_name


def listed_names(project: dict, reference_path: str) -> list[str]:
    path, key, field_name = reference_parts(reference_path)
    records = setting_or_default(project, path, key)
    if not isinstance(records, list):
        return []
    return [str(record[field_name]) for record in records if isinstance(record, dict) and field_name in record]
