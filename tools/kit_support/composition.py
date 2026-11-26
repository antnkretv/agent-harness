from __future__ import annotations

import json
from collections import Counter
import os
import re

from installer.catalog import (
    catalog_setting,
    condition_path,
    file_modules,
    lookup,
    missing_path,
    owner,
    EXPLICIT_PLACES,
    pack_skills,
    setting_condition,
    wanted,
)
from installer.documents import block_of, entry_markers, markers
from installer.render import fragment_folder
from installer.verify import twice
from installer.resources import (
    PROJECT_BLOCK,
    AREA_MAP,
    CONDITION,
    GUIDES,
    METHODOLOGY,
    PLACEHOLDER,
    POSIX_MODES,
    SEED_FOLDERS,
    SERVICE_AREAS,
    SIDES,
    SKILLS,
    SETTINGS_DEFAULTS,
    STACKS_WITH_ADAPTER,
)

from .catalog_checks import catalog_paths, catalog_problems, setting_holders
from .context import KIT, REFERENCE, SHIPPED, catalog, msg
from .source import git, tree_files


def composition(kit: dict) -> list[str]:
    problems = []
    files = tree_files()
    declared = {f"payload/{path}" for path, meta in kit["files"].items() if not meta.get("compiled")}
    present = {path for path in files if path.startswith("payload/")}
    compiled = {f"payload/{path}" for path, meta in kit["files"].items() if meta.get("compiled")}
    problems += [msg("kit_compiled_shipped", path) for path in sorted(present & compiled)]
    problems += [msg("kit_undeclared", path) for path in sorted(present - declared - compiled)]
    problems += [msg("kit_missing", path) for path in sorted(declared - present)]
    modules = catalog()
    packs = pack_skills(modules)
    problems += catalog_problems(modules)
    rules = json.loads((KIT / "payload" / METHODOLOGY).read_text(encoding="utf-8"))
    problems += [msg("kit_rule_twice", METHODOLOGY, name) for name in twice(rules)]
    paths = catalog_paths(modules)
    sided = sorted(path.split("/", 1)[1] for path in paths if path.startswith("stack/"))
    if sided != sorted(STACKS_WITH_ADAPTER):
        problems.append(msg("kit_stacks_adapter", ", ".join(sided), ", ".join(sorted(STACKS_WITH_ADAPTER))))
    for path, meta in kit["files"].items():
        for module in file_modules(meta) + meta.get("also", []):
            if module not in paths:
                problems.append(msg("kit_module_unknown", path, module))
        if meta.get("role") not in ("managed", "seed", "block", "json"):
            problems.append(msg("kit_role_unknown", path, meta.get("role")))
        if "place" in meta and (meta.get("role", "managed") != "managed" or meta["place"] not in EXPLICIT_PLACES):
            problems.append(msg("kit_place_unknown", path, meta["place"]))
        if "at" in meta:
            extensions = modules["modules"].get(owner(meta), {}).get("extensions", {})
            if not extensions or any(meta["at"] not in spec for spec in extensions.values()):
                problems.append(msg("kit_at_unknown", path, meta["at"]))
    owned: dict[str, set[str]] = {}
    for path, meta in kit["files"].items():
        if owner(meta) in packs:
            folder = path.split("/")[2] if path.startswith(SKILLS + "/") else path
            owned.setdefault(owner(meta), set()).add(folder)
    for pack, names in sorted(packs.items()):
        if owned.get(pack, set()) != set(names):
            problems.append(msg("kit_catalog_skills", pack, ", ".join(sorted(names)), ", ".join(sorted(owned.get(pack, set()))) or "-"))
    # Модуль правила с гейтом-файлом - модуль этого файла: второе объявление разошлось бы с ним молча.
    for record in rules["modules"]:
        if record.get("gate") in kit["files"]:
            if "module" in record:
                problems.append(msg("kit_rule_module_twice", record["id"], record["gate"]))
        elif record.get("module") not in paths:
            problems.append(msg("kit_rule_module_unknown", record["id"], record.get("module")))
    folders = {fragment_folder(path) for path in paths}
    for path in files:
        if path.startswith("fragments/"):
            folder, target = path.split("/", 2)[1:]
            if folder not in folders or target not in kit["files"]:
                problems.append(msg("kit_fragment_orphan", path))
    for name in SHIPPED + GUIDES:
        if not (KIT / name).is_file():
            problems.append(msg("kit_missing", name))
    # Условие инструкции установки называет путь каталога: опечатка молча сняла бы участок из каждого архива.
    for name in GUIDES:
        template = (KIT / name).read_text(encoding="utf-8") if (KIT / name).is_file() else ""
        problems += condition_problems(name, template, paths)
    # Режим в составе сверяется с индексом git: архив и распаковка его не переносят, установщик берет его из kit.json.
    staged = {}
    for line in git("ls-files", "-s", "-z", "--", "payload").decode("utf-8").split("\0"):
        if line:
            staged[line.split("\t", 1)[1].removeprefix("payload/")] = line.startswith("100755")
    for path, meta in kit["files"].items():
        source = KIT / "payload" / path
        if not source.is_file():
            continue
        executable = staged.get(path, os.access(source, os.X_OK) if POSIX_MODES else bool(meta.get("executable")))
        if bool(meta.get("executable")) != executable:
            problems.append(msg("kit_exec_flag", path, bool(meta.get("executable")), executable))
        text = source.read_text(encoding="utf-8", errors="replace")
        templated = bool(PLACEHOLDER.search(text) or CONDITION.search(text)) and owner(meta) not in packs
        if templated != bool(meta.get("render")):
            problems.append(msg("kit_render_flag", path, templated))
        if templated:
            problems += condition_problems(path, text, paths)
        if meta.get("role") == "block":
            for begin, end in (entry_markers(path, meta), markers(path, PROJECT_BLOCK)):
                if block_of(text, begin, end) is None:
                    problems.append(msg("kit_block_markers", path, begin))
    problems += notification_problems(kit, modules)
    problems += adapter_problems(kit, modules)
    problems += wiring_problems(kit, files, paths)
    problems += reference_problems(kit, modules)
    problems += inventory_problems(kit)
    problems += settings_problems()
    service = sorted(json.loads((KIT / "payload" / AREA_MAP).read_text(encoding="utf-8"))["areas"])
    if service != sorted(SERVICE_AREAS):
        problems.append(msg("kit_service_areas", ", ".join(service), ", ".join(SERVICE_AREAS)))
    for item in kit.get("dependencies", []):
        if item.get("pinSetting"):
            defaults = {"agents": {key: meta["default"] for key, meta in modules["agents"].items()}}
            pinned = lookup(defaults, item["pinSetting"])
            if pinned != item.get("pinned"):
                problems.append(msg("kit_pin_drift", item["name"], item.get("pinned"), item["pinSetting"], pinned or "-"))
        if item.get("pinFile"):
            pinned = re.search(item["pinPattern"], (KIT / "payload" / item["pinFile"]).read_text(encoding="utf-8"))
            if not pinned or pinned.group(1) != item.get("pinned"):
                problems.append(msg("kit_pin_drift", item["name"], item.get("pinned"), item["pinFile"],
                                    pinned.group(1) if pinned else "-"))
    return problems


MODULE_PATH = r"[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)?"


NOTIFICATIONS = "notifications"
CHANNELS = ".harness/notify/channels/"
RELEASE = ".github/workflows/release.yml"
SECRET_SLOT = re.compile(r"secrets\[steps\.[\w-]+\.outputs\.secret-(\d+)\]")
WIRED = re.compile(r"((?:\.claude|\.codex|\.cursor|\.harness)/[\w./-]+\.(?:py|sh|ps1))")
AGENT_DIRS = (".claude/", ".cursor/", ".codex/")


def strings(node):
    """Строки JSON на любой глубине: команду хука каждый агент кладет в свое поле."""
    if isinstance(node, str):
        yield node
    for value in node.values() if isinstance(node, dict) else node if isinstance(node, list) else ():
        yield from strings(value)


def condition_paths(*paths: str) -> frozenset[str]:
    """Включенные пути наименьшей установки, где условие держится: путь, его модуль, у стороны - stack/."""
    found = {"core"}
    for path in paths:
        name, _, extension = path.partition("/")
        found |= {path, name} | ({f"stack/{extension}"} if name in SIDES and extension else set())
    return frozenset(found)


def condition_problems(where: str, text: str, paths: frozenset[str]) -> list[str]:
    """Условие шаблона называет путь каталога, а setting: - настройку выбора и одно из ее значений: опечатка
    молча сняла бы участок из каждой установки."""
    problems = []
    for _, condition, _ in CONDITION.findall(text):
        kind, _, value = condition.partition(":")
        path = condition_path(kind, value)
        if kind == "setting":
            path, key, expected = setting_condition(value)
            if expected not in catalog_setting(path, key).get("values", {}):
                problems.append(msg("kit_condition_setting", where, condition))
        elif path is not None and path not in paths:
            problems.append(msg("kit_module_unknown", where, condition))
    return problems


def wiring_problems(kit: dict, tree: list[str], paths: frozenset[str]) -> list[str]:
    """Конфиг агента и его фрагмент вызывают только скрипты, которые ставятся при том же условии: хук без файла
    падает на каждом старте сессии или вызове."""
    files, problems = kit["files"], []
    folders = {fragment_folder(path): path for path in paths}
    hosts = [(f"payload/{path}", path, ()) for path in files if path.startswith(AGENT_DIRS) and path.endswith(".json")]
    for path in tree:
        _, folder, target = (path.split("/", 2) + ["", ""])[:3]
        if path.startswith("fragments/") and target.startswith(AGENT_DIRS) and target.endswith(".json") and folder in folders:
            hosts.append((path, target, (folders[folder],)))
    for source, host, extra in hosts:
        meta = files.get(host)
        if meta is None:
            continue
        text = "\n".join(strings(json.loads((KIT / source).read_text(encoding="utf-8")))).replace("\\", "/")
        wired = sorted(set(WIRED.findall(text)) & set(files))
        for module in file_modules(meta):
            enabled = condition_paths(module, *extra, *meta.get("also", []))
            for script in wired:
                if (needed := missing_path(files[script], enabled)) is not None:
                    problems.append(msg("kit_wiring_absent", source, module, script, needed))
    return problems


def adapter_problems(kit: dict, modules: dict) -> list[str]:
    """Поле adapter расширения называет класс в .harness/lib комплекта, который ставится с этим расширением. У
    модуля с адаптерами его объявляет каждое расширение: иначе выбранная технология молча дала бы пустой интерфейс."""
    problems = []
    for name, spec in modules["modules"].items():
        extensions = spec.get("extensions", {})
        declared = {extension: meta["adapter"] for extension, meta in extensions.items() if "adapter" in meta}
        if declared and set(declared) != set(extensions):
            problems.append(msg("kit_adapter_missing", name, ", ".join(sorted(set(extensions) - set(declared)))))
        for extension, named in sorted(declared.items()):
            source, _, cls = named.rpartition(".")
            path = f".harness/lib/{source}.py"
            meta = kit["files"].get(path)
            shipped = meta is not None and wanted(meta, condition_paths(f"{name}/{extension}"))
            text = (KIT / "payload" / path).read_text(encoding="utf-8") if shipped else ""
            if not re.search(rf"^class {re.escape(cls)}\b", text, re.M):
                problems.append(msg("kit_adapter_broken", f"{name}/{extension}", named, path))
    return problems


def notification_problems(kit: dict, modules: dict) -> list[str]:
    """Канал уведомлений - расширение модуля notifications: у каждого расширения каталога есть исполняемый адаптер
    channels/<имя>.sh и политика onFailure, файлы каталога каналов принадлежат своему расширению, а релиз передает
    рассылке столько секретов, сколько могут назвать каналы каталога."""
    problems = []
    channels = modules["modules"].get(NOTIFICATIONS, {}).get("extensions", {})
    for name, meta in sorted(channels.items()):
        adapter = kit["files"].get(f"{CHANNELS}{name}.sh", {})
        if adapter.get("module") != f"{NOTIFICATIONS}/{name}" or not adapter.get("executable"):
            problems.append(msg("kit_notify_adapter", name, f"{CHANNELS}{name}.sh"))
        if "onFailure" not in meta.get("settings", {}):
            problems.append(msg("kit_notify_policy", name))
    for path, meta in sorted(kit["files"].items()):
        channel = path[len(CHANNELS):].split(".", 1)[0] if path.startswith(CHANNELS) else ""
        if channel and meta.get("module") != f"{NOTIFICATIONS}/{channel}":
            problems.append(msg("kit_notify_foreign", path))
    needed = sum("secret" in setting.get("sources", []) for meta in channels.values()
                 for setting in meta.get("settings", {}).values())
    slots = len(set(SECRET_SLOT.findall((KIT / "payload" / RELEASE).read_text(encoding="utf-8"))))
    if needed > slots:
        problems.append(msg("kit_notify_slots", RELEASE, slots, needed))
    return problems


REFERENCE_MODULE = re.compile(r"^### `([a-z][a-z0-9-]*)`$")
REFERENCE_ROW = re.compile(rf"^\| `({MODULE_PATH}(?:\.[a-zA-Z]+)?)` \|(.*)\|$")


def catalog_items(modules: dict) -> dict[str, tuple[str, str] | None]:
    """Модули, расширения и настройки каталога; у настройки - вид и форма."""
    items: dict[str, tuple[str, str] | None] = {}
    for path, settings in setting_holders(modules):
        items[path] = None
        items |= {f"{path}.{key}": (setting["kind"], setting["shape"]) for key, setting in settings.items()}
    return items


def item_module(item: str) -> str:
    return item.split(".")[0].split("/")[0]


def reference_problems(kit: dict, modules: dict) -> list[str]:
    """Справочник параметров называет каждый модуль, расширение и настройку каталога в разделе своего модуля, у
    настройки - вид и форму каталога."""
    expected = catalog_items(modules)
    documented: dict[str, tuple[str, tuple[str, ...]]] = {}
    problems, section = [], None
    for line in (KIT / "payload" / REFERENCE).read_text(encoding="utf-8").splitlines():
        if line.startswith(("## ", "### ")):
            heading = REFERENCE_MODULE.match(line)
            section = heading.group(1) if heading else None
            item, cells = section, ()
        elif section and (row := REFERENCE_ROW.match(line)) and ("/" in row.group(1) or "." in row.group(1)):
            item, cells = row.group(1), tuple(cell.strip().strip("`") for cell in row.group(2).split("|")[:2])
        else:
            continue
        if item in documented:
            problems.append(msg("kit_reference_twice", item))
        if item:
            documented[item] = (section, cells)
    for item, (place, cells) in sorted(documented.items()):
        if item not in expected:
            problems.append(msg("kit_reference_stale", item))
        elif place != item_module(item):
            problems.append(msg("kit_reference_place", item, place, item_module(item)))
        elif expected[item] and cells != expected[item]:
            problems.append(msg("kit_reference_kind", item, " ".join(cells), " ".join(expected[item])))
    problems += [msg("kit_reference_missing", item, item_module(item)) for item in sorted(set(expected) - set(documented))]
    return problems


SETTING_LINE = re.compile(r"^(?:\{\{#if module:([\w/-]+)\}\})?([A-Z][A-Z0-9_]*)=", re.M)
SETTING_ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \| `?([^`|]+?)`? \|", re.M)


def settings_problems() -> list[str]:
    shipped = {name: module or msg("kit_settings_core") for module, name in SETTING_LINE.findall(
        (KIT / "payload" / SETTINGS_DEFAULTS).read_text(encoding="utf-8").replace("{{/if}}", ""))}
    text = (KIT / "payload" / REFERENCE).read_text(encoding="utf-8")
    section = text.split(msg("kit_settings_section"), 1)[-1].split("\n## ", 1)[0]
    documented = dict(SETTING_ROW.findall(section))
    if documented == shipped:
        return []
    return [msg("kit_settings_table", SETTINGS_DEFAULTS, ", ".join(f"{name} ({module})" for name, module in shipped.items()))]


INVENTORY_ROW = re.compile(r"^\| `([^`]+)` \| (\d+) \| (\d+) \| (\d+) \|$")
SEED_ROW = re.compile(r"^\| \[[^\]]+\]\(payload/([^)]+)\) \| `([^`]+)` \|$")
NOTE = re.compile(r"^### `([^`]+)`", re.M)


def inventory_problems(kit: dict) -> list[str]:
    """Счет файлов INVENTORY.md и таблица заготовок README.md - выводы из kit.json: правка состава без них
    разошлась бы с копией молча, поэтому сверка называет строку, которую ждет. Заметка MAINTAINING.md
    называет существующий файл одним разделом: переименование файла оставило бы ее без адресата.
    Строки README - ровно заготовки папок SEED_FOLDERS."""
    files = kit["files"]
    counts: dict[str, list[int]] = {}
    for meta in files.values():
        counts.setdefault(owner(meta), [0, 0])[0 if meta.get("origin") else 1] += 1
    listed = {path: ", ".join(file_modules(meta)) for path, meta in files.items() if meta.get("listed") == "README.md"}
    expected = {module: counts.get(module, [0, 0]) for module in sorted({owner(files[path]) for path in listed})}
    problems, rows = [], {}
    for line in (KIT / "INVENTORY.md").read_text(encoding="utf-8").splitlines():
        if row := INVENTORY_ROW.match(line):
            rows.setdefault(row.group(1), []).append([int(cell) for cell in row.groups()[1:]])
    for module in sorted(set(expected) | set(rows)):
        wanted = expected[module] + [sum(expected[module])] if module in expected else None
        if rows.get(module, []) != ([wanted] if wanted else []):
            problems.append(msg("kit_inventory_count", module, " | ".join(map(str, wanted)) if wanted else "-"))
    seeds = {}
    for line in (KIT / "README.md").read_text(encoding="utf-8").splitlines():
        if row := SEED_ROW.match(line):
            seeds.setdefault(row.group(1), []).append(row.group(2))
    for path in sorted(set(listed) | set(seeds)):
        if seeds.get(path, []) != ([listed[path]] if path in listed else []):
            problems.append(msg("kit_readme_seed", path, listed.get(path, "-")))
    folders = {path for path, meta in files.items() if meta.get("role") == "seed" and path.startswith(SEED_FOLDERS)}
    problems += [msg("kit_seed_listed", path) for path in sorted(folders ^ set(listed))]
    notes = Counter(NOTE.findall((KIT / "MAINTAINING.md").read_text(encoding="utf-8")))
    problems += [msg("kit_note_missing", path) for path in notes if not (KIT / path).exists()]
    problems += [msg("kit_note_twice", path) for path, count in sorted(notes.items()) if count > 1]
    return problems
