from __future__ import annotations

import datetime
import functools
import json
import os
import posixpath
import re
import runpy
import tomllib
from pathlib import Path

from .catalog import (
    at_targets,
    enabled,
    holds,
    installed_path,
    is_on,
    layerless,
    load_catalog,
    lookup,
    missing_path,
    modules_of,
    off_texts,
    owner,
    setting,
    setting_or_default,
    shipped_bytes,
    wanted,
)
from .documents import json_indent, json_text, markers, merge_json
from .resources import (
    PROJECT_BLOCK,
    AREA_MAP,
    CONDITION,
    ENTRY,
    HASH_MARKERS,
    CATALOG,
    INSTALLED_CATALOG,
    Item,
    LEFTOVER,
    MARKDOWN_LINK,
    METHODOLOGY,
    PLACEHOLDER,
    Refusal,
    kit_root,
    layer_manifest,
    msg,
)


def markdown_cell(text: str) -> str:
    return text.replace("|", "\\|")


def runner_pools(project: dict) -> dict[str, str]:
    paths = enabled(project)
    known = load_catalog()["modules"]["runners"]["extensions"]
    chosen = next((name for name in known if f"runners/{name}" in paths), None) or next(
        (name for name, spec in known.items() if all(path in paths for path in spec.get("requires", []))), None)
    if chosen is None:
        return {}
    path, specs = f"runners/{chosen}", known[chosen].get("settings", {})
    pools = {kind: setting_or_default(project, path, kind) for kind in specs if kind != "variables"}
    variables = (setting_or_default(project, path, "variables") if "variables" in specs else None) or {}

    def shown(kind: str, pool) -> str:
        labels = json.dumps([pool] if isinstance(pool, str) else pool, ensure_ascii=False)
        name = variables.get(kind) if isinstance(variables, dict) else None
        return f"${{{{ fromJSON(vars.{name} || '{labels}') }}}}" if name else labels

    return {f"harness.runner.{kind}": shown(kind, pool) for kind, pool in pools.items()}


def entry_cell(app: dict) -> str:
    folder = app["path"]
    cells = [f"`{role}` `{markdown_cell(entry['command'])}`" + (f" в `{entry['cwd']}`" if entry["cwd"] != folder else "")
             for role, entry in layer_manifest()["app_entries"](app).items()]
    return ", ".join(cells) or "-"


def computed(kit: dict, project: dict) -> dict[str, str]:
    apps = [app for app in project.get("apps", []) if isinstance(app, dict)]
    areas = [f"| `{app['path'].rstrip('/')}/` | {markdown_cell(app.get('doc') or '-')} | "
             f"{markdown_cell(app.get('skill') or '-')} | `{markdown_cell(app.get('check', ''))}` |" for app in apps]
    profiles = [f"| `{app['path'].rstrip('/')}/` | `{markdown_cell(app.get('check', ''))}` |" for app in apps]
    entries = [f"| `{app['path'].rstrip('/')}/` | `{markdown_cell(app.get('check', ''))}` | {entry_cell(app)} |" for app in apps]
    links = ["[{0}](../{0})".format(app["doc"]) if app.get("doc") else "-" for app in apps]
    rows = [f"| `{app['path'].rstrip('/')}/` | {app.get('stack', '-')} | {link} | "
            f"`{markdown_cell(app.get('check', ''))}` |" for app, link in zip(apps, links)]
    return {
        "harness.kitVersion": kit["version"],
        "harness.areasTable": "\n".join(areas),
        "harness.profileRows": "\n".join(profiles),
        "harness.entryRows": "\n".join(entries),
        "harness.appRows": "\n".join(rows) or "| - | - | - | - |",
        "harness.installDate": datetime.date.today().isoformat(),
        **runner_pools(project),
        **{f"harness.extension.{name}": next(iter(entry["extensions"])) for name, entry in modules_of(project).items()
           if is_on(entry) and isinstance(entry.get("extensions"), dict) and len(entry["extensions"]) == 1},
    }


def render(text: str, project: dict, extra: dict[str, str], where: str) -> str:
    def section(match: re.Match[str]) -> str:
        then, _, otherwise = match.group(3).partition("{{else}}")
        return then if holds(match.group(2), project) == (match.group(1) == "if") else otherwise

    text = CONDITION.sub(section, text)
    left = LEFTOVER.search(text)
    if left:
        raise Refusal(msg("render_tag_left", where, left.group(0)))

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        found = extra.get(name, lookup(project, name))
        if found is None or isinstance(found, (dict, list)):
            raise Refusal(msg("param_missing", name, where))
        return str(found)

    return PLACEHOLDER.sub(value, text)


def rule_condition(record: dict, files: dict) -> dict:
    return files.get(record.get("gate"), record)


def disabled_modules(text: str, kit: dict, catalog: dict, project: dict) -> str:
    data = json.loads(text)
    paths = enabled(project)
    for module in data.get("modules", []):
        missing = missing_path(rule_condition(module, {**kit.get("absent", {}), **kit["files"]}), paths)
        if missing is None:
            continue
        module["enabled"] = False
        module["disabledReason"], module["replacement"] = off_texts(catalog, project, missing)
        module["disabledBy"] = missing
    absent = absent_paths(kit, paths, catalog)
    for step in data.get("processes", {}).get("review", {}).get("steps", []):
        prose = step.get("prose", {})
        if "except" in prose:
            prose["except"] = [path for path in prose["except"] if path not in absent]
    return json_text(data)


def app_prefix(path: str) -> str:
    return "" if path in ("", ".") else path.rstrip("/") + "/"


def app_areas(text: str, project: dict) -> str:
    data = json.loads(text)
    for app in project.get("apps", []):
        data["areas"].setdefault(app["id"], {"deploy": True, "prefixes": [app_prefix(app["path"])]})
    return json_text(data)


AGENT_SERVERS = {".mcp.json": "claude-code", ".cursor/mcp.json": "cursor", ".codex/config.toml": "codex"}
CLAUDE_SETTINGS = ".claude/settings.json"
TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")
LSP_BRIDGE = ".harness/provisioning/scripts/lsp-mcp.mjs"
LSP_SERVER = {"script": LSP_BRIDGE, "timeout": 300}
COMMAND_LAUNCHER = ".harness/provisioning/scripts/mcp-launch.mjs"
MCP_MODULE = "mcp"


def agent_servers(kit: dict, catalog: dict, project: dict, paths: frozenset[str]) -> dict[str, dict]:
    bridge = {**kit.get("absent", {}), **kit["files"]}[LSP_BRIDGE]
    servers = {"lsp": LSP_SERVER} if wanted(bridge, paths) else {}
    servers.update({extension: meta["server"] for extension, meta in catalog["modules"][MCP_MODULE]["extensions"].items()
                    if f"{MCP_MODULE}/{extension}" in paths})
    for record in setting_or_default(project, "stands/mcp-stand", "list") or []:
        servers[record["name"]] = {"url": record["url"]}
    return servers


def script_launcher(script: str) -> str:
    return ("import { execFileSync } from 'node:child_process'; import { pathToFileURL } from 'node:url'; "
            "const root = execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim(); "
            f"process.argv.splice(1, 0, root + '/{script}'); await import(pathToFileURL(process.argv[1]).href);")


def agent_server(agent: str, server: dict) -> dict:
    if "url" in server:
        return {"type": "http", "url": server["url"]} if agent == "claude-code" else {"url": server["url"]}
    script, words = server.get("script", COMMAND_LAUNCHER), []
    if "command" in server:
        words = [*server.get("ensure", []), "--", server["command"], *server["args"]]
    args = ["${workspaceFolder}/" + script, *words] if agent == "cursor" \
        else ["--input-type=module", "-e", script_launcher(script), *(["--", *words] if words else [])]
    found = {"command": "node", "args": args}
    if "timeout" in server and agent == "claude-code":
        found["timeout"] = server["timeout"] * 1000
    if "timeout" in server and agent == "codex":
        found |= {"startup_timeout_sec": server["timeout"], "tool_timeout_sec": server["timeout"]}
    return found


def claude_servers(servers: dict[str, dict]) -> dict:
    found = {"enabledMcpjsonServers": list(servers)}
    timeouts = [server["timeout"] for server in servers.values() if "timeout" in server]
    if timeouts:
        found["env"] = {"MCP_TIMEOUT": str(max(timeouts) * 1000)}
    return found


def server_clashes(current: str, block: str) -> list[tuple[str, str]]:
    try:
        mine = tomllib.loads(current).get("mcp_servers", {})
        kit = tomllib.loads(block).get("mcp_servers", {})
    except tomllib.TOMLDecodeError:
        return []
    clashes = []
    for name, server in mine.items():
        if name in kit:
            clashes.append((name, ""))
        elif isinstance(server, dict):
            other = next((other for other, theirs in kit.items() if isinstance(theirs, dict) and same_server(server, theirs)), None)
            if other:
                clashes.append((name, other))
    return clashes


def same_server(left: dict, right: dict) -> bool:
    if left.get("url"):
        return left.get("url") == right.get("url")
    mine = [left.get("command"), *(left.get("args") or [])]
    theirs = [right.get("command"), *(right.get("args") or [])]
    return bool(left.get("command")) and (mine == theirs or theirs[-len(mine) - 1:] == ["--", *mine])


def agent_config(path: str, text: str, servers: dict[str, dict]) -> str:
    agent = AGENT_SERVERS[path]
    if agent == "codex":
        lines = []
        for name, server in servers.items():
            key = name if TOML_KEY.fullmatch(name) else json.dumps(name)
            lines += [f"[mcp_servers.{key}]", *(f"{field} = {json.dumps(value, ensure_ascii=False)}"
                                                for field, value in agent_server(agent, server).items()), ""]
        end = HASH_MARKERS[1].format(id="core")
        return text.replace(end, "\n".join(lines) + end)
    data = json.loads(text)
    for name, server in servers.items():
        data["mcpServers"][name] = agent_server(agent, server)
    return json_text(data)


def relink(text: str, path: str, absent: set[str]) -> str:
    def link(match: re.Match[str]) -> str:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), match.group(2)))
        return match.group(1) if target in absent else match.group(0)
    return MARKDOWN_LINK.sub(link, text)


def absent_entries(kit: dict, paths: frozenset[str], catalog: dict) -> dict[str, dict]:
    found = {}
    for path, meta in kit["files"].items():
        if "at" in meta:
            kept = installed_path(path, meta, paths, catalog) if wanted(meta, paths) else None
            found.update({target: meta for target in at_targets(meta, catalog).values() if target != kept})
        elif not wanted(meta, paths):
            found[path] = meta
    return found


def absent_paths(kit: dict, paths: frozenset[str], catalog: dict) -> set[str]:
    return set(absent_entries(kit, paths, catalog)) | set(kit.get("absent", []))


def removed_seeds(kit: dict, lock: dict, root: Path) -> set[str]:
    return {path for path, entry in lock.get("files", {}).items()
            if entry.get("role") == "seed" and path.endswith(".md") and path in kit["files"] and not (root / path).exists()}


@functools.cache
def payload_scripts(kit_dir: Path):
    scripts = kit_dir / "payload" / ".harness" / "scripts"
    runpy.run_path(str(scripts / "script_paths.py"))
    import text_scan
    import text_templates

    return text_scan, text_templates


def absent_only(kit_dir: Path, kit: dict, paths: frozenset[str], read) -> set[str]:
    present, absent = set(), set()
    for path, meta in kit["files"].items():
        found = read(path, lambda: shipped_bytes(kit_dir, path, meta).decode("utf-8", errors="replace"))
        (present if wanted(meta, paths) else absent).update(found)
    return absent - present


def drop_keys(text: str, keys: set[str]) -> str:
    return ENTRY.sub(lambda found: "" if found.group(1) in keys else found.group(0), text)


def drop_variables(text: str, names: set[str]) -> str:
    data = json.loads(text)
    data["variables"] = {name: entry for name, entry in data["variables"].items() if name not in names}
    return json_text(data)


def registry_pruner(kit_dir: Path, kit: dict, paths: frozenset[str]):
    text_scan, templates = payload_scripts(kit_dir)
    keys = absent_only(kit_dir, kit, paths, lambda path, text: () if os.path.basename(path) == "messages.env"
                       or path.endswith(".md") else [key for key, _ in text_scan.uses(path, text())])
    variables = absent_only(kit_dir, kit, paths, lambda path, text: templates.names(text())
                            if path.startswith((templates.FOLDER, templates.PROMPTS)) and path.endswith(".md") else ())

    def prune(path: str, data: bytes) -> bytes:
        if keys and os.path.basename(path) == "messages.env":
            return drop_keys(data.decode("utf-8"), keys).encode("utf-8")
        if variables and path == templates.REGISTRY:
            return drop_variables(data.decode("utf-8"), variables).encode("utf-8")
        return data

    return prune


def unpruned(_path: str, data: bytes) -> bytes:
    return data


def fragment_folder(path: str) -> str:
    return path.replace("/", ".")


def plan(kit_dir: Path, kit: dict, catalog: dict, project: dict, root: Path, lock: dict) -> list[Item]:
    paths = enabled(project)
    extra = computed(kit, project)
    absent = absent_paths(kit, paths, catalog)
    prune = registry_pruner(kit_dir, kit, paths) if absent else unpruned
    absent |= removed_seeds(kit, lock, root)
    servers = agent_servers(kit, catalog, project, paths)
    items = []
    for path, meta in sorted(kit["files"].items()):
        if not wanted(meta, paths):
            continue
        data = shipped_bytes(kit_dir, path, meta)
        executable = bool(meta.get("executable"))
        if meta.get("render"):
            text = render(data.decode("utf-8"), project, extra, path).rstrip("\r\n")
            data = (text + "\n" if text else "").encode("utf-8")
        if path.endswith(".md") and absent:
            data = relink(data.decode("utf-8"), path, absent).encode("utf-8")
        data = prune(path, data)
        if path == METHODOLOGY:
            data = disabled_modules(data.decode("utf-8"), kit, catalog, project).encode("utf-8")
        if path == AREA_MAP:
            data = app_areas(data.decode("utf-8"), project).encode("utf-8")
        if path in AGENT_SERVERS:
            data = agent_config(path, data.decode("utf-8"), servers).encode("utf-8")
        if meta.get("role") == "json":
            merged = json.loads(data)
            for name in sorted(paths):
                fragment = kit_dir / "fragments" / fragment_folder(name) / path
                if fragment.is_file():
                    merged = merge_json(merged, json.loads(fragment.read_text(encoding="utf-8")))
            if path == CLAUDE_SETTINGS and servers:
                merged = merge_json(merged, claude_servers(servers))
            data = json_text(merged, json_indent(data)).encode("utf-8")
        destination = installed_path(path, meta, paths, catalog)
        items.append(Item(destination, meta.get("role", "managed"), owner(meta), data, executable,
                          meta.get("block", "core"), meta.get("extend", ""), meta.get("place", "")))
    items.append(Item(INSTALLED_CATALOG, "managed", "core", (kit_dir / CATALOG).read_bytes(), False))
    solution = setting(project, "backend/dotnet", "solution")
    if solution:
        items.append(dotnet_wiring(root, posixpath.dirname(solution)))
    compile_agent_permissions(kit_dir, project, items)
    return items


def compile_agent_permissions(kit_dir: Path, project: dict, items: list[Item]) -> None:
    scripts = kit_dir / "payload" / ".harness" / "scripts"
    runpy.run_path(str(scripts / "script_paths.py"))
    from agent_permissions import CLIENT_PATHS, compile_configs

    planned = {item.path: item for item in items}
    policy = json.loads(planned[METHODOLOGY].content)["policies"]["permissions"]
    clients = list(modules_of(project)["agents"]["extensions"])
    native_paths = [*CLIENT_PATHS.values(), ".cursor/hooks.json"]
    configs = {path: planned[path].content.decode("utf-8") for path in native_paths if path in planned}
    try:
        compiled = compile_configs(policy, clients, configs)
    except (ValueError, RuntimeError) as error:
        raise Refusal(str(error)) from error
    for path, text in compiled.items():
        planned[path].content = text.encode("utf-8")


DOTNET_TARGETS = "Directory.Build.targets"


def dotnet_wiring(root: Path, folder: str) -> Item:
    chain = [folder]
    while chain[-1]:
        chain.append(os.path.dirname(chain[-1]))
    home = next((step for step in chain if (root / step / DOTNET_TARGETS).is_file()), folder)
    path = f"{home}/{DOTNET_TARGETS}" if home else DOTNET_TARGETS
    up = os.path.relpath(".", home or ".").replace(os.sep, "/")
    prefix = "" if up == "." else up + "/"
    begin, end = markers(path, "dotnet")
    imported = f"$(MSBuildThisFileDirectory){prefix}.harness/dotnet/Harness.Build.targets"
    own = "\n  ".join(markers(path, PROJECT_BLOCK))
    text = f'<Project>\n  {own}\n  {begin}\n  <Import Project="{imported}" />\n  {end}\n</Project>\n'
    return Item(path, "block", "backend/dotnet", text.encode("utf-8"), False, "dotnet")


NODE_ADAPTER = "layerBoundaries"
LOCAL_IMPORT = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""")
IMPORT_SUFFIXES = ("", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", "/index.js", "/index.mjs", "/index.ts")


def wiring_text(root: Path, config: Path) -> str:
    adapter, seen, pending, texts = (root / ".config").resolve(), set(), [config.resolve()], []
    while pending:
        current = pending.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        text = current.read_text(encoding="utf-8", errors="replace")
        texts.append(text)
        for specifier in LOCAL_IMPORT.findall(text):
            base = os.path.normpath(current.parent / specifier)
            found = next((Path(base + suffix) for suffix in IMPORT_SUFFIXES if os.path.isfile(base + suffix)), None)
            if found and found.is_relative_to(root.resolve()) and not found.is_relative_to(adapter) \
                    and "node_modules" not in found.parts:
                pending.append(found)
    return "\n".join(texts)


def remediation(root: Path, project: dict) -> list[str]:
    lines = []
    if "forge/gitlab" in enabled(project):
        payload_scripts(kit_root())
        import gitlab_workflow

        lines += [msg("remediation_gitlab_workflow", path, gitlab_workflow.KIT_FILE,
                      "".join("\n      " + line for rule in rules for line in rule.splitlines()))
                  for path, rules in gitlab_workflow.missing(str(root))]
    released = json.loads((kit_root() / "kit.json").read_text(encoding="utf-8")).get("released", [])
    for app in project.get("apps", []) if "stack/node" in enabled(project) else []:
        if app.get("stack") != "node":
            continue
        text = "".join(wiring_text(root, path) for path in sorted((root / app["path"]).glob("eslint.config.*")))
        if NODE_ADAPTER not in text and not layerless(app):
            lines.append(msg("remediation_node", app["id"], app["path"], NODE_ADAPTER))
        lines += [msg("remediation_released", app["id"], path) for path in released
                  if posixpath.basename(path) in text and (root / path).is_file()
                  and "export const required" not in (root / path).read_text(encoding="utf-8")]
    return lines
