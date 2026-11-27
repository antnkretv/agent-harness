from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from installer.catalog import pack_skills
from installer.archive import archive
from installer.documents import block_of, compose, json_text, markers
from installer.resources import (ANSWERS, LOADER, AREA_MAP, CONFIG, GUIDES, LEGACY_MANIFEST, LEGACY_PROJECT, LEGACY_SEEDS, LOCK, METHODOLOGY, POSIX_MODES,
                                 PROJECT, PROJECT_BLOCK, SETUP_WRAPPERS, SETTINGS, SETTINGS_DEFAULTS, index_modes,
                                 rewrite_lock, sha256)
from installer.resources import msg as kit_msg
from installer.resources import unpack
from installer.lock_install import archive_build, loader
from installer.verify import run_gate

from .context import KIT, catalog, msg
from .fixtures import (
    BACKEND_DECISIONS,
    DOTNET_FILES,
    DOTNET_PACKS,
    EDITED,
    GITLAB_ANSWERS,
    GITLAB_CI,
    GITLAB_PIPELINES,
    IDENTITY,
    LSP_CONFIG,
    NODE_FILES,
    SIDE_LSP,
    SKILL_PACKS,
    SMOKE_ANSWERS,
    SMOKE_APP_PATHS,
    SMOKE_MCP_URL,
    SMOKE_STAND_URL,
)
from .source import leak_rules, leaks

INSTALLER_PATHS = (".harness/state/harness.py", ".harness/state/installer", ".harness/state/kit.json",
                   ".harness/state/messages.env", *(f"{folder}/skills/{name}" for folder in (".agents", ".claude", ".cursor", ".codex")
                                                        for name in ("harness-init", "harness-sync")))

# Сценарии ставят комплект от имени человека: признаки агентской сессии, в которой идет проверка, в фикстуры не переносятся.
AGENT_ENV = {pair.partition("=")[0] for pair in
             json.loads((KIT / "payload" / METHODOLOGY).read_text(encoding="utf-8"))["policies"]["authorship"]["agentEnv"]}
ENV = {**{key: value for key, value in os.environ.items() if key not in AGENT_ENV and not key.startswith("HARNESS_TASK_")},
       **IDENTITY}


WINDOWS_REASON = "буквой диска"
WALKER = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from installer.configure import settle
from installer.resources import Refusal
root, summaries = Path(sys.argv[2]), json.loads(sys.argv[3])
asked, items, given = [], {}, {}
try:
    while len(asked) < 500:
        pending, problems, _ = settle(root, given, strict=True)
        if pending is None:
            print(json.dumps({"asked": asked, "items": items, "problems": problems}))
            sys.exit(0)
        value = summaries.get(pending.id, pending.default)
        if value is None:
            print(json.dumps({"stuck": pending.id, "text": pending.text}))
            sys.exit(1)
        asked.append(pending.id)
        if pending.items is not None:
            items[pending.id] = pending.items
        given = {pending.id: value}
except Refusal as error:
    print(error, file=sys.stderr)
    sys.exit(2)
sys.exit(1)
"""

PROMPTER = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from installer import prompt
prompt.questionary()
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
keys, asked, items, shown = json.loads(sys.argv[3]), [], [], prompt.asked


def scripted(library, question):
    asked.append(question.id)
    items.extend(item["id"] for item in question.items or [])
    with create_pipe_input() as pipe:
        pipe.send_text(keys.get(question.id, "\\r"))
        with create_app_session(input=pipe, output=DummyOutput()):
            return shown(library, question)


prompt.terminal, prompt.asked = (lambda: True), scripted
problems = prompt.answered(Path(sys.argv[2]), sys.argv[4] == "change")
print(json.dumps({"asked": asked, "items": items, "problems": problems}))
"""


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return run_gate(cwd, command, env={**ENV, **(env or {})})


def retitle(text: str, mark: str) -> str:
    """Правка проекта в первой строке файла комплекта."""
    first, _, rest = text.partition("\n")
    return f"{first} - {mark}\n{rest}"


def following(kit_folder: Path) -> str:
    """Следующий патч собранной версии: сценарий обновления не зависит от номера выпуска."""
    major, minor, patch = json.loads((kit_folder / "kit.json").read_text(encoding="utf-8"))["version"].split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def variant(kit_folder: Path, out: Path, version: str, edit) -> Path:
    """Следующая версия комплекта для проверки обновления: тот же состав, правка одного файла."""
    folder = out / f"kit-{version}"
    shutil.copytree(kit_folder, folder)
    shipped = json.loads((folder / "kit.json").read_text(encoding="utf-8"))
    target = folder / "payload" / EDITED
    target.write_text(edit(target.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
    shipped["version"] = version
    (folder / "kit.json").write_text(json_text(shipped), encoding="utf-8", newline="\n")
    return folder


class Smoke:
    """Установка в пустой репозиторий без файлов источника и сценарии против нее: каждый печатает итог."""

    def __init__(self, kit_folder: Path, adapters: list[str], work: Path):
        self.kit, self.adapters, self.work = kit_folder, adapters, work
        self.installed = kit_folder
        self.repo = work / "project"
        self.problems: list[str] = []
        self.archives: dict[Path, Path] = {}

    def installer(self, kit: Path | None = None) -> list[str]:
        return [sys.executable, str((kit or self.kit) / "ah.py")]

    def harness(self, *args: str) -> list[str]:
        """Обслуживание установки идет установщиком архива установленной версии: в проекте его нет."""
        return self.installer(self.installed) + [*args, "--target", str(self.repo)]

    def answers(self) -> list[str]:
        """Ответы сценария: форму и стек дают фикстуры, без них - бэкенд стека без адаптера."""
        found = ([] if self.adapters else ["shape=backend", "modules.backend=none"]) + list(SMOKE_ANSWERS)
        found.append("modules.skills=" + ",".join(self.packs()))
        return found

    def packs(self) -> tuple[str, ...]:
        return SKILL_PACKS + DOTNET_PACKS * ("dotnet" in self.adapters)

    def configure(self, title: str, *answers: str, kit: Path | None = None) -> bool:
        """Настройка вопросами за владельца: ответы сценария, на прочие решения - ответ вопроса по умолчанию."""
        used = self.walk(dict(pair.split("=", 1) for pair in answers), kit) is not None
        self.check(title, used)
        return used

    def apply(self, kit: Path | None = None) -> list[str]:
        """Установка идет архивом сборки, как у владельца: lock.json закрепляет его tree."""
        kit = kit or self.kit
        return self.installer(kit) + ["apply", "--target", str(self.repo), "--archive", str(self.archive_of(kit))]

    def archive_of(self, kit: Path) -> Path:
        if kit not in self.archives:
            self.archives[kit] = self.built(kit, str(len(self.archives)))
        return self.archives[kit]

    def upgrade(self, title: str, kit: Path, reason: str = "") -> bool:
        """Обновление: дальше verify и status идут установщиком новой версии."""
        passed = self.expect(title, self.apply(kit), reason=reason)
        if passed:
            self.installed = kit
        return passed

    def expect(self, title: str, command: list[str], code: int = 0, cwd: Path | None = None, reason: str = "",
               env: dict[str, str] | None = None) -> bool:
        """Отказ засчитывается, только если вывод называет ожидаемую причину: иначе отказал бы не тот гейт."""
        done = run(command, cwd or self.repo, env)
        passed = (done.returncode == code if code >= 0 else done.returncode != 0) and reason in done.stdout + done.stderr
        print(("  ok    " if passed else "  FAIL  ") + title, flush=True)
        if not passed:
            self.problems.append(msg("kit_smoke_failed", title, done.returncode))
            print(done.stdout + done.stderr, flush=True)
        return passed

    def check(self, title: str, condition: bool) -> None:
        print(("  ok    " if condition else "  FAIL  ") + title, flush=True)
        if not condition:
            self.problems.append(msg("kit_smoke_failed", title, "-"))

    def commit(self, message: str) -> None:
        listed = run(["git", "ls-files", "-z", "--others", "--modified", "--deleted", "--exclude-standard"], self.repo)
        paths = sorted({path for path in listed.stdout.split("\0") if path})
        if paths:
            run(["git", "add", "--", *paths], self.repo)
        run(["git", "commit", "-qm", message, "--allow-empty"], self.repo)

    def clean(self) -> bool:
        return not run(["git", "status", "--porcelain"], self.repo).stdout.strip()

    def write(self, path: str, text: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(text, encoding="utf-8", newline="\n")

    def write_json(self, path: str, data: dict) -> None:
        self.write(path, json_text(data))

    def read_json(self, path: str) -> dict:
        return json.loads(self.read(path))

    def plant(self, violation: str, path: str) -> None:
        """Заведомое нарушение лежит фикстурой: сценарий кладет его в дерево и ждет отказа гейта."""
        self.write(path, (KIT / "tools" / "fixtures" / "violations" / violation).read_text(encoding="utf-8"))

    def agent_mcp(self, name: str, every: bool = True, url: str | None = None) -> bool:
        """Сервер MCP подключен у каждого установленного агента разработки (every) или хотя бы у одного;
        url сверяет адрес найденной записи."""
        entries = []
        if (self.repo / ".codex" / "config.toml").is_file():
            entries.append(tomllib.loads(self.read(".codex/config.toml")).get("mcp_servers", {}).get(name))
        if (self.repo / ".cursor" / "mcp.json").is_file():
            entries.append(self.read_json(".cursor/mcp.json").get("mcpServers", {}).get(name))
        if (self.repo / ".claude" / "settings.json").is_file():
            config = self.read_json(".mcp.json") if (self.repo / ".mcp.json").is_file() else {}
            enabled = name in self.read_json(".claude/settings.json").get("enabledMcpjsonServers", [])
            entries.append(config.get("mcpServers", {}).get(name) if enabled else None)
        found = [entry is not None and (url is None or entry.get("url") == url) for entry in entries]
        return bool(found) and (all(found) if every else any(found))

    def read(self, path: str) -> str:
        return (self.repo / path).read_text(encoding="utf-8")

    def restore(self) -> None:
        run(["git", "reset", "-q", "--hard"], self.repo)
        run(["git", "clean", "-fdq", "-e", "node_modules", "-e", "bin", "-e", "obj"], self.repo)
        if (self.repo / LOADER).is_file() and (self.repo / LOCK).is_file():
            run([sys.executable, str(self.repo / LOADER), "install"], self.repo)

    def bootstrap(self, remote: str = "https://github.com/example/smoke.git") -> None:
        self.repo.mkdir(parents=True)
        run(["git", "init", "-q", "-b", "develop"], self.repo)
        run(["git", "remote", "add", "origin", remote], self.repo)
        self.write("README.md", "# smoke\n")
        for adapter in self.adapters:
            shutil.copytree(KIT / "tools" / "fixtures" / adapter, self.repo, dirs_exist_ok=True)
        self.commit("chore: init")
        self.base = run(["git", "rev-parse", "HEAD"], self.repo).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/develop", self.base], self.repo)

    def install(self) -> bool:
        self.bootstrap()
        if not self.configure(msg("smoke_configure"), *self.answers()):
            return False
        project = self.read_json(PROJECT)
        self.check(msg("smoke_apps_detected"), sorted(app["path"] for app in project["apps"])
                   == sorted(SMOKE_APP_PATHS[name] for name in self.adapters))
        shipped = pack_skills(catalog())
        self.check(msg("smoke_vendored_packs"), {name for pack in self.packs() for name in shipped[f"skills/{pack}"]}
                   <= set(project["docs"]["vendoredSkills"]))
        broken = json.loads(json.dumps(project))
        broken["modules"]["forge"]["extensions"]["github"]["repo"] = ""
        self.write_json(PROJECT, broken)
        self.expect(msg("smoke_bad_manifest"), self.apply(), 2, reason="forge/github.repo")
        self.check(msg("smoke_bad_manifest_untouched"), not (self.repo / LOCK).exists())
        self.write_json(PROJECT, project)
        self.write(".agents/skills/review/smoke-team-notes.md", "# smoke-team-skill\n")
        self.expect(msg("smoke_skill_taken"), self.apply(), 2, reason=".agents/skills/review/")
        (self.repo / ".agents/skills/review/smoke-team-notes.md").unlink()
        if not self.expect(msg("smoke_apply"), self.apply()):
            return False
        self.check(msg("smoke_no_installer"), not any((self.repo / path).exists() for path in INSTALLER_PATHS))
        self.commit("chore: harness")
        self.installed_head = run(["git", "rev-parse", "HEAD"], self.repo).stdout.strip()
        unset = kit_msg("verify_entries_unset", "\0", "\0").split("\0")[1] if self.adapters else ""
        return self.expect(msg("smoke_verify"), self.harness("verify"), reason=unset)

    def isolated(self, kit: dict) -> None:
        """Установленное дерево не знает каталога комплекта на машине сборки и следов проектов."""
        found = leaks(leak_rules(kit, installed=True), self.repo, run(["git", "ls-files"], self.repo).stdout.splitlines())
        self.check(msg("smoke_no_source_refs"), not found)
        self.problems += found[:20]

    def gates(self) -> None:
        self.expect(msg("smoke_apply_again"), self.apply())
        self.check(msg("smoke_idempotent"), self.clean())
        self.expect(msg("smoke_check_all"), [sys.executable, ".harness/scripts/check/check-all.py", "--base", self.base],
                    reason="NONE" if self.adapters else "")
        self.expect(msg("smoke_kit_anchors"), [sys.executable, ".harness/scripts/check/check-docs.py", "--anchors", "--kit-files"])
        # Самопроверки поставляемого кода в поставку не входят: сценарий кладет их в дерево на время прогона.
        shutil.copytree(KIT / "tests" / "platform", self.repo / ".harness" / "tests", dirs_exist_ok=True)
        self.expect(msg("smoke_self_manifest"), ["bash", ".harness/tests/manifest.sh"])
        self.expect(msg("smoke_self_hooks"), [sys.executable, ".harness/tests/hooks.py"])
        self.expect(msg("smoke_self_review_plan"), [sys.executable, ".harness/tests/review-plan.py"])
        self.expect(msg("smoke_self_tracker_close"), [sys.executable, ".harness/tests/tracker-close.py"])
        self.expect(msg("smoke_self_text_forms"), [sys.executable, ".harness/tests/text-forms.py"])
        self.restore()
        self.change_gates()
        self.kit_weakening()

    def change_gates(self) -> None:
        """Гейты данных запроса входят в план только с PR_NUMBER: Check в CI его не передает, прогон конвейера передает."""
        listed = [sys.executable, ".harness/scripts/check/check-all.py", "--base", self.base, "--list"]
        for title, extra, wanted in ((msg("smoke_change_gate_skipped"), {"GITHUB_ACTIONS": "true"}, False),
                                     (msg("smoke_change_gate_run"), {"GITHUB_ACTIONS": "true", "PR_NUMBER": "1"}, True)):
            done = run(listed, self.repo, extra)
            gates = set(json.loads(done.stdout)["gates"]) if done.returncode == 0 else set()
            self.check(title, done.returncode == 0 and ({"pr-size", "pr-issue-link"} <= gates) == wanted)

    def kit_weakening(self) -> None:
        """Правка поля kit в lock.json не открывает ослабление severity в блоке комплекта."""
        lock = self.read_json(LOCK)
        lock["kit"]["version"] = "99.0.0"
        self.write_json(LOCK, lock)
        end = markers(".editorconfig", "core")[1]
        text = self.read(".editorconfig")
        stop = text.index(end)
        self.write(".editorconfig", text[:stop] + "dotnet_diagnostic.CA1000.severity = none\n" + text[stop:])
        self.expect(msg("smoke_kit_weakening"), [sys.executable, ".harness/scripts/check/check-diagnostic-integrity.py",
                                                 "--base", self.installed_head], -1, reason="CA1000")
        self.restore()

    def preservation(self) -> None:
        agents = self.read("AGENTS.md")
        self.check(msg("smoke_seed_entry"), ".harness/AGENTS.md" in agents and "<!--" not in agents
                   and "@.harness/AGENTS.md" in self.read("CLAUDE.md"))
        agents += "\n## smoke-project-rule\n"
        self.write("AGENTS.md", agents)
        self.write("specs/README.md", self.read("specs/README.md") + "\nsmoke-project-row\n")
        seed = ".github/workflows/project-deploy.yml"
        deploy = self.read(seed)
        (self.repo / seed).unlink()
        self.commit("docs: smoke-project-rules")
        self.expect(msg("smoke_apply_keeps"), self.apply())
        self.check(msg("smoke_seed_project_kept"), self.read("AGENTS.md") == agents)
        self.check(msg("smoke_section_kept"), "\n[*]\nindent_style = tab\n" in compose(
            ".editorconfig", "root = true\n\n[*]\nindent_style = tab\n", self.read(".editorconfig"), "core"))
        self.check(msg("smoke_seed_kept"), self.read("specs/README.md").endswith("smoke-project-row\n"))
        self.check(msg("smoke_seed_removed"), not (self.repo / seed).exists() and self.clean())
        self.expect(msg("smoke_seed_removed_verify"), self.harness("verify", "--no-gates"))
        self.expect(msg("smoke_seed_dangling"), [sys.executable, ".harness/scripts/check/check-pipeline.py"], -1, reason=seed)
        self.write(seed, deploy)
        self.commit("chore: smoke-seed-restored")
        path = ".editorconfig"
        config = self.read(path)
        begin, end = markers(path, "core")
        self.check(msg("smoke_no_duplicates"), config.count(begin) == 1)
        stop = config.index(end)
        self.write(path, config[:stop] + "smoke-project-block-line\n" + config[stop:])
        self.expect(msg("smoke_status_edit"), self.harness("status"), reason=path)
        self.expect(msg("smoke_apply_again"), self.apply())
        self.check(msg("smoke_block_overwritten"), self.read(path) == config and self.clean())
        self.write(path, config + "smoke-stray-line\n")
        self.expect(msg("smoke_outside_verify"), self.harness("verify", "--no-gates"), -1, reason=path)
        self.expect(msg("smoke_apply_again"), self.apply())
        moved = self.read(path)
        self.check(msg("smoke_outside_moved"), "smoke-stray-line\n" + markers(path, PROJECT_BLOCK)[1] in moved)
        self.restore()
        self.project_files()

    def project_files(self) -> None:
        """Отслеживаемый файл не ссылается на установленный, а AGENTS.md подключает правила комплекта."""
        self.write("docs/own.md", "# own\n\nГейты - [ci-cd](../.harness/docs/ci-cd.md).\n")
        self.expect(msg("smoke_installed_link"), self.harness("verify", "--no-gates"), -1, reason="docs/own.md:3")
        self.write("docs/own.md", "# own\n\nГейты - `.harness/docs/ci-cd.md`.\n")
        self.write("AGENTS.md", "# Agents\n")
        self.expect(msg("smoke_entry_line"), self.harness("verify", "--no-gates"), -1, reason=".harness/AGENTS.md")
        self.restore()

    def legacy_layout(self) -> None:
        """Проект прежней раскладки: блок комплекта в AGENTS.md, установленный стандарт и ссылки на файлы комплекта.
        apply делает AGENTS.md и стандарт файлами проекта и снимает ссылки."""
        agents = self.read("AGENTS.md")
        kit_text = (self.repo / ".harness/AGENTS.md").read_text(encoding="utf-8").split("\n", 1)[1]
        begin, end = markers("AGENTS.md", "core")
        own, done = markers("AGENTS.md", PROJECT_BLOCK)
        rules = ("- [находки](CONTRIBUTING.md#10-находки-по-пути), [релиз](docs/platform/delivery/ci-cd.md#релиз), "
                 "[справочник](https://github.com/antnkretv/agent-harness/blob/abc123/payload/docs/platform/agents/harness.md#обновление), "
                 "[`issue-dev`](.agents/skills/issue-dev/SKILL.md), [`check-docs.py`](.harness/scripts/check/check-docs.py)")
        self.write("AGENTS.md", f"# Agents\n\n{begin}\n{kit_text.strip()}\n{end}\n\n{own}\n## smoke-own\n\n{rules}\n{done}\n")
        lock = self.read_json(LOCK)
        standard = "docs/platform/standards/text.md"
        lock["files"]["AGENTS.md"] = {"role": "block", "block": "core", "module": "core", "sha256": "0" * 64}
        lock["files"].pop(standard)
        self.write_json(LOCK, lock)
        record = Path(run(["git", "rev-parse", "--absolute-git-dir"], self.repo).stdout.strip()) / "harness" / "installed.json"
        placed = json.loads(record.read_text(encoding="utf-8"))
        placed["files"][standard] = "0" * 64
        record.write_text(json.dumps(placed), encoding="utf-8")
        run(["git", "rm", "-q", "--cached", "--", standard], self.repo)
        self.write(standard, "# old installed copy\n")
        ignore = self.read(".gitignore")
        stop = ignore.index(markers(".gitignore", "core")[1])
        self.write(".gitignore", ignore[:stop] + "/" + standard + "\n" + ignore[stop:])
        self.commit("chore: smoke-legacy-layout")
        self.expect(msg("smoke_legacy_apply"), self.apply(), reason=kit_msg("state_seed_converted"))
        converted = self.read("AGENTS.md")
        self.check(msg("smoke_legacy_converted"), "<!--" not in converted and "## smoke-own" in converted
                   and ".harness/AGENTS.md" in converted and self.read_json(LOCK)["files"]["AGENTS.md"]["role"] == "seed")
        self.check(msg("smoke_legacy_links"), "](" not in converted.split("## smoke-own", 1)[1]
                   and "`.harness/CONTRIBUTING.md`" in converted and "`.harness/docs/ci-cd.md`" in converted
                   and "`.harness/docs/harness.md`" in converted and "гейт `docs-structure`" in converted)
        ignored = run(["git", "check-ignore", "-q", "--", standard], self.repo).returncode == 0
        self.check(msg("smoke_legacy_adopted"), not ignored and self.read(standard) == (self.installed / "payload" / standard).read_text(encoding="utf-8"))
        lock = self.read_json(LOCK)
        lock["files"]["AGENTS.md"] = {"role": "block", "block": "core", "module": "core", "sha256": "0" * 64}
        self.write_json(LOCK, lock)
        self.expect(msg("smoke_apply_again"), self.apply())
        self.check(msg("smoke_legacy_again"), self.read("AGENTS.md") == converted)
        self.commit("chore: smoke-legacy-converted")
        self.expect(msg("smoke_legacy_verify"), self.harness("verify", "--no-gates"))
        self.expect(msg("smoke_apply_again"), self.apply())
        self.check(msg("smoke_idempotent"), self.clean())
        self.write("AGENTS.md", agents)
        self.commit("chore: smoke-legacy-restored")

    def extensions(self) -> None:
        ignored = run(["git", "check-ignore", "-q", "--", SETTINGS], self.repo).returncode == 0
        self.check(msg("smoke_settings_ignored"), (self.repo / SETTINGS).is_file() and ignored
                   and (self.repo / SETTINGS).read_text(encoding="utf-8") == self.read(SETTINGS_DEFAULTS))
        defaults = self.read(SETTINGS_DEFAULTS)
        self.write(SETTINGS, "SMOKE_ENV=settings\nSMOKE_SETTINGS=settings\nSMOKE_DEFAULTS=\n")
        self.write(SETTINGS_DEFAULTS, defaults + "SMOKE_SETTINGS=defaults\nSMOKE_DEFAULTS=defaults\n")
        probe = "import os; print(*(os.environ.get(name) for name in ('SMOKE_ENV', 'SMOKE_SETTINGS', 'SMOKE_DEFAULTS')))"
        done = run(["bash", ".harness/hooks/python.cmd", "-c", probe], self.repo, {"SMOKE_ENV": "env"})
        self.check(msg("smoke_settings_order"), done.stdout.split() == ["env", "settings", "defaults"])
        (self.repo / SETTINGS).write_text(defaults, encoding="utf-8", newline="\n")
        check, release = self.read(".github/workflows/check.yml"), self.read(".github/workflows/release.yml")
        self.check(msg("smoke_ci_seeds"), "uses: ./.github/workflows/project-check.yml" in check
                   and "uses: ./.github/workflows/project-deploy.yml" in release
                   and (self.repo / ".github/workflows/project-check.yml").is_file())
        self.write(".harness/project/messages.env", "MSG_gate_settings_read='smoke-project-message'\n")
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat " + SETTINGS}})
        done = run(["bash", "-c", f"printf '%s' '{payload}' | .harness/hooks/python.cmd .harness/hooks/agent-gate.py claude tool"],
                   self.repo)
        self.check(msg("smoke_settings_denied"), "smoke-project-message" in done.stdout and '"deny"' in done.stdout
                   and "Read(./" + SETTINGS + ")" in self.read(".claude/settings.json"))
        project = self.read_json(PROJECT)
        self.write_json(PROJECT, {**project, "setup": {"session": ["touch smoke-session-first", "test -f smoke-session-first && touch smoke-session-step"]}})
        run(["bash", ".harness/provisioning/scripts/ensure-repo-deps.sh", "--only-steps"], self.repo)
        self.check(msg("smoke_session_steps"), (self.repo / "smoke-session-step").is_file())
        self.restore()

    def legacy(self) -> None:
        renamed = {new: old for old, new in (*LEGACY_PROJECT, *LEGACY_SEEDS)}
        for old, new in (*LEGACY_PROJECT, *LEGACY_SEEDS):
            if (self.repo / new).is_file():
                (self.repo / old).parent.mkdir(parents=True, exist_ok=True)
                (self.repo / new).rename(self.repo / old)
                self.write(old, self.read(old).replace(new, old))
        rewrite_lock(self.repo, lambda path: renamed.get(path, path))
        lock = self.read_json(LOCK)
        project = self.read_json(LEGACY_MANIFEST)
        project["modules"]["agents"].setdefault("settings", {})["sessionSteps"] = ["tools/smoke-step"]
        broken = copy.deepcopy(project)
        broken["modules"]["forge"]["extensions"]["github"]["repo"] = ""
        self.write_json(LEGACY_MANIFEST, broken)
        self.commit("chore: smoke-legacy-layout")
        old = [old for old, _ in (*LEGACY_PROJECT, *LEGACY_SEEDS) if (self.repo / old).is_file()]
        self.expect(msg("smoke_legacy_refused"), self.apply(), 2, reason="forge/github.repo")
        self.check(msg("smoke_legacy_rollback"), all((self.repo / path).is_file() for path in old)
                   and not (self.repo / PROJECT).exists() and self.read_json(LOCK) == lock)
        self.write_json(LEGACY_MANIFEST, project)
        legacy_answers = LEGACY_PROJECT[1][0]
        synced = self.read_json(legacy_answers)
        synced["manifest"] = sha256((self.repo / LEGACY_MANIFEST).read_bytes())
        self.write_json(legacy_answers, synced)
        self.commit("chore: smoke-legacy-steps")
        self.expect(msg("smoke_legacy_apply"), self.apply())
        moved = self.read_json(LOCK)["files"]
        deploy = ".github/workflows/project-deploy.yml"
        self.check(msg("smoke_legacy_moved"), not any((self.repo / path).exists() for path in old)
                   and all(renamed_path in moved for renamed_path in (AREA_MAP, ".github/workflows/project-deploy.yml"))
                   and "sessionSteps" not in self.read_json(PROJECT)["modules"]["agents"].get("settings", {})
                   and self.read_json(PROJECT).get("setup", {}).get("session") == ["bash tools/smoke-step"]
                   and ".github/workflows/deploy.yml" not in self.read(deploy) and deploy in self.read(deploy))
        self.expect(msg("smoke_legacy_verify"), self.harness("verify", "--no-gates"))
        self.commit("chore: smoke-legacy-moved")
        self.wrappers()

    def wrappers(self) -> None:
        """Обертка прежнего каталога шагов из одной строки exec встает командой в список манифеста, остальное называет verify."""
        moved, kept = SETUP_WRAPPERS + "/snapshot/01-docker.sh", SETUP_WRAPPERS + "/session/02-two.sh"
        self.write(moved, '#!/usr/bin/env bash\nexec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)/tools/smoke docker.sh" "$@"\n')
        self.write(kept, "#!/usr/bin/env bash\ncd tools\nexec bash smoke-step\n")
        self.write(SETUP_WRAPPERS + "/session/01-one.sh", "exec bash tools/smoke-one.sh\n")
        self.expect(msg("smoke_wrappers_apply"), self.apply(), reason=moved)
        setup = self.read_json(PROJECT).get("setup", {})
        self.check(msg("smoke_wrappers_moved"), setup.get("snapshot") == ["bash 'tools/smoke docker.sh'"]
                   and setup.get("session") == ["bash tools/smoke-step"]
                   and not (self.repo / moved).exists() and (self.repo / kept).is_file()
                   and (self.repo / SETUP_WRAPPERS / "session/01-one.sh").is_file())
        self.expect(msg("smoke_wrappers_verify"), self.harness("verify", "--no-gates"), 1, reason=kept)
        self.check(msg("smoke_wrappers_synced"), self.read_json(ANSWERS)["manifest"] == sha256((self.repo / PROJECT).read_bytes()))
        self.restore()

    def line_endings(self) -> None:
        """Последнее правило .gitattributes побеждает: блок комплекта уходит под правило проекта * eol=crlf, и
        файлы комплекта остаются с LF, а файлы проекта - с правилом проекта."""
        self.write(".gitattributes", self.read(".gitattributes") + "\n* text=auto eol=crlf\n")
        self.commit("chore: smoke-project-eol")
        self.expect(msg("smoke_apply_again"), self.apply())
        kit = [".harness/scripts/check/check-all.py", ".githooks/pre-push", ".harness/hooks/session-start.sh"]
        done = run(["git", "check-attr", "eol", "--", *kit, "README.md"], self.repo).stdout.splitlines()
        self.check(msg("smoke_eol_kit"), done == [f"{path}: eol: lf" for path in kit] + ["README.md: eol: crlf"]
                   and self.read(".gitattributes").rstrip().endswith(markers(".gitattributes", "core")[1]))
        self.write(".gitattributes", self.read(".gitattributes").replace("* text=auto eol=crlf\n", ""))
        self.commit("chore: smoke-kit-eol")

    def windows_paths(self) -> None:
        """Путь Windows с буквой диска вместо пути от корня отклоняют и ответ настройки, и манифест."""
        converted = "C:/Users/smoke/AppData/Local/Programs/Git/node_modules/"
        self.seed({"docs.generatedSegments": converted})
        self.expect(msg("smoke_msys_answer"), self.walker(self.installed), 2, reason=WINDOWS_REASON)
        self.restore()
        project = self.read_json(PROJECT)
        project["docs"]["generatedSegments"] = [converted]
        self.write_json(PROJECT, project)
        self.expect(msg("smoke_msys_manifest"), self.apply(), 2, reason=WINDOWS_REASON)
        self.restore()

    def violations(self) -> None:
        self.line_endings()
        self.windows_paths()
        # На Windows chmod бит не снимает, и сценарий там ничего не проверил бы.
        if POSIX_MODES:
            hook = ".githooks/pre-push"
            (self.repo / hook).chmod(0o644)
            self.expect(msg("smoke_apply_again"), self.apply())
            self.check(msg("smoke_exec_restored"), os.access(self.repo / hook, os.X_OK) and self.clean())
        hook = ".claude/hooks/session-start.sh"
        run(["git", "config", "core.fileMode", "false"], self.repo)
        run(["git", "update-index", "--chmod=-x", "--", hook], self.repo)
        self.expect(msg("smoke_apply_again"), self.apply())
        mode = index_modes(self.repo).get(hook)
        run(["git", "config", "--unset", "core.fileMode"], self.repo)
        self.check(msg("smoke_index_mode_restored"), mode == "100755" and self.clean())
        release = ".github/workflows/release.yml"
        run(["git", "rm", "-q", "--cached", "--", release], self.repo)
        self.write(".git/info/exclude", "[Rr]elease.yml\n")
        done = run(self.harness("verify", "--no-gates"), self.repo)
        self.check(msg("smoke_kit_excluded_locally"), "info/exclude" not in done.stdout + done.stderr)
        self.write(".gitignore", self.read(".gitignore") + "[Rr]elease.yml\n")
        self.expect(msg("smoke_kit_ignored"), self.harness("verify", "--no-gates"), -1, reason=release)
        self.write(".git/info/exclude", "")
        self.restore()
        workflow = ".github/workflows/check.yml"
        self.write(workflow, self.read(workflow) + "# smoke-managed-edit\n")
        self.expect(msg("smoke_managed_gate"), [sys.executable, ".harness/scripts/check/check-harness-files.py"], -1,
                    reason="project-check.yml")
        self.expect(msg("smoke_managed_verify"), self.harness("verify", "--no-gates"), -1, reason="project-check.yml")
        self.restore()
        self.write(EDITED, retitle(self.read(EDITED), "smoke-project"))
        self.commit("docs: smoke-project-edit")
        self.expect(msg("smoke_apply_again"), self.apply())
        self.check(msg("smoke_edit_overwritten"), self.read(EDITED) == (self.installed / "payload" / EDITED).read_text(encoding="utf-8"))
        self.commit("chore: smoke-kit-edit")
        gate = ".harness/scripts/check/check-adr-refs.py"
        (self.repo / gate).unlink()
        self.expect(msg("smoke_missing_gate"), self.harness("verify", "--no-gates"), -1, reason=gate)
        self.restore()
        manifest = self.read_json(PROJECT)
        manifest["rules"] = {"disabled": [{"id": "adr-reference"}]}
        self.write_json(PROJECT, manifest)
        self.expect(msg("smoke_silent_disable"), self.harness("verify", "--no-gates"), -1, reason="adr-reference")
        self.restore()
        manifest = self.read_json(PROJECT)
        self.write("tools/smoke-rule.py", "print('smoke')\n")
        manifest["rules"] = {"own": [{"id": "smoke-project-rule", "title": "smoke", "doc": "README.md",
                                      "gate": "tools/smoke-rule.py", "command": [sys.executable, "tools/smoke-rule.py"]}]}
        self.write_json(PROJECT, manifest)
        self.expect(msg("smoke_project_rule"), [sys.executable, ".harness/scripts/check/check-all.py", "--base", self.base, "--list"],
                    reason="smoke-project-rule")
        self.restore()

    def update(self) -> None:
        version = following(self.kit)
        newer = variant(self.kit, self.work, version, lambda text: text + "\nsmoke-kit-line\n")
        self.write(EDITED, retitle(self.read(EDITED), "smoke-project"))
        self.commit("docs: smoke-project-edit")
        self.upgrade(msg("smoke_update"), newer)
        self.check(msg("smoke_update_kit_version"), self.read(EDITED) == (newer / "payload" / EDITED).read_text(encoding="utf-8"))
        changed = run(["git", "status", "--porcelain", "--untracked-files=all"], self.repo).stdout.splitlines()
        self.check(msg("smoke_update_lock_only"), [line[3:] for line in changed] == [LOCK])
        self.commit(f"chore: harness {version}")
        self.expect(msg("smoke_apply_again"), self.apply(newer))
        self.check(msg("smoke_idempotent"), self.clean())
        rebuilt = variant(newer, self.work / "rebuilt", version, lambda text: text)
        shipped = json.loads((rebuilt / "kit.json").read_text(encoding="utf-8"))
        shipped["digest"] = sha256(b"smoke-rebuilt")
        (rebuilt / "kit.json").write_text(json_text(shipped), encoding="utf-8", newline="\n")
        notice = kit_msg("apply_same_version", version, "\0", "\0").split("\0")[0]
        self.upgrade(msg("smoke_same_version"), rebuilt, reason=notice)
        self.commit(f"chore: harness {version} rebuilt")
        self.reconfigure()

    def reconfigure(self) -> None:
        """Правка манифеста без apply - отказ verify и гейта конвейера; apply рендерит новое значение."""
        project = self.read_json(PROJECT)
        project["git"]["releaseBranch"] = "smoke-release"
        self.write_json(PROJECT, project)
        moved = kit_msg("verify_manifest_moved", "\0", "\0", "\0").split("\0")[1]
        self.expect(msg("smoke_manifest_moved"), self.harness("verify", "--no-gates"), -1, reason=moved)
        self.expect(msg("smoke_manifest_moved_gate"), [sys.executable, ".harness/scripts/check/check-pipeline.py"], -1,
                    reason=PROJECT)
        self.expect(msg("smoke_apply_again"), self.apply(self.installed))
        self.check(msg("smoke_reconfigure"), "smoke-release" in self.read(".harness/docs/ci-cd.md"))
        self.restore()

    def node(self) -> None:
        app, path = self.repo / "apps/web", "apps/web/eslint.config.js"
        gate = [sys.executable, ".harness/scripts/check/check-node-adapter.py"]
        self.plant("leak.ts", "apps/web/src/domain/leak.ts")
        self.expect(msg("smoke_node_boundary"), ["npm", "run", "check"], -1, app, "no-restricted-imports")
        self.restore()
        config = self.read(path)
        weakened = '...required, "@typescript-eslint/no-misused-promises": ["error", { checksVoidReturn: false }]'
        replaced = '  { files: ["src/**/*.ts"], rules: { "no-restricted-imports": ["error", { paths: ["fs"] }] } },\n);'
        narrow = '  { files: ["src/domain/price.ts"], rules: { "no-restricted-imports": "off" } },\n);'
        for name, old, new, reason in (
            (msg("smoke_node_wiring"), "...required", "", "no-floating-promises"),
            (msg("smoke_node_options"), "...required", weakened, "no-misused-promises"),
            (msg("smoke_node_imports"), "\n);", "\n" + replaced, "no-restricted-imports"),
            (msg("smoke_node_layer_file"), "\n);", "\n" + narrow, "apps/web/src/domain/price.ts"),
            (msg("smoke_node_layers"), "    layers: [", "    layers: [], unused: [", "layerBoundaries"),
            (msg("smoke_node_source"), 'name: "domain"', 'name: "domian"', "apps/web/src/domian/"),
        ):
            self.write(path, config.replace(old, new))
            self.expect(name, gate, -1, reason=reason)
            self.restore()
        # Гейт видит исходники по индексу git: незакоммиченный файл добавляется в него на время пробы.
        lost = "apps/web/src/domain/lost.mts"
        self.write(path, config.replace("src/**/*.{ts,tsx,mts,cts}", "src/**/*.ts"))
        self.plant("lost.mts", lost)
        run(["git", "add", "--", lost], self.repo)
        self.expect(msg("smoke_node_mts"), gate, -1, reason=lost)
        self.restore()

    def dotnet(self) -> None:
        """Проект .NET без своих анализаторов: журнал SARIF блока комплекта ловит предупреждение компилятора."""
        solution = self.repo / "src"
        self.plant("Unused.cs", "src/Modules/Sample/Sample.Domain/Shared/Services/Unused.cs")
        run(["dotnet", "build", "Sample.slnx", "-c", "Release"], solution)
        self.expect(msg("smoke_dotnet_diagnostics"), [sys.executable, ".harness/scripts/check/check-build-diagnostics.py",
                                                       "--configuration", "Release"], -1, reason="CS0168")
        self.restore()
        targets = "src/Directory.Build.targets"
        text = self.read(targets)
        start, stop = block_of(text, *markers(targets, "dotnet"))
        self.write(targets, text[:start] + text[stop:])
        self.expect(msg("smoke_apply_again"), self.apply(self.installed))
        self.check(msg("smoke_dotnet_wiring"), block_of(self.read(targets), *markers(targets, "dotnet")) is not None)
        self.restore()

    def without_ci(self) -> None:
        """Проект без CI: поставщик кода выключен с причиной, модули GitHub неприменимы, приемка идет без их гейтов."""
        self.bootstrap()
        answers = ("shape=backend", "modules.backend=none", SMOKE_ANSWERS[0], "modules.forge=off",
                   "modules.forge.off=" + msg("smoke_no_forge_reason"))
        if self.configure(msg("smoke_no_ci_configure"), *answers) \
                and self.expect(msg("smoke_no_ci_apply"), self.apply()):
            self.check(msg("smoke_notify_absent"), not (self.repo / ".harness/notify").exists())
            self.commit("chore: harness")
            self.expect(msg("smoke_no_ci_verify"), self.harness("verify"))

    def gitlab_jira(self) -> None:
        """Код и CI в GitLab, задачи в Jira: файлы GitHub не ставятся, ветка и запрос называют задачу ключом Jira."""
        self.bootstrap("git@gitlab.example.com:acme/platform/smoke.git")
        answers = ("shape=backend", "modules.backend=none", SMOKE_ANSWERS[0], "modules.tracker=jira",
                   "modules.tracker.extensions.jira.url=https://example.atlassian.net",
                   "modules.tracker.extensions.jira.projects=PROJ", *GITLAB_ANSWERS)
        if not (self.configure(msg("smoke_gitlab_configure"), *answers)
                and self.expect(msg("smoke_gitlab_apply"), self.apply())):
            return
        include = self.read(".gitlab-ci.yml")
        self.check(msg("smoke_gitlab_files"), (self.repo / GITLAB_CI).is_file()
                   and not (self.repo / ".github" / "workflows").exists()
                   and all(f"local: .gitlab/ci/{name}.yml" in include for name in GITLAB_PIPELINES))
        self.check(msg("smoke_gitlab_runners"), 'tags: ["docker"]' in self.read(".gitlab/ci/review-round.yml")
                   and "tags: []" in self.read(GITLAB_CI))
        self.check(msg("smoke_stand_servers"), self.agent_mcp("production", url=SMOKE_STAND_URL))
        self.check(msg("smoke_mcp_servers"), self.agent_mcp("context7", url=SMOKE_MCP_URL))
        self.check(msg("smoke_lsp_absent"), not self.agent_mcp("lsp", every=False)
                   and "lsp" not in self.read_json(".mcp.json").get("mcpServers", {})
                   and not (self.repo / LSP_CONFIG).exists())
        self.commit("chore: harness")
        self.expect(msg("smoke_gitlab_verify"), self.harness("verify"))
        self.gitlab_workflow()
        link = [sys.executable, ".harness/scripts/check/check-pr-issue-link.py"]
        branch = {"PR_BRANCH": "feat/PROJ-12-login", "PR_NUMBER": ""}
        self.expect(msg("smoke_jira_unlinked"), link, -1, reason="PROJ-12", env={**branch, "PR_BODY": "Closes OTHER-3"})
        self.expect(msg("smoke_jira_linked"), link, env={**branch, "PR_BODY": "Closes PROJ-12"})
        run(["git", "checkout", "-q", "-b", "feat/PROJ-7-cart"], self.repo)
        self.write(".git/smoke-message", "feat: smoke\n")
        run(["bash", ".githooks/commit-msg", ".git/smoke-message"], self.repo)
        self.check(msg("smoke_jira_trailer"),
                   "Issue: https://example.atlassian.net/browse/PROJ-7" in self.read(".git/smoke-message"))
        shutil.copytree(KIT / "tests" / "platform", self.repo / ".harness" / "tests", dirs_exist_ok=True)
        self.expect(msg("smoke_jira_close"), [sys.executable, ".harness/tests/tracker-close.py"])
        self.expect(msg("smoke_gitlab_change"), [sys.executable, ".harness/tests/gitlab-change.py"])
        plan = run([sys.executable, ".harness/scripts/pipeline/pipeline-run.py", "plan", "release"], self.repo)
        self.check(msg("smoke_gitlab_release_plan"), plan.returncode == 0 and ".gitlab/ci/harness.yml" in plan.stdout
                   and ".gitlab/ci/release.yml" in plan.stdout and ".github/" not in plan.stdout)

    def gitlab_workflow(self) -> None:
        """Свой workflow проекта в файле include перекрывает workflow комплекта: apply называет правила, которые
        проект переносит в начало своих rules, check-pipeline.py отказывает без них и принимает с ними."""
        own = ".gitlab/ci/project.yml"
        rules = '  rules:\n    - if: $CI_PIPELINE_SOURCE == "merge_request_event"\n    - when: never\n'
        self.write(own, "workflow:\n" + rules)
        self.write(".gitlab-ci.yml", self.read(".gitlab-ci.yml").rstrip("\n")
                   + f"\n  - local: {own}\n    rules:\n      - if: $HARNESS_PIPELINE == null\n")
        self.commit("ci: smoke-project-workflow")
        gate = [sys.executable, ".harness/scripts/check/check-pipeline.py"]
        self.expect(msg("smoke_gitlab_workflow_named"), self.apply(), reason="HARNESS_RELEASE")
        self.expect(msg("smoke_gitlab_workflow_lost"), gate, -1, reason=own)
        kit = "\n".join(self.read(".gitlab/ci/harness.yml").split("workflow:\n", 1)[1].split("\n\n", 1)[0]
                        .splitlines()[1:])
        kept = [line for line in kit.splitlines() if line.strip() != "- when: always"]
        self.write(own, "workflow:\n  rules:\n" + "\n".join(kept) + "\n" + rules.split("\n", 1)[1])
        self.expect(msg("smoke_gitlab_workflow_kept"), gate)
        self.restore()

    def sided(self, shape: str, absent: tuple[str, ...]) -> None:
        """Проект одной стороны: форма найдена по дереву, адаптер и гейты стека другой стороны не ставятся."""
        self.bootstrap()
        agents = ("cursor",) if shape == "frontend" else ("claude-code", "codex")
        answers = ["modules.agents=" + ",".join(agents)]
        if not (self.configure(msg("smoke_side_configure", shape), *answers)
                and self.expect(msg("smoke_side_apply", shape), self.apply())):
            return
        lock = self.read_json(LOCK)
        self.check(msg("smoke_side_absent", shape), all(not (self.repo / path).exists() and path in lock["absent"]
                                                        for path in absent))
        self.check(msg("smoke_side_lsp", shape),
                   [server["id"] for server in self.read_json(LSP_CONFIG)["servers"]] == SIDE_LSP[shape]
                   and self.agent_mcp("lsp") and ("cursor" not in agents or (self.repo / ".cursor" / "cli.json").is_file())
                   and ("claude-code" not in agents
                        or self.read_json(".claude/settings.json")["env"]["MCP_TIMEOUT"] == "300000"))
        if "node" in self.adapters:
            config = self.read("apps/web/eslint.config.js")
            self.write("apps/web/eslint.config.js", "export default [];\n")
            self.expect(msg("smoke_node_remediation"), self.apply(), reason="apps/web): eslint.config")
            self.write("apps/web/eslint.config.js", config)
        self.commit("chore: harness")
        self.expect(msg("smoke_side_verify", shape), self.harness("verify"))

    def built(self, kit: Path, name: str) -> Path:
        """Архив сборки, как его собирает kit.py build: каталог комплекта и tar.gz рядом."""
        folder = self.work / "archives" / name / f"agent-harness-{name}"
        shutil.copytree(kit, folder)
        return archive(folder)

    def locked(self) -> None:
        """Установка по lock: apply --archive кладет архив в кеш, загрузчик ставит версию из кеша без архива и
        сети, а у каждой ветки своя версия."""
        loader = [sys.executable, str(self.repo / LOADER), "install"]
        self.bootstrap()
        if not self.configure(msg("smoke_configure"), *self.answers()) or not self.expect(msg("smoke_apply"), self.apply()):
            return
        first = self.archive_of(self.kit)
        self.commit("chore: harness")
        self.check(msg("smoke_lock_sha"), self.read_json(LOCK)["kit"].get("tree") == archive_build(first)[2]
                   and set(self.read_json(LOCK)["kit"]) <= {"name", "version", "repository", "commit", "tree"})
        (self.repo / EDITED).unlink()
        self.expect(msg("smoke_lock_cache"), loader)
        self.check(msg("smoke_lock_restored"), self.clean() and (self.repo / ".harness/kit/ah.py").is_file())
        newer = variant(self.kit, self.work, following(self.kit), lambda text: text + "\nsmoke-kit-line\n")
        run(["git", "switch", "-qc", "newer"], self.repo)
        self.expect(msg("smoke_apply"), self.apply(newer))
        self.commit("chore: harness newer")
        self.expect(msg("smoke_lock_branch"), ["git", "switch", "-q", "develop"])
        older = not self.read(EDITED).endswith("smoke-kit-line\n") and self.linked()
        self.expect(msg("smoke_lock_branch"), ["git", "switch", "-q", "newer"])
        self.check(msg("smoke_lock_versions"), older and self.read(EDITED).endswith("smoke-kit-line\n") and self.linked()
                   and self.clean())
        read = run([sys.executable, "-c", "import sys; sys.path.insert(0, '.harness/scripts'); import ci_common; "
                    f"sys.stdout.write(ci_common.base_read('develop', {EDITED!r}).decode())"], self.repo)
        self.check(msg("smoke_base_reader"), read.returncode == 0 and bool(read.stdout)
                   and "smoke-kit-line" not in read.stdout)
        self.expect(msg("smoke_lock_branch"), ["git", "switch", "-q", "develop"])
        self.released(newer)

    def released(self, newer: Path) -> None:
        """Выпуск площадки - каталог file://: загрузчик скачивает архив версии lock в пустой кеш, а upgrade ставит
        следующую версию и переводит ссылки проекта на справочники на ее тег."""
        releases = self.work / "releases"
        lock = self.read_json(LOCK)
        lock["kit"]["repository"] = releases.as_uri()
        for kit in (self.kit, newer):
            shipped = json.loads((kit / "kit.json").read_text(encoding="utf-8"))
            published = releases / loader().release({**lock["kit"], "version": shipped["version"]}).removeprefix(releases.as_uri() + "/")
            published.parent.mkdir(parents=True, exist_ok=True)
            data = self.archive_of(kit).read_bytes()
            published.write_bytes(data)
            published.with_name(published.name + ".sha256").write_text(f"{sha256(data)}  {published.name}\n")
        version = shipped["version"]
        self.write_json(LOCK, lock)
        self.commit("chore: harness releases")
        empty = {"AGENT_HARNESS_CACHE": str(self.work / "download-cache")}
        self.expect(msg("smoke_release_download"), [sys.executable, str(self.repo / LOADER), "install"], env=empty)
        self.check(msg("smoke_release_cached"), (self.work / "download-cache" / lock["kit"]["tree"] / "ah.py").is_file())
        self.expect(msg("smoke_upgrade"), self.harness("upgrade", "--version", version, "--no-pr"))
        self.check(msg("smoke_upgraded"), self.read_json(LOCK)["kit"]["version"] == version
                   and self.read(EDITED).endswith("smoke-kit-line\n"))
        self.expect(msg("smoke_upgrade_current"), self.installer(newer) + ["upgrade", "--target", str(self.repo),
                                                                          "--new-archive", str(self.archive_of(newer)), "--no-pr"],
                    reason=kit_msg("upgrade_current", version, "\0").split("\0")[0])

    def committed(self) -> None:
        """Установка по коммиту без выпуска: lock закрепляет коммит репозитория комплекта, загрузчик собирает
        комплект из его исходника в пустой кеш, а upgrade --commit ставит следующий коммит ветки и переводит
        ссылки проекта на справочники на него."""
        source, out = self.work / "source", self.work / "source-build"
        source.mkdir(parents=True)
        run(["git", "init", "-q", "-b", "develop"], source)
        listed = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], KIT).stdout.split("\0")
        for path in filter(None, listed):
            if (KIT / path).is_file():
                (source / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(KIT / path, source / path)
        run(["git", "add", "-A"], source)
        run(["git", "commit", "-qm", "kit"], source)
        first = self.commit_build(source, out)
        self.bootstrap()
        if not self.configure(msg("smoke_configure"), *self.answers(), kit=first) or not self.expect(msg("smoke_apply"), self.apply(first)):
            return
        self.installed = first
        lock = self.read_json(LOCK)
        old = lock["kit"].get("commit", "")
        self.check(msg("smoke_commit_pinned"), old == run(["git", "rev-parse", "HEAD"], source).stdout.strip())
        missing = (self.work / "missing-kit").as_uri()
        lock["kit"]["repository"] = missing
        self.write_json(LOCK, lock)
        self.commit("chore: harness")
        head = run(["git", "symbolic-ref", "HEAD"], self.repo).stdout
        hook = {"AGENT_HARNESS_CACHE": str(self.work / "commit-cache"), "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "true",
                "GIT_CONFIG_KEY_1": f"url.{source.as_uri()}.insteadOf", "GIT_CONFIG_VALUE_1": missing,
                "GIT_DIR": str(self.repo / ".git"), "GIT_WORK_TREE": str(self.repo)}
        self.expect(msg("smoke_commit_built"), [sys.executable, str(self.repo / LOADER), "install"], env=hook)
        self.check(msg("smoke_commit_hook"), self.clean() and not (self.repo / ".git" / "shallow").exists()
                   and run(["git", "symbolic-ref", "HEAD"], self.repo).stdout == head)
        self.check(msg("smoke_commit_cached"), (self.work / "commit-cache" / lock["kit"]["tree"] / "ah.py").is_file())
        edited = source / "payload" / EDITED
        edited.write_text(edited.read_text(encoding="utf-8") + "\nsmoke-kit-line\n", encoding="utf-8", newline="\n")
        run(["git", "commit", "-qam", "kit next"], source)
        new = run(["git", "rev-parse", "HEAD"], source).stdout.strip()
        lock = self.read_json(LOCK)
        lock["kit"]["repository"] = source.as_uri()
        self.write_json(LOCK, lock)
        self.expect(msg("smoke_commit_upgrade"), self.harness("upgrade", "--commit", "develop", "--no-pr"))
        self.check(msg("smoke_commit_upgraded"), self.read_json(LOCK)["kit"].get("commit") == new
                   and self.read(EDITED).endswith("smoke-kit-line\n"))
        lock = self.read_json(LOCK)
        lock["kit"]["repository"] = source.as_uri()
        self.write_json(LOCK, lock)
        self.commit("chore: harness next")
        self.expect(msg("smoke_commit_current"), self.harness("upgrade", "--commit", "develop", "--no-pr"),
                    reason=kit_msg("upgrade_current", "\0", "\0").split("\0")[0])

    def commit_build(self, source: Path, out: Path) -> Path:
        """Сборка kit.py build --commit из чистого checkout: архив несет коммит, а каталог распакован рядом."""
        run([sys.executable, str(source / "tools" / "kit.py"), "build", "--out", str(out), "--commit"], source)
        (packed,) = out.glob("*.tar.gz")
        unpacked = out / "unpacked"
        unpack(packed, unpacked)
        (folder,) = unpacked.iterdir()
        self.archives[folder] = packed
        return folder

    def initialized(self) -> None:
        """init в пустом проекте без терминала: вопрос без ответа и без значения по умолчанию - отказ с его id; с
        ответами в answers.json - оболочка, манифест, lock и версия в кеше; повторный init ничего не меняет."""
        self.bootstrap()
        command = self.installer() + ["init", "--archive", str(self.archive_of(self.kit)), "--target", str(self.repo)]
        self.expect(msg("smoke_init_unanswered"), command, 2, reason="shape")
        self.seed(dict(pair.split("=", 1) for pair in self.answers()))
        if not self.expect(msg("smoke_init"), command):
            return
        self.check(msg("smoke_init_state"), self.read_json(LOCK)["kit"].get("tree") == archive_build(self.archive_of(self.kit))[2]
                   and self.linked() and (self.repo / PROJECT).is_file()
                   and self.read_json(ANSWERS)["answers"].get("project.name", {}).get("by") == "detected"
                   and all(self.read_json(ANSWERS)["answers"].get(key, {}).get("by") == "default"
                           for key in ("agents.cli", "agents.tools"))
                   and self.read_json(ANSWERS)["answers"].get("modules.forge", {}).get("by") == "detected")
        self.commit("chore: harness")
        if self.expect(msg("smoke_init_again"), command):
            self.check(msg("smoke_init_idempotent"), self.clean())
        self.seed({"modules.githooks": "commit-msg"})
        change = self.installer() + ["change", "--target", str(self.repo)]
        kept = self.expect(msg("smoke_change_unattended"), change) and self.expect(msg("smoke_change_unattended"), change)
        self.check(msg("smoke_change_owner"), kept and self.owner_kept())
        state = self.read_json(ANSWERS)
        state["reopened"], state["alone"] = {"modules.githooks": ["off"]}, ["modules.githooks"]
        state["answers"].pop("modules.githooks")
        self.write_json(ANSWERS, state)
        before = self.read(ANSWERS), self.read(PROJECT)
        self.expect(msg("smoke_change_reopened"), change, 2, reason="modules.githooks")
        self.check(msg("smoke_change_reopened_kept"), (self.read(ANSWERS), self.read(PROJECT)) == before
                   and list(self.read_json(PROJECT)["modules"]["githooks"]["extensions"]) == ["commit-msg"])

    def owner_kept(self) -> bool:
        """Решение владельца не по значению каталога остается его ответом и попадает в манифест."""
        stored = self.read_json(ANSWERS)["answers"]
        manifest = self.read_json(PROJECT)["modules"]["githooks"]["extensions"]
        return stored.get("modules.githooks") == {"value": ["commit-msg"], "by": "owner"} and list(manifest) == ["commit-msg"]

    def prompted(self) -> None:
        """Вопросы в терминале: questionary ставится в кеш, выбор, отметки и сводный вопрос дают ответы владельца."""
        self.bootstrap()
        last = "\x1b[A \r"
        keys = {"modules.agents": " \r", "modules.backend": last, "modules.frontend": last, "defaults": " \r"}
        found = self.prompter(keys, "init")
        stored = self.read_json(ANSWERS)["answers"] if (self.repo / ANSWERS).is_file() else {}
        asked = found.get("asked", [])
        dropped = asked[asked.index("defaults") + 1] if "defaults" in asked[:-1] else ""
        self.check(msg("smoke_prompt"), bool(found) and not found["problems"] and (self.repo / PROJECT).is_file()
                   and stored.get("modules.agents") == {"value": ["claude-code"], "by": "owner"}
                   and stored.get("shape", {}).get("by") == "owner")
        self.check(msg("smoke_prompt_summary"), bool(dropped) and dropped in stored and stored[dropped]["by"] == "owner")
        self.seed({"modules.githooks": "commit-msg"})
        self.prompter({}, "init")
        manifest = self.read(PROJECT) if (self.repo / PROJECT).is_file() else ""
        changed = self.prompter({}, "change")
        separate = set(changed.get("items", [])) & set(changed.get("asked", []))
        self.check(msg("smoke_prompt_change"), bool(changed) and {"stack", "defaults"} <= set(changed["asked"])
                   and not separate and self.read(PROJECT) == manifest and self.owner_kept())

    def prompter(self, keys: dict, command: str) -> dict:
        done = run([sys.executable, "-B", "-c", PROMPTER, str(self.kit), str(self.repo), json.dumps(keys), command], self.repo)
        found = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 and done.stdout.strip() else {}
        if not found:
            print(done.stdout + done.stderr, flush=True)
        return found

    def scripted(self) -> None:
        """install.sh выпуска: скачивает архив версии, сверяет sha256 и ставит проект через ah init."""
        self.bootstrap()
        shipped = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        releases = (self.work / "script-releases").as_uri()
        published = Path(loader().release({**shipped, "repository": releases}).removeprefix("file://"))
        published.parent.mkdir(parents=True, exist_ok=True)
        data = self.archive_of(self.kit).read_bytes()
        published.write_bytes(data)
        checksum = published.with_name(published.name + ".sha256")
        env = {"AGENT_HARNESS_NAME": shipped["name"], "AGENT_HARNESS_VERSION": shipped["version"],
               "AGENT_HARNESS_REPOSITORY": releases}
        checksum.write_text(f"{'0' * 64}  {published.name}\n")
        self.expect(msg("smoke_script_sha"), ["bash", str(KIT / "install.sh")], 2, reason="sha256", env=env)
        checksum.write_text(f"{sha256(data)}  {published.name}\n")
        self.seed(dict(pair.split("=", 1) for pair in self.answers()))
        if self.expect(msg("smoke_script"), ["bash", str(KIT / "install.sh")], env=env):
            self.check(msg("smoke_script_state"), self.linked() and (self.repo / PROJECT).is_file())

    def linked(self) -> bool:
        """Каталог комплекта ведет на версию в кеше, которую называет lock.json текущей ветки."""
        pinned = Path(ENV["AGENT_HARNESS_CACHE"]) / self.read_json(LOCK)["kit"]["tree"]
        return (self.repo / ".harness/kit").resolve() == pinned.resolve()

    def sequence(self) -> None:
        """Вопросы настройки для одного дерева идут в одном порядке и дают один манифест."""
        self.bootstrap()
        scripted = dict(pair.split("=", 1) for pair in self.answers())
        walks, shown = [], {}
        for _ in range(2):
            self.forget()
            walks.append(self.walk(scripted, shown=shown))
        self.check(msg("smoke_next_stable"), walks[0] is not None and walks[0] == walks[1])
        asked = walks[0][0] if walks[0] else []
        self.check(msg("smoke_next_static"), "defaults" in asked and not {"agents.cli", "agents.tools"} & set(asked))
        summary = shown.get("defaults", {})
        self.check(msg("smoke_next_show"), bool(summary.get("items")) and all(
            item["id"].startswith(("modules.", "git.")) for item in summary["items"]))
        self.forget()
        revised = self.walk(scripted, summaries={"defaults": "modules.githooks,modules.agents.settings.pushback"})
        order = revised[0] if revised else []
        self.check(msg("smoke_next_exception"), "defaults" in order and set(order[order.index("defaults") + 1:][:2]) == {"modules.githooks", "modules.agents.settings.pushback"})
        stored = self.read_json(ANSWERS)["answers"].get("modules.alert-triage.settings.environment", {}) if revised else {}
        self.check(msg("smoke_next_empty"), stored == {"value": "", "by": "default"})
        kept = self.read_json(ANSWERS)["answers"].get("modules.agents.settings.pushback", {}) if revised else {}
        self.check(msg("smoke_next_setting"), kept.get("value") == "critical")

    def runner_roles(self) -> None:
        """Пул раннеров роли спрашивается только при включенной роли: без круга ревью, агента задач, разбора
        алертов и релиза в CI их метки не входят ни в отдельные вопросы, ни в пункты сводного вопроса."""
        self.bootstrap()
        scripted = dict(pair.split("=", 1) for pair in (
            "shape=backend", "modules.backend=none", SMOKE_ANSWERS[0], "modules.ci-agent=off",
            "modules.release=off", "modules.runners=github-actions"))
        shown: dict = {}
        walked = self.walk(scripted, shown=shown)
        asked = set(walked[0]) if walked else set()
        asked |= {item["id"] for question in shown.values() for item in question.get("items", [])}
        roles = {f"modules.runners.extensions.github-actions.{kind}" for kind in ("review", "agent", "triage", "release")}
        self.check(msg("smoke_runner_roles"), walked is not None and not roles & asked
                   and "modules.runners.extensions.github-actions.check" in asked)

    def forget(self) -> None:
        for path in (PROJECT, ANSWERS):
            (self.repo / path).unlink(missing_ok=True)

    def seed(self, scripted: dict) -> None:
        """Ответы сценария владелец записал в answers.json: init и change берут их оттуда."""
        path = self.repo / ANSWERS
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        state.setdefault("answers", {}).update({key: {"raw": value, "by": "owner"} for key, value in scripted.items()})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def walker(self, kit: Path | None = None, summaries: dict | None = None) -> list[str]:
        """Вопросы настройки без терминала, как их проходит init: ответ сценария или ответ вопроса по умолчанию."""
        return [sys.executable, "-B", "-c", WALKER, str(kit or self.kit), str(self.repo), json.dumps(summaries or {})]

    def walk(self, scripted: dict, kit: Path | None = None, summaries: dict | None = None,
             shown: dict | None = None) -> tuple[list[str], str] | None:
        """Ответы сценария в answers.json, на сводный вопрос - ответ summaries, на каждый следующий вопрос - его
        ответ по умолчанию. Итог - порядок и манифест."""
        self.seed(scripted)
        done = run(self.walker(kit, summaries), self.repo)
        found = json.loads(done.stdout) if done.returncode == 0 else {}
        if "asked" not in found:
            print(done.stdout + done.stderr, flush=True)
            return None
        stored = json.loads(self.read(ANSWERS)).get("answers", {})
        lost = [key for key in scripted if stored.get(key, {}).get("by") != "owner" or "value" not in stored[key]]
        if lost:
            print(msg("smoke_answers_lost", ", ".join(lost)), flush=True)
            return None
        if shown is not None:
            shown.update({key: {"items": value} for key, value in found["items"].items()})
        return found["asked"], self.read(PROJECT)

    def packed(self, full: bool) -> None:
        """Архив под решения: одни решения - один архив байт в байт, файлов стека другой стороны в нем нет,
        а настройка из него спрашивает только факты проекта."""
        self.work.mkdir(parents=True, exist_ok=True)
        config = self.work / "backend.json"
        config.write_text(json.dumps({"decisions": BACKEND_DECISIONS}), encoding="utf-8")
        archives = []
        for attempt in ("first", "second"):
            out = self.work / f"pack-{attempt}"
            if self.expect(msg("smoke_pack"), self.installer() + ["pack", "--config", str(config), "--out", str(out)], cwd=self.work):
                archives += sorted(out.glob("*.tar.gz"))
        digests = {(path.name, sha256(path.read_bytes())) for path in archives}
        self.check(msg("smoke_pack_reproducible"), len(archives) == 2 and len(digests) == 1)
        if not archives:
            return
        unpack(archives[0], self.work / "packed")
        kit = self.work / "packed" / archives[0].name.removesuffix(".tar.gz")
        self.check(msg("smoke_pack_trimmed"), not any((kit / "payload" / path).exists() for path in NODE_FILES)
                   and all((kit / "payload" / path).is_file() for path in DOTNET_FILES)
                   and all((kit / path).is_file() for path in (CONFIG, *GUIDES, "skills/harness-init/SKILL.md"))
                   and not any("{{" in (kit / guide).read_text(encoding="utf-8") for guide in GUIDES))
        self.adapters = ["dotnet"]
        self.bootstrap()
        first = self.walk({}, kit)
        detected = self.read_json(ANSWERS)["answers"].get("project.name", {}).get("by") if first else None
        self.check(msg("smoke_pack_facts"), first is not None and not set(first[0]) & set(BACKEND_DECISIONS)
                   and detected == "detected")
        self.forget()
        if self.configure(msg("smoke_pack_configure"), kit=kit) and self.expect(msg("smoke_pack_apply"), self.apply(kit)):
            self.installed = kit
            self.check(msg("smoke_pack_absent"), set(NODE_FILES) <= set(self.read_json(LOCK)["absent"]))
            if full:
                self.expect(msg("smoke_pack_verify"), self.harness("verify", "--no-gates"))


def smoke(kit_folder: Path, adapters: list[str], kit: dict) -> list[str]:
    with tempfile.TemporaryDirectory() as folder:
        ENV["AGENT_HARNESS_CACHE"] = str(Path(folder) / "cache")
        scenario = Smoke(kit_folder, adapters, Path(folder))
        if scenario.install():
            scenario.isolated(kit)
            scenario.gates()
            scenario.preservation()
            scenario.legacy_layout()
            scenario.extensions()
            scenario.legacy()
            scenario.violations()
            scenario.update()
            for adapter in adapters:
                getattr(scenario, adapter)()
        root = Path(folder)
        locked = Smoke(kit_folder, [], root / "lock")
        locked.locked()
        initialized = Smoke(kit_folder, [], root / "init")
        initialized.initialized()
        script = Smoke(kit_folder, [], root / "script")
        script.scripted()
        terminal = Smoke(kit_folder, [], root / "prompt")
        terminal.prompted()
        pinned = Smoke(kit_folder, [], root / "commit")
        pinned.committed()
        bare = Smoke(kit_folder, [], root / "no-ci")
        bare.without_ci()
        roles = Smoke(kit_folder, [], root / "roles")
        roles.runner_roles()
        steps = Smoke(kit_folder, adapters, root / "sequence")
        steps.sequence()
        packed = Smoke(kit_folder, [], root / "pack")
        packed.packed(bool(adapters))
        hosted = Smoke(kit_folder, [], root / "gitlab")
        hosted.gitlab_jira()
        sides = []
        for shape, adapter, absent in (("backend", "dotnet", NODE_FILES), ("frontend", "node", DOTNET_FILES))[:2 * bool(adapters)]:
            sides.append(Smoke(kit_folder, [adapter], root / shape))
            sides[-1].sided(shape, absent)
        return [problem for each in (scenario, locked, initialized, script, terminal, pinned, bare, roles, steps, packed, hosted, *sides) for problem in each.problems]


