from __future__ import annotations

import functools
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import ci_common
from ci_common import LIVE_DOC_PREFIXES, app_prefix, get_list, get_records, msg, setting, tracked, workflow_files
from generated_sources import generated

ROOT = Path(ci_common.ROOT)
NODE_VERSION_FILE = ROOT / ".node-version"
PACKAGE_JSON = ROOT / "package.json"
SETUP_NODE_ACTION = ROOT / ".github" / "actions" / "setup-node" / "action.yml"

AGENT_SECRETS = ("claude_code_oauth_token:", "claude-oauth-token:")
COMMIT_DIGEST = re.compile(r"^[0-9a-f]{40}$")
ACTION_REF = re.compile(r"^\s*-?\s*uses:\s*(\S+)")
PLUGIN_REF = re.compile(r"^\s*(plugins|plugin_marketplaces):\s*(\S.*?)\s*$")

MAJOR_VERSIONED_TOOLS = r"\.NET(?:\s+SDK)?|C#|Python|Node(?:\.js)?|TypeScript|React|Vite|ESLint|xUnit|PostgreSQL|EF Core|Java"
DOTTED_VERSIONED_TOOLS = r"SDK|Docker|Compose|Redis|Go|Gradle|Maven|Playwright|shellcheck|Vale|gitleaks"
DOCUMENTED_TOOL_VERSION = re.compile(
    rf"(?i)(?<![\w.])(?:(?:{MAJOR_VERSIONED_TOOLS})\s+v?\d+|(?:{DOTTED_VERSIONED_TOOLS})\s+v?\d+\.\d+)"
    r"(?:\.\d+)*\b(?![-.]\d)")
IMAGE_VERSION_TAG = r"v?\d{1,2}(?!\d)(?:\.\d+){0,3}(?:-[a-z0-9][a-z0-9._-]*)?"
DOCUMENTED_IMAGE_VERSION = re.compile(
    rf"(?i)(?:(?:postgres|redis|node|python|aspnet|dotnet|golang|eclipse-temurin):{IMAGE_VERSION_TAG}\b|"
    rf"mcr\.microsoft\.com/[^\s`]+:{IMAGE_VERSION_TAG}\b)")
DOCUMENTED_TFM_VERSION = re.compile(r"(?i)\bnet\d+\.\d+\b")
HISTORICAL_PREFIXES = tuple(get_list("docs.historicalPrefixes", ()))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def stacks() -> set[str]:
    return {str(app.get("stack", "")) for app in get_records("apps", [])}


def agent_workflow_pins() -> tuple[list[str], int]:
    findings: list[str] = []
    seen = 0
    sources = [*map(Path, workflow_files()), *(ROOT / ".github" / "actions").rglob("action.yml")]
    for workflow in sorted(sources):
        text = read(workflow)
        if not any(secret in text for secret in AGENT_SECRETS):
            continue
        seen += 1
        for number, line in enumerate(text.splitlines(), start=1):
            action = ACTION_REF.match(line)
            if action and not action.group(1).startswith((".", "/")):
                if not COMMIT_DIGEST.fullmatch(action.group(1).partition("@")[2]):
                    findings.append(msg("pins_action_not_digest", relative(workflow), number, action.group(1)))
            plugin = PLUGIN_REF.match(line)
            if plugin and not re.search(r"[0-9a-f]{40}", plugin.group(2)):
                findings.append(msg("pins_plugin_mutable", relative(workflow), number))
    return findings, seen


def node_contracts() -> tuple[list[str], str]:
    findings: list[str] = []
    if not NODE_VERSION_FILE.is_file():
        return [msg("pins_node_version_missing")], msg("value_absent")
    version = read(NODE_VERSION_FILE).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        findings.append(msg("pins_node_version_form"))
    if not PACKAGE_JSON.is_file():
        findings.append(msg("pins_node_root_package"))
    else:
        major = int(version.split(".", 1)[0]) if version[:1].isdigit() else 0
        engine = json.loads(read(PACKAGE_JSON)).get("engines", {}).get("node")
        expected = f">={major}.0.0 <{major + 1}"
        if engine != expected:
            findings.append(msg("pins_node_engine", engine or msg("value_absent"), expected))
    if SETUP_NODE_ACTION.is_file():
        action = read(SETUP_NODE_ACTION)
        if "node-version-file: .node-version" not in action:
            findings.append(msg("pins_setup_node"))
        if ".harness/provisioning/scripts/ensure-npm.sh" not in action:
            findings.append(msg("pins_setup_npm"))
        for workflow in map(Path, workflow_files()):
            if "actions/setup-node@" in read(workflow):
                findings.append(msg("pins_setup_node_raw", relative(workflow)))
    return findings, version


def solution_projects(solution: Path) -> list[Path]:
    text = read(solution)
    if solution.suffix == ".slnx":
        listed = [node.get("Path", "") for node in ET.fromstring(text).iter() if node.tag.rsplit("}", 1)[-1] == "Project"]
    else:
        listed = SOLUTION_PROJECT.findall(text)
    found = [Path(os.path.normpath(solution.parent / item.replace("\\", "/"))) for item in listed if item.endswith(".csproj")]
    return [path for path in found if path.is_file() and path.is_relative_to(ROOT)]


def msbuild_files() -> list[Path]:
    found = [ROOT / path for path in tracked() if path.endswith((".csproj", ".props", ".targets"))]
    return [path for path in found if path.is_file()]


@functools.cache
def parsed(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def inline_version(node: ET.Element) -> bool:
    return any(name in node.attrib or node.find(f"{{*}}{name}") is not None for name in ("Version", "VersionOverride"))


@functools.cache
def nearest(folder: Path, name: str) -> Path | None:
    for candidate in [folder, *folder.parents]:
        if (candidate / name).is_file():
            return candidate / name
        if candidate == ROOT:
            break
    return None


def central_file(project: Path) -> Path | None:
    return nearest(project.parent, "Directory.Packages.props")


def conditioned(path: Path, name: str) -> list[tuple[ET.Element, bool]]:
    found = []

    def walk(node: ET.Element, conditional: bool) -> None:
        conditional = conditional or "Condition" in node.attrib
        for child in node:
            if child.tag.rsplit("}", 1)[-1] == name:
                found.append((child, conditional or "Condition" in child.attrib))
            else:
                walk(child, conditional)

    walk(parsed(path), False)
    return found


ARGUMENT = r"\s*'?([^,')]+?)'?\s*"
ABOVE = re.compile(rf"\$\(\[MSBuild\]::GetPathOfFileAbove\({ARGUMENT}(?:,{ARGUMENT})?\)\)")
FOLDER_ABOVE = re.compile(rf"\$\(\[MSBuild\]::GetDirectoryNameOfFileAbove\({ARGUMENT},{ARGUMENT}\)\)(.*)")
NORMALIZE = re.compile(r"\$\(\[MSBuild\]::Normalize(?:Path|Directory)\((.*)\)\)")
SOLUTION_PROJECT = re.compile(r'^Project\("[^"]*"\)\s*=\s*"[^"]*",\s*"([^"]+\.csproj)"', re.MULTILINE)


def import_target(path: Path, project: str) -> Path | None:
    for name, value in (("$(MSBuildThisFileDirectory)", f"{path.parent}/"), ("$(MSBuildThisFileFullPath)", str(path)),
                        ("$(MSBuildThisFileName)", path.stem), ("$(MSBuildThisFileExtension)", path.suffix),
                        ("$(MSBuildThisFile)", path.name)):
        project = project.replace(name, value)
    project = project.replace("\\", "/").strip()
    above, folder, normalized = ABOVE.fullmatch(project), FOLDER_ABOVE.fullmatch(project), NORMALIZE.fullmatch(project)
    if normalized:
        parts = [part.strip().strip("'") for part in normalized.group(1).split(",")]
        return Path(os.path.normpath(path.parent.joinpath(*parts))) if parts and "$(" not in "".join(parts) else None
    if above:
        return nearest(Path(os.path.normpath(path.parent / (above.group(2) or "."))), above.group(1))
    if folder:
        found = nearest(Path(os.path.normpath(path.parent / folder.group(1))), folder.group(2))
        return Path(os.path.normpath(f"{found.parent}/{folder.group(3)}")) if found else None
    return Path(os.path.normpath(path.parent / project)) if project and "$(" not in project else None


@functools.cache
def imports(path: Path) -> tuple[tuple[Path, bool], ...]:
    found = []
    for node, conditional in conditioned(path, "Import"):
        target = import_target(path, node.get("Project", ""))
        if target is not None and target.is_file() and target.is_relative_to(ROOT):
            found.append((target, conditional))
    return tuple(found)


def walk(starts: list[Path]) -> tuple[set[Path], set[Path]]:
    pending = [(path, False) for path in starts]
    certain: set[Path] = set()
    maybe: set[Path] = set()
    while pending:
        current, conditional = pending.pop()
        if current in certain or (conditional and current in maybe):
            continue
        (maybe if conditional else certain).add(current)
        pending += [(target, conditional or flag) for target, flag in imports(current)]
    return certain, maybe - certain


def closure(project: Path) -> tuple[set[Path], set[Path]]:
    implicit = (nearest(project.parent, name) for name in ("Directory.Build.props", "Directory.Build.targets"))
    return walk([project, *(path for path in implicit if path)])


def imported(path: Path) -> set[Path]:
    return set().union(*walk([path]))


def dotnet_contracts(solution: Path) -> tuple[list[str], str, int]:
    findings: list[str] = []
    global_json = nearest(solution.parent, "global.json") or solution.parent / "global.json"
    sdk = msg("value_absent")
    if not global_json.is_file():
        findings.append(msg("pins_global_json_missing", relative(global_json)))
    else:
        sdk = str(json.loads(read(global_json)).get("sdk", {}).get("version") or "")
        if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", sdk):
            findings.append(msg("pins_dotnet_sdk_form", sdk or msg("value_absent")))
    found = msbuild_files()
    manifests = {path for path in found if path.suffix in (".props", ".targets") or path.is_relative_to(solution.parent)}
    manifests |= set(solution_projects(solution))
    consumers: dict[Path, set[Path]] = {}
    used: dict[Path, set[str]] = {}
    for project in sorted(path for path in manifests if path.suffix == ".csproj"):
        certain, maybe = closure(project)
        manifests |= certain | maybe
        central = central_file(project)
        if central is None:
            continue
        used.setdefault(central, set())
        for path in certain:
            consumers.setdefault(path, set()).add(central)
    anywhere: set[str] = set()
    for manifest in sorted(manifests):
        if manifest.name == "Directory.Packages.props":
            continue
        references = conditioned(manifest, "PackageReference")
        centrals = consumers.get(manifest, set())
        if manifest.suffix == ".csproj" and not centrals and references:
            findings.append(msg("pins_central_absent", relative(manifest)))
            continue
        for node, conditional in references:
            package = node.get("Include") or node.get("Update") or msg("pins_unknown_package")
            if inline_version(node):
                findings.append(msg("pins_version_not_central", relative(manifest), package,
                                    ", ".join(sorted(map(relative, centrals))) or "-"))
            if "Include" not in node.attrib:
                continue
            if centrals and (manifest.suffix == ".csproj" or not conditional):
                for central in centrals:
                    used[central].add(package)
            else:
                anywhere.add(package)
    closures = {central: imported(central) for central in used}
    declared = {path: {node.get("Include") for node in parsed(path).findall(".//{*}PackageVersion") if node.get("Include")}
                for path in set().union(*closures.values())}
    users = {path: set(anywhere) for path in declared}
    for child, members in closures.items():
        for member in members:
            users[member] |= used[child]
    for path in sorted(declared):
        visible = set().union(*(declared[member] for member in closures.get(path, ())))
        for package in sorted(used.get(path, set()) - visible):
            findings.append(msg("pins_central_missing", relative(path), package))
        for package in sorted(declared[path] - users[path]):
            findings.append(msg("pins_central_unused", relative(path), package))
    return findings, sdk, sum(map(len, declared.values()))


def documented_version_locations(scopes: tuple[str, ...], patterns: tuple[re.Pattern, ...]) -> list[str]:
    locations = []
    for path in tracked():
        if not path.endswith(".md") or path.startswith(HISTORICAL_PREFIXES) or generated(path):
            continue
        if path != "README.md" and not path.startswith(scopes):
            continue
        file = ROOT / path
        if not file.is_file():
            continue
        for number, line in enumerate(read(file).splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                locations.append(f"{path}:{number}")
    return locations


def documented_versions() -> list[str]:
    apps = (app_prefix(str(app.get("path", "."))) for app in get_records("apps", []))
    scopes = ("docs/", *LIVE_DOC_PREFIXES, *(prefix for prefix in apps if prefix))
    patterns = (DOCUMENTED_TOOL_VERSION, DOCUMENTED_IMAGE_VERSION, DOCUMENTED_TFM_VERSION)
    return [msg("pins_tool_not_manifest", location) for location in documented_version_locations(scopes, patterns)]


def main() -> int:
    failures: list[str] = []
    active = stacks()
    agent_findings, agent_seen = agent_workflow_pins()
    failures.extend(agent_findings)
    pinned = msg("pins_digest_all") if not agent_findings else msg("pins_digest_partial")
    print(msg("pins_summary_agents", agent_seen, pinned))
    failures.extend(documented_versions())
    if "node" in active:
        findings, version = node_contracts()
        failures.extend(findings)
        print(f"  Node.js: .node-version = {version}")
    solution = setting("backend/dotnet", "solution", "")
    if "dotnet" in active or solution:
        if not solution or not (ROOT / solution).is_file():
            failures.append(msg("pins_solution_missing", solution or msg("value_absent")))
        else:
            findings, sdk, count = dotnet_contracts(ROOT / solution)
            failures.extend(findings)
            global_json = nearest((ROOT / solution).parent, "global.json") or (ROOT / solution).parent / "global.json"
            print(f"  .NET SDK: {relative(global_json)} = {sdk}")
            print(msg("pins_summary_nuget", count))
    if failures:
        print("\n" + msg("pins_diverged"))
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(msg("pins_consistent"))
    return 0
