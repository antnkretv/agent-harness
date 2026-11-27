#!/usr/bin/env python3
"""Пробы промптов и шаблонов поставки: одна задача на двух коммитах комплекта, одна модель, механическая оценка.
Звать при правке промптов ревью, шаблонов текстов и правил публикуемого текста в payload.

    python3 tests/prompt-probes/run.py --before origin/develop --after HEAD
    python3 tests/prompt-probes/run.py --probe change-text --reps 1
    python3 tests/prompt-probes/run.py --score описание.md
    python3 tests/prompt-probes/run.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import importlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(os.path.dirname(HERE))

FIXTURES = os.path.join(HERE, "fixtures")
LINE = re.compile(r"^MSG_([a-z][a-z0-9_]*)='([^']*)'", re.MULTILINE)
with open(os.path.join(HERE, "messages.env"), encoding="utf-8") as _messages:
    TEMPLATES = dict(LINE.findall(_messages.read()))
with open(os.path.join(FIXTURES, "inputs.json"), encoding="utf-8") as _inputs:
    INPUTS = json.load(_inputs)


def msg(key: str, *args: object) -> str:
    return TEMPLATES[key] % args


VARIANTS = {"before": "baseline", "after": "v1"}
PROBE_REPO = "example/probe"
CHANGE_DIFF = os.path.join(FIXTURES, "change.diff")
CHANGE_BODY = os.path.join(".probe", "change-body.md")
CHANGE_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
DENY = "WebFetch,WebSearch,Task,Bash(git push:*),Bash(git commit:*),Bash(git checkout:*),Bash(git switch:*),Bash(gh:*)"
TEXT_TEMPLATES = os.path.join(".harness", "scripts", "text", "text_templates.py")
VALE_LINT = os.path.join(".harness", "scripts", "check", "vale-lint.py")
CHANGE_METRICS = ("vale", "checklist_max", "checklist_median", "code_per_100_words", "own_links", "pass")
ANSWERS = ("shape=backend", "modules.backend=none", "modules.agents=claude-code", "modules.ci-agent=claude-code", "modules.review=on")
IDENTITY = {"GIT_AUTHOR_NAME": "probe", "GIT_AUTHOR_EMAIL": "probe@example.invalid",
            "GIT_COMMITTER_NAME": "probe", "GIT_COMMITTER_EMAIL": "probe@example.invalid"}

# Унаследовав переменные объемлющей сессии Claude Code, вложенный claude пишет в нее события.
# Учетные данные они не несут.
SESSION_ENV = """CLAUDE_CODE_SESSION_ID CLAUDE_CODE_REMOTE CLAUDE_CODE_REMOTE_SESSION_ID CLAUDE_CODE_MESSAGING_SOCKET
CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2 CLAUDE_CODE_USE_CCR_V2 CLAUDE_CODE_SYNC_SESSION_REFS
CLAUDE_CODE_TEE_SDK_STDOUT CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ATTENDED CLAUDE_CODE_SYNC_SKILLS CLAUDECODE
CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_REMOTE_SEND_KEEPALIVES CLAUDE_CODE_HOLD_UNANSWERED_PARKED_PERMISSION CLAUDE_EFFORT
CLAUDE_CODE_DEBUG CLAUDE_CODE_DIAGNOSTICS_FILE CLAUDE_PID CLAUDE_CODE_WORKER_EPOCH CLAUDE_AUTO_BACKGROUND_TASKS
CLAUDE_CODE_BG_TASKS_REPORT_RUNNING CLAUDE_AFTER_LAST_COMPACT CLAUDE_ADDITIONAL_DIRECTORIES
CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH""".split()


class ProbeError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class Budget:
    def __init__(self, limit: float):
        self.limit, self.spent, self.lock = limit, 0.0, threading.Lock()

    def check(self) -> None:
        with self.lock:
            if self.spent > self.limit:
                raise ProbeError("budget", msg("probe_budget", f"{self.spent:.2f}", f"{self.limit:.2f}"))

    def add(self, cost: float) -> None:
        with self.lock:
            self.spent += cost


class Harness:
    """Модули слоя harness из пробного дерева версии "после": оценка читает форму и вердикт так же, как поставка."""

    def __init__(self, tree: str):
        scripts = os.path.join(tree, ".harness", "scripts")
        sys.path.insert(0, scripts)
        importlib.import_module("script_paths")
        self.forms = importlib.import_module("text_forms")
        self.local = importlib.import_module("review_local")
        self.maps = importlib.import_module("review_inputs")
        self.trust = importlib.import_module("review_trust")
        self.results = importlib.import_module("agent_results")
        self.result = importlib.import_module("session_results").native_result


def git(*args: str, cwd: str = KIT, check: bool = True, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", env=env)
    if check and result.returncode != 0:
        raise ProbeError("harness", msg("probe_git_failed", " ".join(args), result.stderr.strip()))
    return result.stdout.strip()


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def jsonl(path: str) -> list[dict]:
    return [json.loads(line) for line in read(path).splitlines()] if os.path.exists(path) else []


def commit(tree: str, message: str) -> None:
    env = {**os.environ, **IDENTITY}
    git("-c", "core.looseCompression=0", "add", "--", ".", cwd=tree)
    git("commit", "-q", "--no-verify", "--allow-empty", "-m", message, cwd=tree, env=env)


def build_kit(ref: str, folder: str) -> str:
    """Комплект коммита собирает его же kit.py: сборка из чужих инструментов проверяла бы не ту версию."""
    source = os.path.join(folder, "source")
    os.makedirs(source)
    archive = subprocess.Popen(["git", "archive", "--format=tar", ref], cwd=KIT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    untar = subprocess.run(["tar", "-x", "-C", source], stdin=archive.stdout, capture_output=True)
    archive.stdout.close()
    if archive.wait() != 0 or untar.returncode != 0:
        error = (archive.stderr.read() or untar.stderr).decode(errors="replace").strip()
        raise ProbeError("harness", msg("probe_archive_failed", ref, error))
    git("init", "-q", cwd=source)
    commit(source, INPUTS["snapshot_commit"])
    out = os.path.join(folder, "build")
    done = subprocess.run([sys.executable, os.path.join(source, "tools", "kit.py"), "build",
                           "--out", out], cwd=source, capture_output=True, text=True, encoding="utf-8")
    kits = [entry.path for entry in os.scandir(out) if entry.is_dir()] if os.path.isdir(out) else []
    if done.returncode != 0 or len(kits) != 1:
        raise ProbeError("harness", msg("probe_build_failed", ref, (done.stdout + done.stderr).strip()[-1500:]))
    return kits[0]


def install(kit: str, tree: str) -> None:
    """Пустой проект без стека: ответы сценария лежат в answers.json, на прочие вопросы init без терминала берет
    ответ по умолчанию."""
    os.makedirs(tree)
    git("init", "-q", "-b", "develop", cwd=tree)
    git("remote", "add", "origin", f"https://github.com/{PROBE_REPO}.git", cwd=tree)
    write(os.path.join(tree, "README.md"), "# probe\n")
    commit(tree, INPUTS["snapshot_commit"])
    git("update-ref", "refs/remotes/origin/develop", "HEAD", cwd=tree)
    answers = {key: {"raw": value, "by": "owner"} for key, value in (pair.split("=", 1) for pair in ANSWERS)}
    write(os.path.join(tree, ".harness", "project", "answers.json"), json.dumps({"answers": answers}, indent=2) + "\n")
    done = subprocess.run([sys.executable, os.path.join(kit, "ah.py"), "init", "--target", tree, "--archive", kit + ".tar.gz"],
                          cwd=tree, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise ProbeError("harness", msg("probe_configure_failed", (done.stdout + done.stderr).strip()[-1500:]))
    sanitize(tree)
    commit(tree, INPUTS["install_commit"])


def sanitize(tree: str) -> None:
    """Хуки пробного дерева сняты: проба меряет промпт, а не хуки сессии."""
    settings = os.path.join(tree, ".claude", "settings.json")
    deny = json.loads(read(settings)).get("permissions", {}).get("deny", [])
    write(settings, json.dumps({"permissions": {"deny": deny}}, indent=2) + "\n")
    # Мост к скиллам кладет хук старта сессии, а он здесь снят.
    skills = os.path.join(tree, ".claude", "skills")
    if not os.path.lexists(skills):
        os.symlink(os.path.join("..", ".agents", "skills"), skills)


def prepare(ref: str, folder: str) -> str:
    kit = build_kit(ref, folder)
    tree = os.path.join(folder, "project")
    install(kit, tree)
    return tree


def copy_tree(source: str, path: str) -> str:
    shutil.copytree(source, path, symlinks=True)
    return path


def clean_env() -> dict:
    env = dict(os.environ)
    for name in SESSION_ENV:
        env.pop(name, None)
    return env


def claude(cfg: dict, tree: str, prompt: str, trace: list, *, sources: str = "user,project",
           tools: str = CHANGE_TOOLS, deny: str = DENY, effort: str | None = None, timeout: int = 2400) -> dict:
    cfg["budget"].check()
    command = ["claude", "-p", prompt, "--model", cfg["model"], "--effort", effort or cfg["effort"],
               "--setting-sources", sources, "--allowedTools", tools, "--disallowedTools", deny,
               "--permission-mode", "acceptEdits", "--output-format", "stream-json", "--verbose",
               "--no-session-persistence"]
    try:
        process = subprocess.run(command, cwd=tree, env=clean_env(), capture_output=True, text=True,
                                 encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError("timeout", msg("probe_claude_timeout", timeout))
    events = []
    for line in process.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    final = cfg["harness"].result(events)
    trace.append({"prompt": prompt, "events": events, "stderr": process.stderr[-4000:]})
    cost = float(final.get("total_cost_usd") or 0.0)
    cfg["budget"].add(cost)
    if not final or final.get("is_error"):
        raise ProbeError("serving", msg("probe_claude_result_missing", str(final.get("result", ""))[:300]))
    usage = final.get("modelUsage") or {}
    served = usage.get(cfg["model"], {}).get("outputTokens", 0)
    total = sum(item.get("outputTokens", 0) for item in usage.values()) or 1
    if served / total < 0.9:
        raise ProbeError("model-mismatch", msg("probe_model_mismatch", cfg["model"], sorted(usage)))
    return {"text": final.get("result", ""), "cost": cost, "stop_reason": final.get("stop_reason")}


def meta(reply: dict, **extra) -> dict:
    return {"cost_usd": reply["cost"], "stop_reason": reply["stop_reason"], **extra}


# --- оценки: чистые функции, их держит --self-test ------------------------------------------

def grade_review(harness: Harness, verdict_text: str) -> dict:
    """Ответ прохода - объект схемы ревью или текст со строкой вердикта: оба принимает сбор круга."""
    data = harness.results.json_object(verdict_text)
    if isinstance(data, dict) and "verdict" in data:
        verdict = str(data.get("verdict", "")).lower()
        comments = data.get("comments") if isinstance(data.get("comments"), list) else []
        return {"pass": int(verdict in ("approve", "changes")), "verdict": verdict, "finding_lines": len(comments),
                "chars": len(str(data.get("body", "")))}
    found = list(harness.local.VERDICT.finditer(verdict_text))
    findings = [line for line in verdict_text.splitlines()
                if re.search(r"[\w./-]+\.(cs|ts|tsx|md|json|sql|yml|yaml|py|sh|env)\b[^\n]{0,20}?\d+", line)]
    return {"pass": int(bool(found)), "verdict": found[-1].group(1).lower() if found else "",
            "finding_lines": len(findings), "chars": len(verdict_text)}


def checklist_words(forms, lines: list[str]) -> list[int]:
    return [len(item[len(forms.BOX):].split()) for item in forms.items("\n".join(lines))
            if item.startswith((forms.BOX, forms.CHECKED))]


def grade_change_text(harness: Harness, text: str, alerts: int, rendered: bool) -> dict:
    forms = harness.forms
    parts = {title[3:].strip(): lines for title, lines in forms.sections(text)}
    items = checklist_words(forms, parts.get(INPUTS["change_text_checklist"], []))
    prose = "\n".join(forms.block(parts.get(title, [])) for title in INPUTS["change_text_prose"])
    words = len(prose.split())
    code = len(re.findall(r"`[^`\n]+`", prose))
    links = re.findall(rf"https?://[^\s/]+/{re.escape(PROBE_REPO)}/(?:-/)?(?:pull|issues|merge_requests)/\d+", text)
    return {"pass": int(rendered), "vale": alerts, "checklist_max": max(items, default=0),
            "checklist_median": statistics.median(items) if items else 0,
            "code_per_100_words": round(100 * code / words, 1) if words else 0.0, "own_links": len(links)}


def text_templates(tree: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, os.path.join(tree, TEXT_TEMPLATES), *args], cwd=tree,
                          capture_output=True, text=True, encoding="utf-8")


def harness_text(tree: str, *args: str) -> str:
    result = text_templates(tree, *args)
    if result.returncode != 0:
        raise ProbeError("harness", msg("probe_text_command_failed", " ".join(args), result.stderr.strip()))
    return result.stdout


def vale_alerts(tree: str, text: str) -> int:
    """Вход хука публикации: vale-lint --stdin печатает строку итога и по строке на срабатывание."""
    if not shutil.which("vale"):
        raise ProbeError("harness", msg("probe_no_vale"))
    result = subprocess.run([sys.executable, os.path.join(tree, VALE_LINT), "--stdin"], cwd=tree, input=text,
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode not in (0, 1):
        raise ProbeError("harness", msg("probe_vale_failed", result.stderr.strip()))
    return max(len(result.stderr.strip().splitlines()) - 1, 1) if result.returncode else 0


def score_text(harness: Harness, render_tree: str, vale_tree: str, path: str) -> dict:
    text = read(path)
    rendered = text_templates(render_tree, "render", "change/description", "--set-file",
                              f"ChangeBody={os.path.abspath(path)}").returncode == 0
    return grade_change_text(harness, text, vale_alerts(vale_tree, text), rendered)


# --- пробы -------------------------------------------------------------------------------------

def apply_change(tree: str) -> str:
    """Голова ревью - изменение фикстуры поверх установленного слоя: его PR в пробном проекте нет."""
    applied = subprocess.run(["git", "apply", CHANGE_DIFF], cwd=tree, capture_output=True, text=True, encoding="utf-8")
    if applied.returncode != 0:
        raise ProbeError("harness", msg("probe_fixture_stale", applied.stderr.strip()))
    commit(tree, INPUTS["change_commit"])
    return git("rev-parse", "HEAD", cwd=tree)


def probe_review(cfg: dict, side: str, work: str, trace: list) -> tuple[dict, dict]:
    harness, local = cfg["harness"], cfg["harness"].local
    tree = copy_tree(cfg["trees"][side], os.path.join(work, "tree"))
    base_sha = git("rev-parse", "HEAD", cwd=tree)
    head_sha = apply_change(tree)
    base = os.path.join(tree, harness.trust.BASE_CONFIG)
    git("worktree", "add", "--detach", base, base_sha, cwd=tree)
    try:
        values = local.fallback_reader(local.base_reader(base), base)
        data = os.path.join(tree, local.INPUT_DIR)
        fields = local.emit_pass(base, "code", INPUTS["review_change"], "claude-code", data)
        harness.maps.write_inputs(tree, data, base_sha, head_sha)
        prompt = local.pass_prompt(fields["prompt"], base, local.setting(values, "prompt", ""), base_sha,
                                   INPUTS["review_reason"], "", "", data)
        reply = claude(cfg, tree, prompt, trace, sources="user", tools=local.setting(values, "allowedTools", ""),
                       deny=local.denies(values), effort=fields["effort"], timeout=3600)
        return grade_review(harness, reply["text"]), meta(reply, artifact=reply["text"])
    finally:
        git("worktree", "remove", "--force", base, cwd=tree, check=False)


def probe_change_text(cfg: dict, side: str, work: str, trace: list) -> tuple[dict, dict]:
    tree = copy_tree(cfg["trees"][side], os.path.join(work, "tree"))
    values = {"BODY": CHANGE_BODY, "TASK": read(os.path.join(FIXTURES, "task.md")), "DIFF": read(CHANGE_DIFF),
              "CHECKS": read(os.path.join(FIXTURES, "checks.md")), "FORM": harness_text(tree, "form", "ChangeBody"),
              "SHOW": harness_text(tree, "show", "change/description")}
    prompt = INPUTS["change_text_prompt"]
    for name, value in values.items():
        prompt = prompt.replace("{{" + name + "}}", value)
    reply = claude(cfg, tree, prompt, trace, timeout=1200)
    path = os.path.join(tree, CHANGE_BODY)
    if not os.path.exists(path):
        raise ProbeError("no-artifact", msg("probe_body_missing", CHANGE_BODY, reply["text"][:300]))
    return score_text(cfg["harness"], tree, cfg["trees"]["after"], path), meta(reply, artifact=read(path))


PROBES = {
    "review": (probe_review, 1),
    "change-text": (probe_change_text, 3),
}


# --- прогон ------------------------------------------------------------------------------------

def resumable(prior: dict, state: dict) -> bool:
    """Досчитывать каталог можно только тем же прогоном: имя ref без коммита его не определяет."""
    def identity(run: dict) -> tuple:
        return {name: side["commit"] for name, side in run["variants"].items()}, run["model"], run["effort"]
    return identity(prior) == identity(state)


def done_keys(path: str) -> set:
    return {(row["prompt_id"], row["meta"]["rep"]) for row in jsonl(path)}


def run_case(cfg: dict, side: str, probe: str, rep: int) -> str:
    variant = os.path.join(cfg["out"], VARIANTS[side])
    trace: list = []
    work = tempfile.mkdtemp(prefix=f"{probe}-", dir=cfg["scratch"])
    started = time.time()
    try:
        grade, found = PROBES[probe][0](cfg, side, work, trace)
        if "artifact" in found:
            write(os.path.join(variant, "artifacts", f"{probe}_rep{rep}.md"), found.pop("artifact"))
        row = {"prompt_id": probe, "prompt": probe, "tags": [probe], "status": "ok", "grade": grade,
               "model": cfg["model"], "latency_s": round(time.time() - started),
               "meta": {**found, "rep": rep, "ref": cfg["refs"][side], "commit": cfg["commits"][side]}}
        target, line = "results.jsonl", msg("probe_case_pass", probe, rep, grade.get("pass"), f"{found.get('cost_usd', 0):.2f}")
    except Exception as error:
        # Сломанная фикстура - отказ среды одного прогона, а не обрыв пула без summary.md.
        kind = error.kind if isinstance(error, ProbeError) else "harness"
        message = str(error) if isinstance(error, ProbeError) else f"{type(error).__name__}: {error}"
        row = {"prompt_id": probe, "rep": rep, "class": kind, "error": message, "model": cfg["model"]}
        target, line = "errors.jsonl", msg("probe_case_error", probe, rep, kind, message)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write(os.path.join(variant, "traces", f"{probe}_rep{rep}.json"), json.dumps(trace, ensure_ascii=False))
    with cfg["write_lock"]:
        with open(os.path.join(variant, target), "a", encoding="utf-8") as sink:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    return msg("probe_case_result", VARIANTS[side], line)


def summarize(out: str) -> str:
    table: dict = defaultdict(lambda: {side: {"n": 0, "pass": 0, "cost": 0.0, "err": 0} for side in VARIANTS})
    for side, variant in VARIANTS.items():
        for row in jsonl(os.path.join(out, variant, "results.jsonl")):
            cell = table[row["prompt_id"]][side]
            cell["n"] += 1
            cell["pass"] += row["grade"]["pass"]
            cell["cost"] += row["meta"].get("cost_usd", 0.0)
        for row in jsonl(os.path.join(out, variant, "errors.jsonl")):
            table[row["prompt_id"]][side]["err"] += 1
    lines = [msg("probe_summary_header"), "| --- | --- | --- | --- | --- | --- |"]
    for probe, sides in sorted(table.items()):
        before, after = sides["before"], sides["after"]
        lines.append(f"| {probe} | {before['pass']}/{before['n']} | {after['pass']}/{after['n']} | "
                     f"{before['cost']:.2f} | {after['cost']:.2f} | {before['err']}/{after['err']} |")
    metrics = {side: defaultdict(list) for side in VARIANTS}
    for side, variant in VARIANTS.items():
        rows = [row for row in jsonl(os.path.join(out, variant, "results.jsonl")) if row["prompt_id"] == "change-text"]
        for row in sorted(rows, key=lambda row: row["meta"]["rep"]):
            for name in CHANGE_METRICS:
                metrics[side][name].append(str(row["grade"][name]))
    if any(metrics[side] for side in VARIANTS):
        lines += ["", msg("probe_metrics_header"), "| --- | --- | --- |"]
        lines += [f"| {name} | {', '.join(metrics['before'][name])} | {', '.join(metrics['after'][name])} |"
                  for name in CHANGE_METRICS]
    return "\n".join(lines)


def self_test(harness: Harness, tree: str) -> int:
    with open(os.path.join(FIXTURES, "self-test.json"), encoding="utf-8") as handle:
        samples = json.load(handle)
    run_a = samples["run"]
    bad = grade_change_text(harness, samples["change_text_bad"], 0, False)
    good = grade_change_text(harness, samples["change_text_good"], 0, False)
    try:
        alerts = [vale_alerts(tree, samples[name]) for name in ("change_text_bad", "change_text_good")]
    except ProbeError:
        alerts = [-1, 0]
    checks = [
        (msg("probe_self_review_verdict"), grade_review(harness, samples["review_changes"])["verdict"] == "changes"),
        (msg("probe_self_review_missing"), grade_review(harness, samples["review_missing"])["pass"] == 0),
        (msg("probe_self_review_inline"), grade_review(harness, samples["review_inline"])["pass"] == 0),
        (msg("probe_self_review_structured"), grade_review(harness, samples["review_structured"])["verdict"] == "approve"),
        (msg("probe_self_resume_ref"), resumable(run_a, {**run_a, "variants": {"v1": {"ref": "HEAD", "commit": "a1"}}})),
        (msg("probe_self_resume_commit"), not resumable(run_a, {**run_a, "variants": {"v1": {"ref": "x", "commit": "b2"}}})),
        (msg("probe_self_resume_effort"), not resumable(run_a, {**run_a, "effort": "low"})),
        (msg("probe_self_checklist_max"), bad["checklist_max"] > good["checklist_max"]),
        (msg("probe_self_checklist_median"), bad["checklist_median"] > good["checklist_median"]),
        (msg("probe_self_code"), bad["code_per_100_words"] > good["code_per_100_words"]),
        (msg("probe_self_links"), bad["own_links"] > good["own_links"]),
        (msg("probe_self_vale"), alerts[0] > alerts[1]),
        (msg("probe_self_fixture"), subprocess.run(["git", "apply", "--check", CHANGE_DIFF], cwd=tree,
                                                   capture_output=True).returncode == 0),
    ]
    for name, ok in checks:
        print(msg("probe_self_result", "ok  " if ok else "FAIL", name))
    return int(not all(ok for _, ok in checks))


def positive(cast, wanted: str):
    def parse(value: str):
        try:
            number = cast(value)
            if number > 0:
                return number
        except ValueError:
            pass
        raise argparse.ArgumentTypeError(msg("probe_not_positive", wanted, value))
    return parse


def review_model(tree: str) -> str:
    """Модель проб - модель комплекта для Claude Code в пробном проекте: та, что ведет ревью у поставки."""
    source = read(os.path.join(tree, ".harness", "scripts", "agent", "agent_provider.py"))
    kinds = next((node.value for node in ast.parse(source).body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "KINDS" for target in node.targets)), None)
    model = ast.literal_eval(kinds).get("claude-code", {}).get("model") if kinds is not None else None
    if not model:
        raise ProbeError("harness", msg("probe_no_model"))
    return model


def main() -> int:
    parser = argparse.ArgumentParser(description="Пробы промптов и шаблонов поставки: до и после")
    parser.add_argument("--before", default="origin/develop", help="коммит с прежними промптами")
    parser.add_argument("--after", default="HEAD", help="коммит с новыми промптами")
    parser.add_argument("--probe", action="append", choices=sorted(PROBES), help="только эта проба, повторяемый")
    parser.add_argument("--reps", type=positive(int, msg("probe_whole_number")), help="повторов каждой пробы вместо принятых по умолчанию")
    parser.add_argument("--workers", type=positive(int, msg("probe_whole_number")), default=4)
    parser.add_argument("--budget-usd", type=positive(float, msg("probe_number")), default=60.0)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--out", default=os.path.join(KIT, "dist", "prompt-probes"))
    parser.add_argument("--self-test", action="store_true", help="проверить оценки и фикстуру без вызова модели")
    parser.add_argument("--score", help="оценить готовое описание PR пробой change-text без вызова модели")
    args = parser.parse_args()
    scratch = tempfile.mkdtemp(prefix="prompt-probes-")
    try:
        sides = ("after",) if args.self_test or args.score else tuple(VARIANTS)
        refs = {"before": args.before, "after": args.after}
        print(msg("probe_preparing", ", ".join(f"{side} {refs[side]}" for side in sides)), flush=True)
        trees = {side: prepare(refs[side], os.path.join(scratch, side)) for side in sides}
        harness = Harness(trees["after"])
        if args.self_test:
            return self_test(harness, trees["after"])
        if args.score:
            grade = score_text(harness, trees["after"], trees["after"], args.score)
            for name in CHANGE_METRICS:
                print(msg("probe_score_line", name, grade[name]))
            return 0
        if not shutil.which("claude"):
            print(msg("probe_no_claude"), file=sys.stderr)
            return 2
        cfg = {"model": review_model(trees["after"]), "effort": args.effort, "refs": refs, "trees": trees,
               "harness": harness, "commits": {side: git("rev-parse", "--short", ref) for side, ref in refs.items()},
               "out": args.out, "budget": Budget(args.budget_usd), "write_lock": threading.Lock(), "scratch": scratch}
        state = {"flow": "prompt-probes", "metrics": [{"id": "pass", "label": msg("probe_metric_pass"), "kind": "binary"}],
                 "variants": {VARIANTS[side]: {"ref": refs[side], "commit": cfg["commits"][side]} for side in refs},
                 "model": cfg["model"], "effort": args.effort}
        state_path = os.path.join(args.out, "_state.json")
        if os.path.exists(state_path) and not resumable(json.loads(read(state_path)), state):
            print(msg("probe_resume_mismatch", args.out), file=sys.stderr)
            return 2
        write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        done = {side: done_keys(os.path.join(args.out, VARIANTS[side], "results.jsonl")) for side in VARIANTS}
        jobs = [(side, probe, rep) for probe in args.probe or list(PROBES)
                for rep in range(PROBES[probe][1] if args.reps is None else args.reps)
                for side in VARIANTS if (probe, rep) not in done[side]]
        print(msg("probe_started", refs["before"], cfg["commits"]["before"], refs["after"],
                  cfg["commits"]["after"], cfg["model"], len(jobs)), flush=True)
        with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for future in cf.as_completed([pool.submit(run_case, cfg, *job) for job in jobs]):
                print(future.result(), flush=True)
        summary = summarize(args.out)
        write(os.path.join(args.out, "summary.md"), summary + "\n")
        print(summary)
        print(msg("probe_finished", f"{cfg['budget'].spent:.2f}", args.out))
        return 0
    except ProbeError as error:
        print(error, file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
