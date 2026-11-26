from __future__ import annotations

import importlib
import json
import os
import sys

MANIFEST = "project/manifest.json"
LEGACY_MANIFEST = "project.json"
_CACHE: dict[str, dict] = {}
_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def root() -> str:
    return _ROOT


def set_root(path: str) -> None:
    global _ROOT
    _ROOT = os.path.abspath(path)
    _CACHE.clear()


def layer_manifest(present) -> str:
    return next((name for name in (MANIFEST, LEGACY_MANIFEST) if present(name)), MANIFEST)


def _data(file: str) -> dict:
    if file in _CACHE:
        return _CACHE[file]
    try:
        with open(os.path.join(root(), ".harness", file), encoding="utf-8") as handle:
            loaded = json.load(handle)
        _CACHE[file] = loaded if isinstance(loaded, dict) else {}
    except Exception:
        _CACHE[file] = {}
    return _CACHE[file]


def _walk(path: str, file: str):
    node = _data(file)
    for step in [s.strip('"') for s in path.split(".") if s]:
        if step.endswith("[]"):
            node = node.get(step[:-2]) if isinstance(node, dict) else None
            if not isinstance(node, list):
                return None
            return node
        if not isinstance(node, dict) or step not in node:
            return None
        node = node[step]
    return node


def get(path: str, default, file: str = MANIFEST):
    value = _walk(path, file)
    if value is None or isinstance(value, (list, dict)):
        return default
    return value


def values(path: str, file: str = MANIFEST):
    if "[]." in path:
        head, field = path.split("[].", 1)
        items = _walk(head + "[]", file)
        if not isinstance(items, list):
            return None
        return [i.get(field) for i in items if isinstance(i, dict) and i.get(field) is not None]
    node = _walk(path, file)
    if node is None:
        return None
    return node if isinstance(node, list) else [node]


def get_list(path: str, default, file: str = MANIFEST):
    if "[]." in path:
        return values(path, file) or default
    value = _walk(path, file)
    if isinstance(value, list):
        return value or default
    return default


def get_record(path: str, default, file: str = MANIFEST):
    value = _walk(path, file)
    return value if isinstance(value, dict) else default


def get_map(path: str, default, file: str = MANIFEST):
    value = get_record(path, None, file)
    if value is not None and all(isinstance(item, dict) for item in value.values()):
        return value
    return default


def get_records(path: str, default, file: str = MANIFEST):
    value = _walk(path, file)
    if isinstance(value, list) and all(isinstance(i, dict) for i in value):
        return value or default
    return default


APP_ROLES = ("format", "lint", "analyze", "architecture", "test")


def app_entries(app: dict) -> dict[str, dict]:
    entries = app.get("entries") if isinstance(app.get("entries"), dict) else {}
    folder = str(app.get("path") or ".")
    return {role: {"command": entry["command"].strip(), "cwd": str(entry.get("cwd") or folder)}
            for role in APP_ROLES if isinstance(entry := entries.get(role), dict)
            and isinstance(entry.get("command"), str) and entry["command"].strip()}


def npm_scripts(folder: str) -> dict:
    try:
        with open(os.path.join(_ROOT, folder, "package.json"), encoding="utf-8") as handle:
            scripts = json.load(handle).get("scripts")
    except (OSError, ValueError, AttributeError):
        return {}
    return scripts if isinstance(scripts, dict) else {}


def check_links(command: str, scripts: dict, seen: frozenset[str] = frozenset()) -> set[str]:
    links = {part.strip() for part in command.split("&&") if part.strip()}
    for link in list(links):
        script = link.removeprefix("npm run ").strip() if link.startswith("npm run ") else ""
        if isinstance(scripts.get(script), str) and script not in seen:
            links |= check_links(scripts[script], scripts, seen | {script})
    return links


def own_entries(app: dict) -> dict[str, dict]:
    folder = str(app.get("path") or ".")
    links = check_links(str(app.get("check") or ""), npm_scripts(folder))
    own: dict[str, dict] = {}
    for role, entry in app_entries(app).items():
        if (entry["command"] not in links or entry["cwd"] != folder) and entry not in own.values():
            own[role] = entry
    return own


def unset_roles(app: dict) -> list[str]:
    waived = isinstance(app.get("architectureWaiver"), str) and bool(app["architectureWaiver"].strip())
    entries = app_entries(app)
    return [role for role in APP_ROLES if role not in entries and not (role == "architecture" and waived)]


def rule_records(methodology: dict, project: dict) -> list[dict]:
    own = project.get("rules") if isinstance(project.get("rules"), dict) else {}
    disabled = {entry.get("id"): entry for entry in own.get("disabled", []) if isinstance(entry, dict)}
    records = []
    for record in methodology.get("modules", []):
        if not isinstance(record, dict):
            continue
        if record.get("id") in disabled:
            entry = disabled[record["id"]]
            record = {key: value for key, value in record.items() if key != "disabledBy"}
            record.update(enabled=False, disabledReason=entry.get("reason", ""), replacement=entry.get("replacement", ""))
        records.append(record)
    return records + [record for record in own.get("own", []) if isinstance(record, dict)]


def rules() -> list[dict]:
    return rule_records(_data("methodology.json"), _data(MANIFEST))


def module_on(entry: object) -> bool:
    return isinstance(entry, dict) and "off" not in entry


def entry_extensions(entry: object) -> dict[str, dict]:
    if not module_on(entry) or not isinstance(entry.get("extensions"), dict):
        return {}
    return {name: settings if isinstance(settings, dict) else {} for name, settings in entry["extensions"].items()}


def extensions(module: str) -> dict[str, dict]:
    return entry_extensions(_walk(f"modules.{module}", MANIFEST))


def module_paths() -> list[str]:
    entries = _walk("modules", MANIFEST)
    if not isinstance(entries, dict):
        return []
    named = [name for name, entry in entries.items() if module_on(entry)]
    return [path for name in named for path in (name, *(f"{name}/{extension}" for extension in extensions(name)))]


def _settings(path: str) -> tuple[dict, dict] | None:
    name, _, extension = path.partition("/")
    entry = _walk(f"modules.{name}", MANIFEST)
    if not module_on(entry):
        return None
    spec = (_data("state/modules.json").get("modules") or {}).get(name) or {}
    if extension:
        values = extensions(name).get(extension)
        spec = (spec.get("extensions") or {}).get(extension) or {}
    else:
        values = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
    return (values, spec) if values is not None else None


def _known_extensions(module: str) -> dict:
    return ((_data("state/modules.json").get("modules") or {}).get(module) or {}).get("extensions") or {}


def extension_value(module: str, field: str) -> str:
    known = _known_extensions(module)
    return next((str(known[name][field]) for name in extensions(module) if field in (known.get(name) or {})), "")


def adapter(module: str):
    known = _known_extensions(module)
    for name, settings in extensions(module).items():
        source, _, cls = ((known.get(name) or {}).get("adapter") or "").rpartition(".")
        if source:
            return getattr(importlib.import_module(source), cls)(settings)
    return None


def _reference(spec: dict, value) -> str:
    sources = spec.get("sources", ["literal"])
    if isinstance(value, dict) and "secret" in sources and set(value) == {"secret"}:
        return "secret"
    if isinstance(value, dict) and "env" in sources and "env" in value and set(value) <= {"env", "default"}:
        return "env"
    return ""


_SLOTS = {"secret": "SECRET", "env": "VAR"}


def environ(name: str, kind: str = "secret") -> str:
    if os.environ.get(name):
        return os.environ[name]
    prefix = _SLOTS[kind]
    for variable, value in os.environ.items():
        if variable.startswith(f"{prefix}_NAME_") and value == name:
            return os.environ.get(f"{prefix}_VALUE_{variable[len(prefix) + 6:]}", "")
    return ""


def setting(path: str, key: str, default=None):
    found = _settings(path)
    if found is None:
        return default
    values, entry = found
    spec = (entry.get("settings") or {}).get(key) or {}
    value = values.get(key, spec.get("default"))
    kind = _reference(spec, value)
    if kind == "secret":
        return environ(str(value["secret"])) or default
    if kind == "env":
        return environ(str(value["env"]), "env") or value.get("default", default)
    return default if value is None else value


def references(path: str, kind: str, record: str | None = None) -> list[str]:
    found = _settings(path)
    if found is None:
        return []
    values, entry = found
    names = []
    for key, spec in (entry.get("settings") or {}).items():
        value = values.get(key, spec.get("default"))
        if _reference(spec, value) == kind:
            names.append(str(value[kind]))
        elif kind == "secret" and spec.get("shape") == "records" and isinstance(value, list):
            fields = (spec.get("fields") or {}).get("secrets") or []
            names += [item[field] for item in value if isinstance(item, dict) and record in (None, item.get("name"))
                      for field in fields if isinstance(item.get(field), str) and item[field]]
    return names


def secrets(path: str) -> list[str]:
    found = _settings(path)
    if found is None:
        return []
    values, entry = found
    names = [str(name) for name in entry.get("secrets") or []]
    for key, spec in (entry.get("settings") or {}).items():
        chosen = spec.get("secrets") if isinstance(spec.get("secrets"), dict) else {}
        names += [str(name) for name in chosen.get(str(values.get(key, spec.get("default")))) or []]
    for forge in extensions("forge"):
        names += [str(name) for name in (entry.get("forgeSecrets") or {}).get(forge) or []]
    return list(dict.fromkeys(names + references(path, "secret")))


def secrets_use(path: str) -> str:
    found = _settings(path)
    name = path.partition("/")[0]
    module = (_data("state/modules.json").get("modules") or {}).get(name) or {}
    return str((found[1] if found else {}).get("secretsUse") or module.get("secretsUse") or "")


def slot_names(paths: list[str]) -> tuple[list[str], list[str]]:
    wanted = [(path, record if separator else None)
              for path, separator, record in (item.partition(":") for item in paths)]
    chosen = [(enabled, record) for enabled in module_paths() for path, record in wanted
              if enabled == path or "/" not in path and enabled.startswith(path + "/")]
    found = {kind: [name for path, record in chosen for name in references(path, kind, record)] for kind in _SLOTS}
    return list(dict.fromkeys(found["secret"])), list(dict.fromkeys(found["env"]))


def _print_slots(argv: list[str]) -> int:
    import argparse

    from platform_messages import msg

    parser = argparse.ArgumentParser(prog="manifest.py --secret-slots")
    parser.add_argument("paths", nargs="+", metavar="ПУТЬ[:ЗАПИСЬ]")
    parser.add_argument("--slots", type=int, default=3, help="число слотов каждого вида в файле CI")
    args = parser.parse_args(argv)
    names, variables = slot_names(args.paths)
    for kind, found in (("secret", names), ("var", variables)):
        if len(found) > args.slots:
            print(msg("manifest_slots_over", kind, len(found), args.slots, ", ".join(found)), file=sys.stderr)
            return 1
    for kind, found in (("secret", names), ("var", variables)):
        for number, name in enumerate(found, 1):
            print(f"{kind}-{number}={name}")
    return 0


if __name__ == "__main__" and sys.argv[1:2] == ["--secret-slots"]:
    raise SystemExit(_print_slots(sys.argv[2:]))

if __name__ == "__main__":
    _found = values(sys.argv[2], sys.argv[1])
    if not _found:
        raise SystemExit(1)
    for _item in _found:
        if _item is not None:
            print(_item if not isinstance(_item, bool) else str(_item).lower())
