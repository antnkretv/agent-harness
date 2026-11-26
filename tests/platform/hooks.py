#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / ".harness").is_dir():
    ROOT = ROOT.parent

SHELL_HOOKS = (
    ".claude/hooks/session-start.sh",
    ".harness/lib/hook.sh",
    ".harness/hooks/session-start.sh",
    ".harness/hooks/eslint-file.sh",
    ".harness/hooks/comments-file.sh",
)

# Хуки на общей рамке: пустой ввод обязан дать объясненный пропуск, а не отказ.
FILE_GATE_HOOKS = (
    ".harness/hooks/eslint-file.sh",
    ".harness/hooks/comments-file.sh",
)

SHARED_LINT = ".harness/hooks/forge-text-lint.py"
GATE = ".harness/hooks/agent-gate.py"
SESSION_START = ".harness/hooks/session-start.sh"
SESSION_ENV = ".harness/provisioning/scripts/session-env.sh"
NEXT_STEP = "./.harness/hooks/python.cmd .harness/scripts/pipeline/pipeline-run.py next change"
AGENT_RUN = ("HARNESS_AGENT_RUN",)
# Git-хук экспортирует адрес своего репозитория: с ним команды пробы ушли бы в настоящее дерево.
GIT_LOCATION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
SESSION_STUBS = ("ensure-vale.sh", "ensure-toolchain-profile.sh", "ensure-lsp.sh", "ensure-repo-deps.sh")


def fail(message: str) -> None:
    raise AssertionError(message)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def commands(entries: list[dict]) -> list[str]:
    return [str(entry.get("command", "")) for entry in entries]


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Общий запуск: кодировка и политика ошибок одни на все проверки."""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, **kwargs)


def expect_hook(entries: list[dict], needle: str, where: str) -> None:
    if not any(needle in item for item in commands(entries)):
        fail(f"{where} does not call {needle}")


def flatten(groups: list[dict]) -> list[dict]:
    return [hook for group in groups for hook in group.get("hooks", [])]


def check_configs() -> None:
    # Каталога агента может не быть: слой переносят в проект с одним агентом, и
    # там прогон обязан оставаться зеленым (.harness/README.md).
    if "agents/cursor" in read_json(ROOT / ".harness" / "state" / "lock.json").get("modules", []):
        codex = read_json(ROOT / ".cursor" / "hooks.json")
        if codex.get("version") != 1:
            fail(".cursor/hooks.json must use schema version 1")
        codex_hooks = codex.get("hooks", {})
        # Хук текстов ставит модуль forge: без поставщика кода проверять нечего.
        if (ROOT / SHARED_LINT).exists():
            expect_hook(codex_hooks.get("preToolUse", []), SHARED_LINT + " cursor", "Cursor preToolUse")
        expect_hook(codex_hooks.get("sessionStart", []), ".codex/hooks/session-start.py", "Codex sessionStart")
        expect_silent(".codex/hooks/session-start.py", "")
        for event in ("beforeSubmitPrompt", "preToolUse", "stop"):
            expect_hook(codex_hooks.get(event, []), GATE, "Cursor " + event)

    if (ROOT / ".codex" / "hooks.json").is_file():
        native = read_json(ROOT / ".codex" / "hooks.json").get("hooks", {})
        for event in ("UserPromptSubmit", "PreToolUse", "Stop"):
            expect_hook(flatten(native.get(event, [])), GATE, "Codex " + event)

    if (ROOT / ".claude").is_dir():
        claude_hooks = read_json(ROOT / ".claude" / "settings.json").get("hooks", {})
        if (ROOT / SHARED_LINT).exists():
            expect_hook(flatten(claude_hooks.get("PreToolUse", [])), SHARED_LINT + '" claude', "Claude PreToolUse")
        expect_hook(flatten(claude_hooks.get("SessionStart", [])),
                    ".claude/hooks/session-start.sh", "Claude SessionStart")
        for event in ("UserPromptSubmit", "PreToolUse", "Stop"):
            expect_hook(flatten(claude_hooks.get(event, [])), GATE, "Claude " + event)
        # Хук ESLint подключает адаптер node вместе с его конфигами: без них проверять файлу нечем.
        adapted = (ROOT / ".config" / "eslint-boundaries.js").is_file()
        for hook in FILE_GATE_HOOKS:
            if adapted or "eslint" not in hook:
                expect_hook(flatten(claude_hooks.get("PostToolUse", [])), hook, "Claude PostToolUse")


def probe_env(drop: tuple[str, ...] = (), **extra: str) -> dict[str, str]:
    return {**{name: value for name, value in os.environ.items() if name not in GIT_LOCATION + drop}, **extra}


def run_python(path: str, payload: str, *args: str, root: Path = ROOT,
               drop: tuple[str, ...] = ()) -> tuple[int, str, str]:
    environment = probe_env(drop, PYTHONIOENCODING="utf-8")
    result = run([sys.executable, str(root / path), *args], cwd=root, env=environment, input=payload)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def find_shell() -> str | None:
    sys.path.insert(0, str(ROOT / ".harness" / "lib"))
    from executables import bash as find_bash

    return shutil.which(find_bash())


def platform_copy(work: str) -> Path:
    """Копия слоя во временном каталоге: проба ломает движок и ведет сессию, не трогая дерево проекта."""
    repo = Path(work).resolve() / "repo"
    shutil.copytree(ROOT / ".harness", repo / ".harness", ignore=shutil.ignore_patterns("__pycache__", "obj", "bin"))
    return repo


def expect_silent(path: str, payload: str, *args: str) -> None:
    code, stdout, stderr = run_python(path, payload, *args)
    if code != 0 or stdout or stderr:
        fail(f"{path} must allow silently: exit={code}, stdout={stdout!r}, stderr={stderr!r}")


def expect_json(path: str, payload: str, *args: str) -> dict:
    code, stdout, stderr = run_python(path, payload, *args)
    if code != 0 or stderr:
        fail(f"{path} failed: exit={code}, stderr={stderr!r}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        fail(f"{path} returned invalid JSON: {stdout!r} ({error})")


def load_shared(path: str):
    spec = importlib.util.spec_from_file_location("shared_forge_text_lint", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_text_lint() -> None:
    shared = SHARED_LINT
    if not (ROOT / shared).exists():
        print("SKIP forge text hook: forge module is off")
        return

    long_title = "проверка порога заголовка " * 6
    lint = load_shared(shared)
    tools = lint.forge.tools
    long_body = "проверка " * 101
    long_message = "fix: заголовок\n\n" + long_body

    ignored = json.dumps({"tool_name": "Read", "tool_input": {"title": long_title}})
    empty = json.dumps({"tool_name": tools.prefix + "get_issue", "tool_input": {"issue_number": 1}})
    denied = json.dumps({
        "tool_name": tools.prefix + tools.comments[0],
        "tool_input": {"message": long_message},
    })

    for payload in ("not-json", ignored, empty):
        expect_silent(shared, payload)
    for agent in ("claude", "cursor"):
        expect_silent(shared, empty, agent)

    # Отказ сверяется с сообщением реестра по ключу, а не с его формулировкой.
    words_over = lint.msg("textlint_words_over", "message", len(long_body.split()), lint.WORD_CAPS["commit"])
    neutral = expect_json(shared, denied)
    if neutral.get("deny") is not True or words_over not in neutral.get("reason", ""):
        fail("shared forge text hook did not return its neutral deny contract")

    claude_result = expect_json(shared, denied, "claude").get("hookSpecificOutput", {})
    if claude_result.get("hookEventName") != "PreToolUse" or claude_result.get("permissionDecision") != "deny":
        fail("forge text hook did not return a Claude PreToolUse deny")

    cursor_result = expect_json(shared, denied, "cursor")
    if cursor_result.get("permission") != "deny" or not cursor_result.get("user_message"):
        fail("forge text hook did not return a Cursor preToolUse deny")


def check_close_gate() -> None:
    """Закрытие запроса в обход forge.py close-change: отказ инструменту площадки, CLI и API у каждого агента."""
    if not (ROOT / SHARED_LINT).exists():
        return
    tools = load_shared(SHARED_LINT).forge.tools
    refused = [{"tool_name": "Bash", "tool_input": {"command": command}} for command in (
        "gh pr close 5", "cd repo && glab mr close 5 --yes",
        "gh api -X PATCH repos/o/r/pulls/5 -f state=closed",
        "curl -X PUT -d state_event=close https://forge.example/api/v4/projects/1/merge_requests/5")]
    allowed = [{"tool_name": "Bash", "tool_input": {"command": command}} for command in (
        "./.harness/hooks/python.cmd .harness/lib/forge.py close-change 5 --body-file note.md",
        "grep -rn 'gh pr close' docs")]
    if tools and tools.close:
        name = tools.prefix + tools.close.action
        refused.append({"tool_name": name, "tool_input": {tools.close.field: tools.close.value}})
        allowed.append({"tool_name": name, "tool_input": {tools.close.field: "open"}})
    for agent in ("claude", "codex", "cursor"):
        for payload in refused:
            code, stdout, _ = run_python(GATE, json.dumps(payload), agent, "tool")
            if code or '"deny"' not in stdout or "close-change" not in stdout:
                fail(f"{agent}: agent gate let a change close bypass forge.py close-change: {payload}")
        for payload in allowed:
            code, stdout, _ = run_python(GATE, json.dumps(payload), agent, "tool")
            if code or '"deny"' in stdout:
                fail(f"{agent}: agent gate refused an allowed call: {payload}")


def check_findings_gate() -> None:
    """Отсрочка находки: отказ на объявлении, пропуск на цитате и на оговорке."""
    shared = SHARED_LINT
    if not (ROOT / shared).exists():
        return
    findings = read_json(ROOT / ".harness" / "methodology.json").get("policies", {}).get("findings", {})
    waiver = findings.get("waiver", "")
    phrase = "отдельной задачей"
    if not waiver or phrase not in findings.get("defer", []):
        print("SKIP findings gate: словаря отсрочки в манифесте нет")
        return

    lint = load_shared(shared)
    import text_data

    tools = lint.forge.tools
    signature = next(iter(lint.signatures()), "")

    def payload(body: str, tool: str = "", template: str = "comment/note", data: dict | None = None) -> str:
        record = text_data.mark(template, {**(data or {}), "Digest": text_data.digest(body)})
        return json.dumps({"tool_name": tool or tools.prefix + tools.comments[0],
                           "tool_input": {"body": body + "\n\n" + record + "\n\n" + signature}})

    announced = f"Правка чинит отбор проходов. Найденную опечатку в скрипте беру {phrase}."
    denied = expect_json(shared, payload(announced))
    # Документ правила сверяется с объявленным, а не с копией строки в тесте.
    if denied.get("deny") is not True or findings.get("doc", "") not in denied.get("reason", ""):
        fail("shared forge text hook did not refuse a deferred finding")

    expect_silent(shared, payload(f"{announced}\n{waiver} #12"))
    expect_silent(shared, payload(f"> беру {phrase}\n\nНет, чиню здесь же."))
    # Тело задачи - место, где отложенное живет по праву.
    expect_silent(shared, payload(announced, tools.prefix + tools.documents[0]))

    # Вердикт круга пишет ревьюер: отказ от совета его словами автору не переписать.
    methodology = read_json(ROOT / ".harness" / "methodology.json")
    review = methodology.get("processes", {}).get("review", {})
    signature = (methodology.get("policies", {}).get("authorship", {}).get("signatures") or [""])[0]
    mark = (review.get("fallback") or {}).get("mark", "")
    marker = next((entry.get("marker", "") for entry in review.get("steps", []) if entry.get("marker")), "")
    if mark and marker:
        expect_silent(shared, payload(f"{marker}. {mark}\n\n{announced}", template="review/verdict",
                                      data={"ReviewMode": mark}))


def check_marker_gate() -> None:
    """Текст агента без маркера данных и текст, правленый после render, получают отказ."""
    shared = SHARED_LINT
    if not (ROOT / shared).exists():
        return
    lint = load_shared(shared)
    import text_data

    tools = lint.forge.tools
    signature = next(iter(lint.signatures()), "")
    text = "Проверка маркера данных."
    record = text_data.mark("comment/note", {"Digest": text_data.digest(text)})

    def payload(body: str) -> str:
        return json.dumps({"tool_name": tools.prefix + tools.comments[0],
                           "tool_input": {"body": body + "\n\n" + signature}})

    expect_silent(shared, payload(text + "\n\n" + record))
    for body, expected in ((text, lint.msg("textlint_unmarked", "body")),
                           ("Правка. " + text + "\n\n" + record, lint.msg("textlint_altered", "body", "comment/note"))):
        denied = expect_json(shared, payload(body))
        if denied.get("deny") is not True or expected not in denied.get("reason", ""):
            fail("shared forge text hook did not refuse an unmarked or altered agent text")


def shape(text: str, placeholder: re.Pattern[str]) -> re.Pattern[str]:
    """Текст с подстановками как шаблон сверки: подстановка совпадает с любым текстом."""
    return re.compile(".*".join(map(re.escape, placeholder.sub("\0", text).split("\0"))), re.DOTALL)


def registry(path: str, key: str) -> re.Pattern[str]:
    """Сообщение реестра как шаблон: отказ сверяется с ключом."""
    sys.path.insert(0, str(ROOT / ".harness" / "lib"))
    from platform_messages import load

    template = load(str(ROOT / path)).get(key)
    if template is None:
        fail(f"{path} has no message {key}")
    return shape(template, re.compile(re.escape("%s")))


def instruction(name: str) -> re.Pattern[str]:
    """Промпт как шаблон: отказ сверяется с файлом промпта, подстановка - в форме движка text_templates."""
    sys.path.insert(0, str(ROOT / ".harness" / "scripts"))
    import script_paths  # noqa: F401
    import text_templates

    path = ROOT / text_templates.PROMPTS / f"{name}.md"
    if not path.is_file():
        fail(f"prompt {name} is missing: {path}")
    return shape(path.read_text(encoding="utf-8").rstrip("\n"), text_templates.Prompt.pattern)


def check_session_gate() -> None:
    """Гейт сессии на настоящих входах: каждый отказ идет парой с чистым входом, сбой движка сессию не отпускает."""
    if not shutil.which("git"):
        print("SKIP session gate: git is unavailable")
        return
    led, free = "probe-led-session", "probe-free-session"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        repo = platform_copy(work)
        # Копию выделяет настоящий task-worktree.py: без поставщика кода и трекера он не ходит в сеть.
        manifest = read_json(repo / ".harness" / "project" / "manifest.json")
        for module in ("forge", "tracker"):
            manifest.setdefault("modules", {}).setdefault(module, {})["extensions"] = {}
        (repo / ".harness" / "project" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        base = manifest.get("git", {}).get("baseBranch", "main")
        (repo / "file.txt").write_text("probe\n", encoding="utf-8")
        for command in (["init", "-q", "-b", base], ["add", "file.txt"],
                        ["-c", "user.name=probe", "-c", "user.email=probe@example.invalid", "-c", "core.hooksPath=",
                         "commit", "-q", "-m", "chore: probe"]):
            done = run(["git", *command], cwd=repo, env=probe_env())
            if done.returncode != 0:
                fail(f"session gate probe repository: git {command[0]} failed: {done.stderr.strip()}")

        def tool(name: str, *args: str) -> str:
            code, stdout, stderr = run_python(".harness/scripts/pipeline/" + name, "", *args, root=repo, drop=AGENT_RUN)
            if code != 0:
                fail(f"session gate probe: {name} {' '.join(args)} failed: {stderr or stdout}")
            return stdout

        def hook(event: str, payload: dict, agent: str = "claude") -> tuple[dict, str]:
            code, stdout, stderr = run_python(GATE, json.dumps(payload), agent, event, root=repo, drop=AGENT_RUN)
            if code != 0:
                fail(f"session gate {event} hook failed: exit={code}, stderr={stderr!r}")
            return (json.loads(stdout) if stdout else {}), stderr

        def edit(path: Path, session: str = led) -> tuple[dict, str]:
            return hook("tool", {"session_id": session, "tool_name": "Edit", "tool_input": {"file_path": str(path)}})

        def allowed(result: tuple[dict, str], case: str) -> None:
            if result != ({}, ""):
                fail(f"session gate must allow silently: {case}: {result!r}")

        def refused(result: tuple[dict, str], expected: re.Pattern[str], case: str) -> str:
            output = result[0].get("hookSpecificOutput", {})
            reason = output.get("permissionDecisionReason", "")
            if output.get("permissionDecision") != "deny" or not expected.fullmatch(reason):
                fail(f"session gate must refuse: {case}: {result!r}")
            return reason

        allowed(edit(repo / "file.txt"), "session outside the pipeline")
        allowed(hook("tool", {"session_id": led, "tool_name": "Bash", "tool_input": {"command": NEXT_STEP}}),
                "first pipeline call")
        refused(edit(repo / "file.txt"), instruction("session/claim"), "edit before claim")

        # Условия входа задает сообщение человека, которое начинается с вызова скилла: остальное их не меняет.
        asked = "probe-entry-session"
        call = "/" + Path(read_json(repo / ".harness" / "methodology.json")["processes"]["develop"]["doc"]).parent.name
        flags = " --base probe-base --review local --rounds 1 --merge agent"

        def conditions() -> str:
            return tool("pipeline-run.py", "next", "change", "--session", asked).splitlines()[-1]

        hook("prompt", {"session_id": asked, "prompt": "/probe " + call + flags})
        hook("tool", {"session_id": asked, "tool_name": "Bash", "tool_input": {"command": NEXT_STEP}})
        if re.search(r"probe-base|\bagent\b", conditions()):
            fail("session gate must ignore entry flags that do not open a human message")
        hook("prompt", {"session_id": asked, "prompt": call + flags + " task --merge human"})
        if not re.search(r"probe-base\b.*\blocal\b.*\b1\b.*\bagent\b", conditions()):
            fail("session gate must take entry flags from a human message that starts with the skill call")
        hook("prompt", {"session_id": asked, "prompt": call + " --rounds abc task"})
        code, _, stderr = run_python(".harness/scripts/pipeline/pipeline-run.py", "", "next", "change", "--session",
                                     asked, root=repo, drop=AGENT_RUN)
        if code == 0 or "--rounds abc" not in stderr:
            fail(f"session gate must refuse --rounds abc: exit={code}, stderr={stderr!r}")
        allowed(edit(repo / "file.txt", free), "same edit from a session outside the pipeline")

        copy = Path(json.loads(tool("task-worktree.py", "start", "--type", "chore", "--key", "probe", "--session", led))["path"])
        refused(edit(copy / "file.txt"), instruction("session/clarify"), "edit before clarify")
        tool("pipeline-run.py", "next", "change", "--session", led, "--clarified")
        allowed(edit(copy / "file.txt"), "edit in the task copy after clarify")
        refused(edit(repo / "file.txt"), instruction("session/wrong-copy"), "edit outside the task copy")

        # Гейты идут фоновой командой: остановка ждет ее уведомления, фоновый сервер ее не держит.
        journal = Path(work) / "transcript.jsonl"
        stamp = datetime.now(timezone.utc).isoformat()

        def launch(use: str, command: str) -> str:
            call = {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": use, "name": "Bash", "input": {"command": command}}]}}
            done = {"type": "user", "timestamp": stamp, "toolUseResult": {"backgroundTaskId": use},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": use}]}}
            return json.dumps(call) + "\n" + json.dumps(done) + "\n"

        implement = instruction("session/implement")
        stop = {"session_id": led, "transcript_path": str(journal)}
        journal.write_text(launch("toolu_server", "npm run dev"), encoding="utf-8")
        if not implement.fullmatch(hook("stop", stop)[0].get("reason", "")):
            fail("session gate must lead to the step while only a background server runs")
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(launch("toolu_gates", NEXT_STEP + " --run gates"))
        allowed(hook("stop", stop), "stop while pipeline gates run in the background")
        notice = "<task-notification>\n<tool-use-id>toolu_gates</tool-use-id>\n<status>completed</status>"
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "message": {"content": notice}}) + "\n")
        if not implement.fullmatch(hook("stop", stop)[0].get("reason", "")):
            fail("session gate must lead to the step once the background gates have finished")

        fault = registry(".harness/messages.env", "gate_fault")
        engine = repo / ".harness" / "scripts" / "pipeline" / "pipeline_next.py"
        working = engine.read_text(encoding="utf-8")

        # Падает только оценка шага на остановке: успешная правка не возвращает уже показанную ошибку.
        engine.write_text(working + "\n\ndef on_stop(*_):\n    raise RuntimeError('probe-stop-fault')\n", encoding="utf-8")
        stopped, _ = hook("stop", {"session_id": led})
        if stopped.get("decision") != "block" or not fault.fullmatch(stopped.get("reason", "")):
            fail(f"session gate must report a failed stop evaluation: {stopped!r}")
        if edit(copy / "file.txt")[0]:
            fail("session gate must allow an edit the engine accepts while stop evaluation fails")
        repeated, _ = hook("stop", {"session_id": led})
        if repeated:
            fail(f"session gate must report the same engine error once: {repeated!r}")

        engine.write_text("raise RuntimeError('probe-fault')\n", encoding="utf-8")
        if "probe-fault" not in refused(edit(copy / "file.txt"), fault, "edit with a broken engine"):
            fail("session gate refusal does not name the engine error")
        allowed(edit(copy / "file.txt", free), "broken engine and a session outside the pipeline")
        told, _ = hook("prompt", {"session_id": led})
        if not fault.fullmatch(told.get("hookSpecificOutput", {}).get("additionalContext", "")):
            fail(f"session gate must report a broken engine on a prompt: {told!r}")
        # Cursor на сообщение человека текста не возвращает: показ ошибки остается за остановкой.
        if hook("prompt", {"conversation_id": led}, "cursor")[0] != {"continue": True}:
            fail("session gate must let a Cursor prompt through")
        followup, _ = hook("stop", {"conversation_id": led}, "cursor")
        if not fault.fullmatch(followup.get("followup_message", "")):
            fail(f"session gate must report a broken engine on a Cursor stop after a prompt: {followup!r}")
        if hook("stop", {"conversation_id": led}, "cursor")[0]:
            fail("session gate must report a broken engine on a Cursor stop once")


def check_hook_faults() -> None:
    """Сбой внутри проверки - объясненный отказ, а не трассировка с пропуском инструмента."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        repo = platform_copy(work)
        (repo / ".harness" / "scripts" / "agent" / "permission_policy.py").write_text(
            "raise KeyError('probe-fault')\n", encoding="utf-8")
        shell = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        code, stdout, stderr = run_python(".harness/hooks/agent-deny.py", shell, "claude", root=repo)
        refusal = json.loads(stdout).get("hookSpecificOutput", {}) if stdout else {}
        if code != 0 or refusal.get("permissionDecision") != "deny" \
                or "probe-fault" not in refusal.get("permissionDecisionReason", ""):
            fail(f"deny hook must refuse when its policy cannot be read: exit={code}, {stdout!r} {stderr!r}")

        if not (ROOT / SHARED_LINT).exists():
            return
        tools = load_shared(SHARED_LINT).forge.tools
        (repo / ".harness" / "lib" / "authorship.py").write_text(
            "def signatures():\n    raise RuntimeError('probe-fault')\n\n\ndef signed(*_):\n    return True\n",
            encoding="utf-8")
        comment = json.dumps({"tool_name": tools.prefix + tools.comments[0], "tool_input": {"body": "Проба сбоя."}})
        code, stdout, stderr = run_python(SHARED_LINT, comment, root=repo)
        refusal = json.loads(stdout) if stdout else {}
        if code != 0 or refusal.get("deny") is not True \
                or not registry(".harness/messages.env", "textlint_fault").fullmatch(refusal.get("reason", "")):
            fail(f"forge text hook must refuse when its check fails: exit={code}, {stdout!r} {stderr!r}")


def check_session_start() -> None:
    """Подготовка удаленной сессии: успех отдает одну строку, отказ шага - вывод этого шага, а не всех."""
    bash = find_shell()
    if not bash or not shutil.which("git"):
        print("SKIP session start output: bash or git is unavailable")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        project = Path(work).resolve() / "project"
        scripts = project / ".harness" / "provisioning" / "scripts"
        scripts.mkdir(parents=True)
        if run(["git", "init", "-q", str(project)], env=probe_env()).returncode != 0:
            fail("session start probe repository was not created")

        def stub(name: str, code: int = 0, steps: int = 0) -> None:
            # Шаги проекта хук зовет тем же ensure-repo-deps.sh с флагом --only-steps.
            only = f'case "$*" in *--only-steps*) echo probe-noise-project-step; exit {steps} ;; esac\n'
            (scripts / name).write_text(f"#!/bin/bash\n{only}echo probe-noise-{name}\nexit {code}\n",
                                        encoding="utf-8", newline="\n")
            (scripts / name).chmod(0o755)

        def start() -> subprocess.CompletedProcess:
            environment = probe_env(AGENT_PROJECT_DIR=project.as_posix(), AGENT_REMOTE="true", AGENT_ENV_FILE="")
            return run([bash, (ROOT / SESSION_START).as_posix()], cwd=project, env=environment)

        for name in SESSION_STUBS:
            stub(name)
        quiet = start()
        journal = project / ".git" / "harness" / "session-start.log"
        if quiet.returncode != 0 or quiet.stderr.strip() or len(quiet.stdout.strip().splitlines()) != 1 \
                or "probe-noise" in quiet.stdout:
            fail(f"session start must report success with one line: {quiet.stdout!r} {quiet.stderr!r}")
        if not journal.is_file() or "probe-noise-ensure-repo-deps.sh" not in journal.read_text(encoding="utf-8"):
            fail("session start did not keep step output in its journal")

        stub("ensure-lsp.sh", 1)
        soft = start()
        if soft.returncode != 0 or "probe-noise-ensure-lsp.sh" not in soft.stdout \
                or "probe-noise-ensure-repo-deps.sh" in soft.stdout:
            fail(f"session start must show the output of the failed optional step only: {soft.stdout!r}")

        stub("ensure-lsp.sh")
        stub("ensure-repo-deps.sh", steps=1)
        step = start()
        ready = registry(".harness/messages.env", "session_ready")
        if step.returncode != 0 or "probe-noise-project-step" not in step.stdout or ready.search(step.stdout):
            fail(f"session start must show a failed project step and must not report readiness: {step.stdout!r}")

        stub("ensure-repo-deps.sh", 1)
        hard = start()
        if hard.returncode == 0 or "probe-noise-ensure-repo-deps.sh" not in hard.stderr or "probe-noise" in hard.stdout:
            fail(f"session start must fail with the output of the failed required step: {hard.stdout!r} {hard.stderr!r}")


def check_session_env() -> None:
    """Окружение сессии: расширение проекта доходит до экспорта и до запущенной команды, его отказ назван."""
    bash = find_shell()
    if not bash:
        print("SKIP session environment: bash is unavailable")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        repo = platform_copy(work)
        manifest = repo / ".harness" / "project" / "manifest.json"
        project = read_json(manifest)
        project["modules"]["agents"].setdefault("settings", {})["envScript"] = "probe-env.sh"
        manifest.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        script = (repo / SESSION_ENV).as_posix()

        def extension(code: int) -> None:
            (repo / "probe-env.sh").write_text(f"echo 'export HARNESS_PROBE_ENV=1'\nexit {code}\n",
                                               encoding="utf-8", newline="\n")

        extension(0)
        shown = run([bash, script, "--export"], cwd=repo, env=probe_env())
        if shown.returncode != 0 or "export PATH=" not in shown.stdout or "HARNESS_PROBE_ENV=1" not in shown.stdout:
            fail(f"session-env.sh --export must print the toolchain and the project extension: {shown.stdout!r} {shown.stderr!r}")
        launched = run([bash, script, "--exec", bash, "-c", 'printf %s "$HARNESS_PROBE_ENV"'], cwd=repo, env=probe_env())
        if launched.returncode != 0 or launched.stdout != "1":
            fail(f"session-env.sh --exec must run the command with the project extension: {launched.stdout!r} {launched.stderr!r}")

        extension(1)
        broken = run([bash, script, "--export"], cwd=repo, env=probe_env())
        named = registry(".harness/provisioning/messages.env", "session_env_extension_failed")
        if broken.returncode != 1 or "export PATH=" not in broken.stdout or "HARNESS_PROBE_ENV" in broken.stdout \
                or not named.search(broken.stderr):
            fail(f"session-env.sh --export must name a failed extension and keep the toolchain: {broken.stdout!r} {broken.stderr!r}")


def check_shell_adapters() -> None:
    bash = find_shell()
    if not bash:
        print("SKIP shell adapter syntax: bash is unavailable")
        return
    for path in SHELL_HOOKS:
        if not (ROOT / path).exists():
            continue
        result = run([bash, "-n", path], cwd=ROOT)
        if result.returncode != 0:
            fail(f"{path} has invalid shell syntax: {result.stderr.strip()}")

    if (ROOT / ".claude").is_dir():
        environment = os.environ.copy()
        environment["CLAUDE_CODE_REMOTE"] = "false"
        session = run([bash, ".claude/hooks/session-start.sh"], cwd=ROOT, env=environment)
        if session.returncode != 0:
            fail(f"Claude session adapter failed its local no-op: {session.stderr.strip()}")
        skill_bridge = ROOT / ".claude" / "skills"
        if not skill_bridge.is_dir() or skill_bridge.resolve() != (ROOT / ".agents" / "skills").resolve():
            fail("Claude session adapter did not bridge .claude/skills to canonical .agents/skills")

    # Хук стека ставит модуль стека: в дереве без него проверять нечего.
    for path in (path for path in FILE_GATE_HOOKS if (ROOT / path).exists()):
        hook = run([bash, path], cwd=ROOT, input="{}")
        if hook.returncode != 0:
            fail(f"{path} failed its no-file no-op: {hook.stderr.strip()}")


def check_powershell_hook() -> None:
    powershell = shutil.which("pwsh")
    if not (ROOT / ".harness" / "hooks" / "code-cleanup.ps1").exists():
        return
    if not powershell:
        print("SKIP PowerShell hook syntax: PowerShell is unavailable")
        return
    script = str(ROOT / ".harness" / "hooks" / "code-cleanup.ps1").replace("'", "''")
    command = f"$errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$errors); if ($errors) {{ exit 1 }}"
    result = run([powershell, "-NoProfile", "-Command", command])
    if result.returncode != 0:
        fail(f"code-cleanup.ps1 has invalid PowerShell syntax: {result.stderr.strip()}")


def main() -> int:
    try:
        for path in (".harness/project/manifest.json", ".harness/methodology.json", ".harness/pipelines.json",
                     ".harness/lib/manifest.py", ".harness/lib/hook.sh"):
            if not (ROOT / path).is_file():
                fail(f"Installed harness file is missing: {ROOT / path}")
        check_configs()
        check_text_lint()
        check_findings_gate()
        check_close_gate()
        check_marker_gate()
        check_session_gate()
        check_hook_faults()
        check_session_start()
        check_session_env()
        check_shell_adapters()
        check_powershell_hook()
    except (AssertionError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"hooks: FAIL: {error}", file=sys.stderr)
        return 1
    print("hooks: shared logic, agent dialects and manifests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
