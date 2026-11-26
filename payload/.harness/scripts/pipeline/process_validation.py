from __future__ import annotations
import functools
import ast
import dataclasses
import glob
import json
import os
import re
import subprocess
import sys
import tomllib
from ci_common import HARNESS_BLOCK, ROOT, forge, heading_anchor, get, get_list, msg, process_definitions, platform_messages, workflow_files
from manifest import get_record, rule_records
from pipeline_state import MERGE_FACTS
from agent_provider import KINDS
from process_config import METHODOLOGY, RUNNER_KINDS, agent_prompts, fallback, passes, phrase, phrases, pass_values, process_entries, prompt_text, tools
from text_templates import fill

MANIFEST = os.path.join(ROOT, ".harness", "pipelines.json")
PIPELINES = "pipelines.json"

JOB_LINE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
GITLAB_JOB_LINE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):\s*$")

GITLAB_KEYS = {"default", "include", "stages", "variables", "workflow", "image", "services", "cache",
               "before_script", "after_script"}

ROUND_CALL = re.compile(r"\.harness/scripts/review/review-round\.py[ \t]+(prepare|pass|guard)\b(?:[ \t]+([A-Za-z0-9_-]+))?")

PIPELINE_CALL = re.compile(r'^[ \t]+(?:run:[ \t]+|-[ \t]+)?'
                           r'(?:python3|(?:\./)?\.harness/hooks/python\.cmd|'
                           r'"\$(?:GITHUB_WORKSPACE|CI_PROJECT_DIR)/\.harness/hooks/python\.cmd")[ \t]+'
                           r"\.harness/scripts/pipeline/pipeline-run\.py[ \t]+(audit|check-ready)\b([^\n]*)", re.M)

DEFER = re.compile(r"review-round\.py[ \t]+pass\b[^\n]*--defer-publish\b")
COLLECT = re.compile(r"review-round\.py[ \t]+guard\b[^\n]*--publish-deferred\b")

PUBLISH_GROUP = "harness-review-$HARNESS_REVIEW-publish"
RESOURCE_GROUP = re.compile(r"^[ \t]+resource_group:[ \t]*['\"]?([^'\"\s#]+)", re.M)

PREPARE_JOB = re.compile(r"--prepare-job[=\s]+[\"']?([A-Za-z0-9_-]+)")

CHECKOUT = re.compile(r"uses:[ \t]*actions/checkout@")
BASE_REF = re.compile(r"ref:[ \t]*\$\{\{[ \t]*github\.event\.pull_request\.base\.sha(?:[ \t]*\|\|[ \t]*github\.workflow_sha)?[ \t]*\}\}")
NO_CREDENTIALS = re.compile(r"persist-credentials:[ \t]*false")

HEAD_REFS = ("pull_request.head.", "github.head_ref", "github.sha", "refs/pull/", "refs/merge-requests/",
             "CI_MERGE_REQUEST_SOURCE")

GITLAB_GATE = re.compile(r"if:[ \t]*\$HARNESS_REVIEW\b")
GITLAB_OWN_RULE = re.compile(r"^\s*-?\s*if:\s*['\"]?\$HARNESS_PIPELINE\s*==\s*null['\"]?\s*$", re.M)

LIST_ITEM = re.compile(r"^(\s*)-\s")
ROOT_KEY = re.compile(r"^([^\s#-][^:\n]*):", re.M)

OPEN_TOOLS = re.compile(r"Bash\(|mcp__")

KEEP_ON_COMMIT = re.compile(r"^[ \t]+on_new_commit:[ \t]*none\b", re.M)

MERGE_METHODS = ("squash", "merge", "rebase")

EFFORTS = {level for spec in KINDS.values() for level in spec["efforts"]}

FIELD_REFERENCE = re.compile(r"\b(?:processes|pipelines|policies|prompts)(?:\.[A-Za-z0-9_*~-]+|\[[A-Za-z0-9_*~-]*\])+")

STEP_HEADING = re.compile(r"^#{1,6}[ ]+`?([^` ]+)`?[ ]*$")


def anchor(heading: str) -> str:
    return heading_anchor(heading.strip().lstrip("#").strip())


def anchors_of(path: str) -> set[str]:
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return set()
    return {anchor(line) for line in lines if line.startswith("#")}


@functools.cache
def absent() -> frozenset[str]:
    try:
        with open(os.path.join(ROOT, ".harness", "state", "lock.json"), encoding="utf-8") as handle:
            return frozenset(json.load(handle).get("absent", []))
    except (OSError, ValueError):
        return frozenset()


def check_target(target: str, where: str, problems: list[str]) -> None:
    if not isinstance(target, str):
        problems.append(msg("pipeline_contract_field", where))
        return
    path, _, fragment = target.partition("#")
    if not path:
        problems.append(msg("pipeline_path_unset", where))
        return
    if path in absent():
        return
    if not os.path.exists(os.path.join(ROOT, path)):
        problems.append(msg("pipeline_path_missing", where, path))
        return
    if fragment and path.endswith(".md") and fragment not in anchors_of(path):
        problems.append(msg("pipeline_anchor_missing", where, target))


def check_calls(path: str, where: str, problems: list[str]) -> None:
    if not path.startswith(".github/workflows/"):
        return
    for job, body in ci_jobs(path).items():
        if re.search(r"^[ \t]+steps:", body, re.M):
            continue
        called = re.search(r"^[ \t]+uses:[ \t]*['\"]?\./([^'\"\s]+)", body, re.M)
        if called and not os.path.exists(os.path.join(ROOT, called.group(1))):
            problems.append(msg("pipeline_call_missing", where, path, job, called.group(1)))


def check_paths(problems: list[str]) -> None:
    for pipeline in process_entries():
        where = msg("pipeline_where_pipeline", pipeline.get('id'))
        check_target(pipeline.get("doc", ""), where, problems)
        for step in pipeline.get("steps", []):
            step_where = msg("pipeline_where_step", where, step.get('id'))
            if step.get("actor") == "agent" and not step.get("doc", "").partition("#")[2].strip():
                problems.append(platform_messages.msg("pipeline_agent_section", step_where))
            check_target(step.get("doc", ""), step_where, problems)
            for gate in step.get("gates", []):
                check_target(gate, msg("pipeline_where_gate", step_where), problems)
                check_calls(gate, msg("pipeline_where_gate", step_where), problems)
            for item in step.get("readiness", []):
                if item.get("doc"):
                    check_target(item["doc"], msg("pipeline_where_readiness", step_where, item.get('id')), problems)

    check_target(get("processes.review.doc", "", file=METHODOLOGY), msg("pipeline_where_review"), problems)
    check_target(get("processes.review.workflow", "", file=METHODOLOGY), msg("pipeline_where_review_workflow"), problems)
    check_target(get("processes.review.commonPrompt", "", file=METHODOLOGY), msg("pipeline_where_review_common"), problems)
    check_target(get("processes.review.verdict", "", file=METHODOLOGY), msg("pipeline_where_review_verdict"), problems)
    for entry in passes():
        where = msg("pipeline_where_pass", entry.get('id'))
        check_target(entry.get("doc", ""), where, problems)
        check_target(entry.get("prompt", ""), msg("pipeline_where_pass_prompt", where), problems)
        check_prompt(entry, where, problems)

    check_target(fallback("doc", ""), msg("pipeline_where_fallback"), problems)
    check_target(fallback("runner", ""), msg("pipeline_where_fallback_runner"), problems)
    check_target(fallback("prompt", ""), msg("pipeline_where_fallback_prompt"), problems)


def check_prompt(entry: dict, where: str, problems: list[str]) -> None:
    try:
        text = prompt_text(entry)
    except OSError:
        return

    marker = entry.get("marker", "")
    if marker and marker in text:
        problems.append(msg("pipeline_prompt_marker_copy", where))
    if marker and "${ReviewType}" not in text:
        problems.append(msg("pipeline_prompt_marker_missing", where))

    bar = entry.get("doc", "")
    if bar and "${ReviewBase}/" + bar not in text:
        problems.append(msg("pipeline_prompt_bar_not_base", where, bar))

    if "${ReviewDecided}" not in text:
        problems.append(msg("pipeline_prompt_no_decided", where))

    if "${ReviewInput}" not in text:
        problems.append(msg("pipeline_prompt_no_input", where))

    try:
        fill(text, pass_values(entry))
    except ValueError as error:
        problems.append(f"{where}: {error}")


def check_fallback(problems: list[str]) -> None:
    for key in ("waitMinutes", "pollMinutes", "timeoutMinutes"):
        value = fallback(key, None)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(msg("pipeline_fallback_minutes", key, repr(value)))
    retries = fallback("retries", None)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        problems.append(msg("pipeline_fallback_retries", repr(retries)))

    wait, work = fallback("waitMinutes", 0), fallback("timeoutMinutes", 0)
    if isinstance(wait, int) and isinstance(work, int) and 0 < wait < work:
        problems.append(msg("pipeline_fallback_wait_short", wait, work))

    if fallback("label", "") in (get("processes.review.label", "", file=METHODOLOGY),
                                 get("processes.review.failedLabel", "", file=METHODOLOGY)):
        problems.append(msg("pipeline_fallback_label_clash"))

    try:
        with open(os.path.join(ROOT, fallback("prompt", "")), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return
    for token in ("${LocalBaseRef}", "${LocalReason}", "${LocalContext}"):
        if token not in text:
            problems.append(msg("pipeline_fallback_token_missing", token))
    try:
        fill(text, dict.fromkeys(("LocalBaseRef", "LocalReason", "LocalContext", "LocalContextMissing"), ""))
    except ValueError as error:
        problems.append(f"{msg('pipeline_where_fallback_prompt')}: {error}")


def check_installed_manifest(problems: list[str]) -> None:
    try:
        with open(os.path.join(ROOT, ".harness", "state", "lock.json"), encoding="utf-8") as handle:
            recorded = json.load(handle).get("manifest")
    except (OSError, ValueError, AttributeError):
        return
    if not recorded:
        return
    done = subprocess.run(["git", "hash-object", "--", ".harness/project/manifest.json"], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8")
    if done.returncode == 0 and done.stdout.strip() != recorded:
        problems.append(msg("pipeline_manifest_moved", ".harness/project/manifest.json", ".harness/state/lock.json"))


def check_gitlab_root(problems: list[str]) -> None:
    try:
        with open(os.path.join(ROOT, ".gitlab-ci.yml"), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return
    for key in ROOT_KEY.findall(HARNESS_BLOCK.sub("", text)):
        problems.append(msg("pipeline_gitlab_own_key", key))
    block = HARNESS_BLOCK.search(text)
    for item in include_items(text[block.end():] if block else ""):
        if not GITLAB_OWN_RULE.search(item):
            problems.append(msg("pipeline_gitlab_own_include", item.splitlines()[0].strip()))
    import gitlab_workflow
    for path, kit in gitlab_workflow.missing(ROOT):
        problems.append(msg("pipeline_gitlab_workflow", path, gitlab_workflow.KIT_FILE, "; ".join(" ".join(rule.split()) for rule in kit)))


def include_items(text: str) -> list[str]:
    items: list[list[str]] = []
    level = None
    for line in text.splitlines():
        if line[:1] not in ("", " ", "\t", "#", "-"):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = LIST_ITEM.match(line)
        if found and (level is None or len(found.group(1)) <= level):
            level = len(found.group(1))
            items.append([line])
        elif items:
            items[-1].append(line)
    return ["\n".join(item) for item in items]


def check_docs_free_of_markers(problems: list[str]) -> None:
    markers = [(entry.get("marker", ""), entry.get("id")) for entry in passes()]
    targets = {get("processes.review.doc", "", file=METHODOLOGY)}
    targets.update(entry.get("doc", "") for entry in passes())
    targets.update(pipeline.get("doc", "") for pipeline in process_entries())

    for target in sorted(t for t in targets if t):
        try:
            with open(os.path.join(ROOT, target), encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        for marker, name in markers:
            if marker and f"{marker}." in text:
                problems.append(msg("pipeline_doc_marker_copy", target, name))


def check_fact_copies(problems: list[str]) -> None:
    numbers = number_words()
    contexts = fact_contexts()
    numbered = re.compile(platform_messages.msg("pipeline_step_number"), re.I)
    compiled: dict[tuple[str, object], re.Pattern[str] | None] = {}
    marks = frozenset(mark.lower() for mark in platform_messages.msg("pipeline_step_marks").split(";"))
    try:
        with open(os.path.join(ROOT, ".harness", METHODOLOGY), encoding="utf-8") as handle:
            methodology = json.load(handle)
        declared = process_definitions(methodology["processes"])
        with open(os.path.join(ROOT, ".harness", PIPELINES), encoding="utf-8") as handle:
            compositions = json.load(handle)
        facts = process_facts(declared, compositions)
        manifest_path = os.path.join(ROOT, ".harness", "project/manifest.json")
        project = {}
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding="utf-8") as handle:
                project = json.load(handle)
    except (OSError, ValueError, KeyError) as error:
        problems.append(msg("pipeline_contract_document", ROOT, error))
        return
    checks = []
    for field, value in facts:
        if (field, value) not in compiled:
            compiled[field, value] = fact_pattern(field, value, numbers, contexts)
        if compiled[field, value] is None:
            problems.append(platform_messages.msg("pipeline_fact_no_context", field))
        else:
            checks.append((field, anchors(value, numbers), compiled[field, value]))
    words = sorted(marks.union(*(found for _, found, _ in checks)), key=len, reverse=True)
    scan = Scan(re.compile("|".join(map(re.escape, words))), marks, numbered, checks,
                {step["id"] for process in declared.values() for step in process["steps"]})
    patterns = [pattern for record in rule_records(methodology, project) if record.get("enabled", True)
                for pattern in record.get("copies", [])]
    for path in sorted({path for pattern in patterns for path in glob.glob(os.path.join(ROOT, pattern))}):
        with open(path, "rb") as handle:
            data = handle.read()
        relative = os.path.relpath(path, ROOT)
        for number, field in scan.file(path, data.decode("utf-8")):
            problems.append(platform_messages.msg("pipeline_fact_copy", relative, number, field) if field
                            else platform_messages.msg("pipeline_step_number_copy", relative, number))


@dataclasses.dataclass(frozen=True)
class Scan:
    anchor: re.Pattern[str]
    marks: frozenset[str]
    numbered: re.Pattern[str]
    checks: list[tuple[str, tuple[str, ...], re.Pattern[str]]]
    identities: set[str]

    def line(self, line: str) -> list[str]:
        heading = STEP_HEADING.fullmatch(line)
        if heading and heading[1] in self.identities:
            return []
        values = FIELD_REFERENCE.sub("", line)
        hits = set(self.anchor.findall(values.lower()))
        if not hits:
            return []
        found = [""] if hits & self.marks and self.numbered.search(values) else []
        return found + [field for field, words, expression in self.checks
                        if hits.intersection(words) and expression.search(values)]

    def file(self, path: str, text: str) -> list[tuple[int, str]]:
        lines = text.splitlines()
        if not path.endswith(".py"):
            return [(number, field) for number, line in enumerate(lines, 1) for field in self.line(line)]
        try:
            literals = Literals.of(text)
        except SyntaxError:
            return [(number, field) for number, line in enumerate(lines, 1) for field in self.line(line)]
        found = [(node, fields) for node in literals if (fields := dict.fromkeys(self.line(" ".join(node.value.splitlines()))))]
        if not found:
            return []
        marked = sorted({number for number, line in enumerate(lines, 1) if self.line(line)})
        return sorted((next((number for number in marked if node.lineno <= number <= (node.end_lineno or node.lineno)),
                             node.lineno), field) for node, fields in found for field in fields)


class Literals(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[ast.Constant] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.found.append(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        text = "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
        self.found.append(ast.Constant(value=text, lineno=node.lineno, end_lineno=node.end_lineno))
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                self.visit(part)

    @classmethod
    def of(cls, text: str) -> list[ast.Constant]:
        visitor = cls()
        visitor.visit(ast.parse(text))
        return visitor.found


def process_facts(declared: dict, compositions: dict) -> list[tuple[str, object]]:
    review = declared["review"]
    facts = [("processes.review." + key, review[key]) for key in ("label", "failedLabel", "managedLabel")]
    facts += [("processes.review.fallback.label", review["fallback"]["label"])]
    facts += [("processes.review.stallRounds", review["stallRounds"])]
    facts += [(f"processes.review.steps[{step['id']}].budget", step["budget"]) for step in review["steps"]]
    for name, process in declared.items():
        for step in process["steps"]:
            where = f"processes.{name}.steps[{step['id']}]"
            if "method" in step:
                facts.append((where + ".method", step["method"]))
            facts += [(where + ".handoff." + key, value)
                      for key, value in step.get("handoff", {}).items() if key in ("label", "method")]
            facts += [(where + ".readiness", item["title"]) for item in step.get("readiness", [])]
    facts += [("processes.review.fallback." + key, value) for key, value in review["fallback"].items()
              if type(value) is int]
    for name, composition in compositions.items():
        for step in composition["steps"]:
            facts += [(f"pipelines.{name}.steps[{step['id']}].transitions.{state}.maxVisits", transition["maxVisits"])
                      for state, transition in step.get("transitions", {}).items()]
    return [(field, value) for field, value in facts if value or value == 0]


def anchors(value, numbers: list[str]) -> tuple[str, ...]:
    if type(value) is not int:
        return (str(value).lower(),)
    spelled = re.split(r"[|() ?:\[\]+]+", numbers[value]) if 0 <= value < len(numbers) else []
    return tuple({str(value), *(word.lower() for word in spelled if word)})


def leaf(field: str) -> str:
    return re.sub(r".*\.", "", field)


def number_words() -> list[str]:
    units = platform_messages.msg("pipeline_count_words").split(";")
    teens = platform_messages.msg("pipeline_count_teens").split(";")
    tens = platform_messages.msg("pipeline_count_tens").split(";")
    spelled = units + teens
    for ten in tens:
        spelled += [ten] + [f"(?:{ten})[ ]+(?:{unit})" for unit in units[1:10]]
    return spelled


def fact_contexts() -> dict[str, str]:
    return {"method": platform_messages.msg("pipeline_fact_method"),
            "budget": platform_messages.msg("pipeline_fact_budget"),
            "stallRounds": platform_messages.msg("pipeline_fact_stallrounds"),
            "retries": platform_messages.msg("pipeline_fact_retries"),
            "waitMinutes": platform_messages.msg("pipeline_fact_waitminutes"),
            "pollMinutes": platform_messages.msg("pipeline_fact_pollminutes"),
            "timeoutMinutes": platform_messages.msg("pipeline_fact_timeoutminutes"),
            "maxVisits": platform_messages.msg("pipeline_fact_maxvisits")}


def fact_pattern(field: str, value, numbers: list[str], contexts: dict[str, str]) -> re.Pattern[str] | None:
    context = contexts.get(leaf(field), "")
    owner = re.findall(r"\[([^\]]+)\]", field)
    context = context.replace("{id}", re.escape(owner[-1]) if owner else "(?!)")
    if type(value) is int:
        if not context:
            return None
        spelled = re.escape(str(value)) + ("|" + numbers[value] if 0 <= value < len(numbers) else "")
        return re.compile(context.replace("{value}", "(?:" + spelled + ")"), re.I)
    if context:
        return re.compile(context.replace("{value}", re.escape(value)), re.I)
    return re.compile(r"(?<![\w/-])" + re.escape(value) + r"(?![\w/-]|\.[A-Za-z])")


WORKFLOW_LABEL = re.compile(r"label\.name[ \t]*==[ \t]*'([^']*)'")
WORKFLOW_TITLE = re.compile(r"display_title[ \t]*,[ \t]*'([^']*)'")
CODEX_MERGE_KEY = "git-pull-request-merge-method"
CODEX_MERGE = re.compile(r"^[ \t]*git-pull-request-merge-method[ \t]*=[ \t]*[\"']([^\"']*)[\"']", re.M)


def codex_merge_methods(text: str) -> list[str]:
    try:
        pending = [tomllib.loads(text)]
    except tomllib.TOMLDecodeError:
        return CODEX_MERGE.findall(text)
    found = []
    while pending:
        for key, value in pending.pop().items():
            if isinstance(value, dict):
                pending.append(value)
            elif key == CODEX_MERGE_KEY and isinstance(value, str):
                found.append(value)
    return found


def check_tool_copies(problems: list[str]) -> None:
    processes = process_definitions(get_record("processes", {}, file=METHODOLOGY))
    labels = forge.declared_labels(processes)
    label = processes.get("review", {}).get("label", "")
    review = get("processes.review.workflow", "", file=METHODOLOGY)
    for path in workflow_files():
        relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        literals = [(found, labels) for found in WORKFLOW_LABEL.finditer(text)] if relative == review else []
        literals += [(found, {label}) for found in WORKFLOW_TITLE.finditer(text)]
        for found, allowed in literals:
            if found.group(1) not in allowed:
                problems.append(msg("pipeline_workflow_label_literal", relative, text.count("\n", 0, found.start()) + 1,
                                    found.group(1), ", ".join(sorted(allowed)) or "-"))
    method = next((step["method"] for process in processes.values() for step in process.get("steps", [])
                   if "merged" in step.get("produces", []) and step.get("method")), "")
    path = os.path.join(ROOT, ".codex", "config.toml")
    if method and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for found in codex_merge_methods(handle.read()):
                if found != method:
                    problems.append(msg("pipeline_codex_merge_method", found, method))


def check_handoff(problems: list[str]) -> None:
    for pipeline in process_entries():
        for step in pipeline.get("steps", []):
            handoff = step.get("handoff")
            if handoff is None:
                continue
            where = msg("pipeline_where_handoff_step", step.get('id'))
            if not isinstance(handoff, dict):
                problems.append(msg("pipeline_handoff_not_record", where, repr(handoff)))
                continue
            for field in ("label", "actor"):
                if not handoff.get(field):
                    problems.append(msg("pipeline_handoff_field_unset", where, field))
            if handoff.get("actor") and handoff.get("actor") == step.get("actor"):
                problems.append(msg("pipeline_handoff_same_actor", where, repr(step.get('actor'))))
            method = handoff.get("method")
            if method and method not in MERGE_METHODS:
                problems.append(msg("pipeline_handoff_method_unknown", where, repr(method), ', '.join(MERGE_METHODS)))


def check_findings(problems: list[str]) -> None:
    doc = get("policies.findings.doc", "", file=METHODOLOGY)
    waiver = get("policies.findings.waiver", "", file=METHODOLOGY)
    phrases = get_list("policies.findings.defer", [], file=METHODOLOGY)
    if not doc and not waiver and not phrases:
        return

    check_target(doc, msg("pipeline_where_findings"), problems)
    if not waiver:
        problems.append(msg("pipeline_findings_no_waiver"))
    if not phrases:
        problems.append(msg("pipeline_findings_empty"))
    seen = set()
    for phrase in phrases:
        if not isinstance(phrase, str) or not phrase.strip():
            problems.append(msg("pipeline_findings_phrase_empty", repr(phrase)))
            continue
        if phrase != phrase.lower().strip():
            problems.append(msg("pipeline_findings_phrase_case", repr(phrase)))
        if phrase in seen:
            problems.append(msg("pipeline_findings_phrase_twice", repr(phrase)))
        seen.add(phrase)


def check_authorship(problems: list[str]) -> None:
    doc = get("policies.authorship.doc", "", file=METHODOLOGY)
    signatures = get_list("policies.authorship.signatures", [], file=METHODOLOGY)
    emails = get_list("policies.authorship.commitEmails", [], file=METHODOLOGY)
    trailers = get_list("policies.authorship.commitTrailers", [], file=METHODOLOGY)
    env = get_list("policies.authorship.agentEnv", [], file=METHODOLOGY)
    if not doc and not signatures and not emails and not trailers and not env:
        return

    check_target(doc, msg("pipeline_where_authorship"), problems)
    for name, values in (("signatures", signatures), ("commitEmails", emails), ("commitTrailers", trailers)):
        if not values or not all(isinstance(v, str) and v.strip() for v in values):
            problems.append(msg("pipeline_authorship_list_empty", name))
    for pair in env:
        if not isinstance(pair, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*=[a-z0-9-]+", pair):
            problems.append(msg("pipeline_authorship_env_form", repr(pair)))


def check_graph(problems: list[str]) -> None:
    prompt_ids = {entry.get("id") for entry in agent_prompts()}
    for pipeline in process_entries():
        where = msg("pipeline_where_pipeline", pipeline.get('id'))
        for step in pipeline.get("steps", []):
            prompt = step.get("prompt")
            if prompt and prompt not in prompt_ids:
                problems.append(msg("pipeline_graph_prompt", where, step.get('id'), prompt))


def check_contract(problems: list[str]) -> None:
    for name in ("pipeline.json", "permissions.json"):
        if os.path.exists(os.path.join(ROOT, ".harness", name)):
            problems.append(msg("pipeline_contract_obsolete", name))
    documents = {}
    for name in (PIPELINES, METHODOLOGY, "project/manifest.json"):
        try:
            with open(os.path.join(ROOT, ".harness", name), encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, dict) or not document:
                raise ValueError(name)
            documents[name] = document
        except (OSError, ValueError) as error:
            problems.append(msg("pipeline_contract_document", name, error))
    if len(documents) != 3:
        return
    methodology, project = documents[METHODOLOGY], documents["project/manifest.json"]
    declared = methodology.get("processes")
    if not isinstance(declared, dict) or not declared:
        problems.append(msg("pipeline_contract_field", "methodology.processes"))
        declared = {}
    try:
        if not all(isinstance(process, dict) and isinstance(process.get("steps"), list)
                   and all(isinstance(step, dict) for step in process["steps"])
                   for process in declared.values()):
            raise ValueError(msg("pipeline_contract_field", "methodology.processes"))
        declared = process_definitions(declared)
    except (ValueError, AttributeError, TypeError) as error:
        problems.append(str(error))
        return
    agents = project.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    if not isinstance(agents.get("cli"), dict) or not agents["cli"]:
        problems.append(msg("pipeline_contract_field", "project.agents.cli"))
    if "runners" in agents:
        problems.append(msg("pipeline_contract_field", "project.agents.runners"))
    efforts = methodology.get("efforts") if isinstance(methodology.get("efforts"), dict) else {}
    if not efforts or any(not isinstance(level, str) or level not in EFFORTS for level in efforts.values()):
        problems.append(msg("pipeline_contract_field", "methodology.efforts"))
    runners = set(efforts)
    granted = agents.get("tools") if isinstance(agents.get("tools"), dict) else {}
    for name, configured in granted.items():
        if name not in runners or not isinstance(configured, dict) or set(configured) - {"allowedTools", "disallowedTools"}:
            problems.append(msg("pipeline_contract_field", f"project.agents.tools.{name}"))
    policies = methodology.get("policies")
    if not isinstance(policies, dict):
        policies = {}
    for name in ("findings", "authorship", "permissions", "text"):
        if not isinstance(policies.get(name), dict):
            problems.append(msg("pipeline_contract_field", f"methodology.policies.{name}"))
    text_policy = policies.get("text")
    machine = text_policy.get("machineMessages") if isinstance(text_policy, dict) else None
    if not isinstance(machine, list) or not all(isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]*", key)
                                               for key in machine):
        problems.append(msg("pipeline_contract_field", "methodology.policies.text.machineMessages"))
    permissions = policies.get("permissions") if isinstance(policies.get("permissions"), dict) else {}
    if not isinstance(permissions.get("deny"), list):
        problems.append(msg("pipeline_contract_field", "methodology.policies.permissions.deny"))
    for key in ("agentCli", "runners", "review", "findings", "authorship", "permissions"):
        if key in methodology:
            problems.append(msg("pipeline_contract_field", f"methodology.{key}"))
    prompts = methodology.get("prompts")
    if not isinstance(prompts, dict):
        prompts = {}
    prompt_ids = set()
    for group in ("agents", "phrases"):
        entries = prompts.get(group)
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            problems.append(msg("pipeline_contract_field", f"methodology.prompts.{group}"))
            continue
        for entry in entries:
            identity = entry.get("id")
            where = f"methodology.prompts.{group}.{identity}"
            if not isinstance(identity, str) or not identity or identity in prompt_ids:
                problems.append(msg("pipeline_contract_field", f"{where}.id"))
            else:
                prompt_ids.add(identity)
            required = ("prompt",) if group == "agents" else ("text",)
            for field in (*required, *(key for key in ("commonPrompt", "runner") if key in entry)):
                if not isinstance(entry.get(field), str) or not entry[field]:
                    problems.append(msg("pipeline_contract_field", f"{where}.{field}"))
            if "runner" in entry and (not isinstance(entry["runner"], str) or entry["runner"] not in runners):
                problems.append(msg("pipeline_contract_runner", where, "runner", repr(entry["runner"])))

    for name, process in declared.items():
        where = f"methodology.processes.{name}"
        if not isinstance(process, dict):
            problems.append(msg("pipeline_contract_field", where))
            continue
        if not isinstance(process.get("doc"), str) or not process["doc"]:
            problems.append(msg("pipeline_contract_field", f"{where}.doc"))
        steps = process.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
            problems.append(msg("pipeline_contract_field", f"{where}.steps"))
            continue
        if name != "review":
            check_dependencies(steps, where, problems)
        else:
            for field in ("workflow", "commonPrompt", "allowedTools", "disallowedTools", "label",
                          "failedLabel", "managedLabel", "verdict", "statusPrefix"):
                if not isinstance(process.get(field), str) or not process[field]:
                    problems.append(msg("pipeline_contract_field", f"{where}.{field}"))
            verdicts = process.get("verdicts")
            if (not isinstance(verdicts, dict)
                    or not all(isinstance(verdicts.get(state), str) and verdicts[state] for state in ("approve", "changes"))):
                problems.append(msg("pipeline_contract_field", f"{where}.verdicts"))
            local = process.get("fallback")
            if not isinstance(local, dict):
                problems.append(msg("pipeline_contract_field", f"{where}.fallback"))
            else:
                for field in ("doc", "runner", "prompt", "label", "mark", "allowedTools"):
                    if not isinstance(local.get(field), str) or not local[field]:
                        problems.append(msg("pipeline_contract_field", f"{where}.fallback.{field}"))
            seen = set()
            for step in steps:
                identity = step.get("id")
                if not isinstance(identity, str) or not identity or identity in seen:
                    problems.append(msg("pipeline_contract_field", f"{where}.steps.id"))
                else:
                    seen.add(identity)
                for field in ("marker", "job", "prompt"):
                    if not isinstance(step.get(field), str) or not step[field]:
                        problems.append(msg("pipeline_pass_field_unset", identity, field))
                budget = step.get("budget")
                if type(budget) is not int or budget < 1:
                    problems.append(msg("pipeline_pass_budget", identity, repr(budget)))
        for entry in (process, *steps):
            for field in ("runner", "auditRunner"):
                if field not in entry:
                    continue
                reference = entry[field]
                if not isinstance(reference, str) or reference not in runners:
                    problems.append(msg("pipeline_contract_runner", where, field, repr(reference)))
        for step in steps:
            stage = f"{where}.{step.get('id')}"
            for field in ("doc", "prompt"):
                if field in step and not isinstance(step[field], str):
                    problems.append(msg("pipeline_contract_field", f"{stage}.{field}"))
            for field in ("gates", "produces"):
                values = step.get(field, [])
                if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                    problems.append(msg("pipeline_contract_field", f"{stage}.{field}"))
            readiness = step.get("readiness", [])
            if not isinstance(readiness, list) or not all(isinstance(item, dict) for item in readiness):
                problems.append(msg("pipeline_contract_field", f"{stage}.readiness"))
            if name != "review" and step.get("actor") not in ("agent", "command", "pipeline", "human"):
                problems.append(msg("pipeline_contract_field", f"{stage}.actor"))
            if "method" in step or MERGE_FACTS & set(step.get("produces", [])) and step.get("actor") == "human":
                if step.get("method") not in MERGE_METHODS:
                    problems.append(platform_messages.msg("pipeline_step_method", stage + ".method", repr(step.get("method")), ", ".join(MERGE_METHODS)))
            if step.get("actor") == "command":
                command = step.get("command")
                if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                    problems.append(msg("pipeline_contract_field", f"{stage}.command"))

    for name, composition in documents[PIPELINES].items():
        where = f"pipelines.{name}"
        if not isinstance(composition, dict) or set(composition) != {"steps"}:
            problems.append(msg("pipeline_contract_field", where))
            continue
        steps = composition["steps"]
        if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
            problems.append(msg("pipeline_contract_field", f"{where}.steps"))
            continue
        known = {step.get("id") for step in steps if isinstance(step.get("id"), str)}
        check_dependencies(steps, where, problems)
        for step in steps:
            stage = f"{where}.{step.get('id')}"
            if set(step) - {"id", "process", "requires", "transitions"}:
                problems.append(msg("pipeline_contract_field", stage))
            if not isinstance(step.get("process"), str) or step["process"] not in declared:
                problems.append(msg("pipeline_contract_process", stage, repr(step.get("process"))))
            transitions = step.get("transitions", {})
            if not isinstance(transitions, dict):
                problems.append(msg("pipeline_contract_field", f"{stage}.transitions"))
                continue
            for state, transition in transitions.items():
                if (state not in {"changes", "approve", "blocked", "error"}
                        or not isinstance(transition, dict)
                        or set(transition) != {"target", "maxVisits"}
                        or not isinstance(transition.get("target"), str)
                        or transition["target"] not in known
                        or type(transition.get("maxVisits")) is not int
                        or transition["maxVisits"] < 1):
                    problems.append(msg("pipeline_contract_transition", stage, state))


def check_dependencies(steps: list[dict], where: str, problems: list[str]) -> None:
    known: set[str] = set()
    edges: dict[str, list[str]] = {}
    for step in steps:
        name = step.get("id")
        if not isinstance(name, str) or not name or name in known:
            problems.append(msg("pipeline_contract_field", f"{where}.steps.id"))
            continue
        known.add(name)
        needs = step.get("requires")
        if not isinstance(needs, list) or not all(isinstance(need, str) for need in needs):
            problems.append(msg("pipeline_contract_field", f"{where}.{name}.requires"))
            continue
        edges[name] = needs
    visited: set[str] = set()

    def walk(name: str, trail: tuple[str, ...]) -> None:
        if name in trail:
            problems.append(msg("pipeline_graph_cycle", where, " => ".join((*trail, name))))
            return
        if name in visited:
            return
        visited.add(name)
        for need in edges.get(name, []):
            if need not in known:
                problems.append(msg("pipeline_graph_dangling", where, name, need))
            else:
                walk(need, (*trail, name))

    for name in edges:
        walk(name, ())


def workflow_jobs(text: str) -> dict[str, str]:
    jobs: dict[str, str] = {}
    lines = text.splitlines()
    inside = False
    name, start = "", 0
    for index, line in enumerate(lines):
        if not inside:
            inside = line.rstrip() == "jobs:"
            continue
        if line[:1] not in ("", " ", "#"):
            break
        found = JOB_LINE.match(line)
        if not found:
            continue
        if name:
            jobs[name] = "\n".join(lines[start:index])
        name, start = found.group(1), index
    if name:
        jobs[name] = "\n".join(lines[start:])
    return jobs


def gitlab_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    name, start = "", 0
    for index, line in enumerate(lines):
        if line[:1] in ("", " ", "\t", "#"):
            continue
        if name:
            blocks[name] = "\n".join(lines[start:index])
        found = GITLAB_JOB_LINE.match(line)
        name = found.group(1) if found else ""
        start = index
    if name:
        blocks[name] = "\n".join(lines[start:])
    return blocks


def gitlab_jobs(text: str) -> dict[str, str]:
    return {name: body for name, body in gitlab_blocks(text).items() if name not in GITLAB_KEYS}


def ci_jobs(relative: str) -> dict[str, str]:
    try:
        with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}
    return workflow_jobs(text) if relative.startswith(".github/") else gitlab_jobs(text)


def guard_needs(guard: str) -> set[str]:
    found = re.search(r"^([ \t]+)needs:[ \t]*(.*)$", guard, re.M)
    if not found:
        return set()
    inline = re.sub(r"(?:^|\s+)#.*$", "", found.group(2)).strip()
    if inline:
        return {name.strip().strip("'\"") for name in inline.strip("[]").split(",") if name.strip()}
    indent = len(found.group(1))
    names: set[str] = set()
    for line in guard[found.end():].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        depth = len(line) - len(line.lstrip())
        item = re.match(r"[ \t]*-[ \t]*(?:job:[ \t]*)?['\"]?([^'\"\s]+)", line)
        if item and depth >= indent:
            names.add(item.group(1))
        elif depth <= indent:
            break
    return names


def round_jobs(jobs: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    return {job: ROUND_CALL.findall(body) for job, body in jobs.items()}


def calling(calls: dict[str, list[tuple[str, str]]], command: str) -> str:
    return next((job for job, found in calls.items() if any(call[0] == command for call in found)), "")


def check_kinds(problems: list[str]) -> None:
    from agent_runtime import COMMANDS, OUTCOMES
    from session_log import READERS
    for kind in RUNNER_KINDS:
        for part, table in (("agent_runtime.COMMANDS", COMMANDS), ("agent_runtime.OUTCOMES", OUTCOMES),
                            ("session_log.READERS", READERS)):
            if kind not in table:
                problems.append(msg("pipeline_runner_adapter_missing", kind, part))


def check_trusted(relative: str, text: str, jobs: dict[str, str], gitlab: bool, problems: list[str]) -> None:
    for ref in HEAD_REFS:
        if ref in text:
            problems.append(msg("pipeline_head_ref", relative, ref))
    for job, body in jobs.items():
        if gitlab:
            if not GITLAB_GATE.search(body):
                problems.append(msg("pipeline_gitlab_rules", relative, job))
            continue
        if not re.search(r"^[ \t]+steps:", body, re.M):
            continue
        checkouts = len(CHECKOUT.findall(body))
        if not checkouts or checkouts != len(BASE_REF.findall(body)) or checkouts != len(NO_CREDENTIALS.findall(body)):
            problems.append(msg("pipeline_checkout_not_base", relative, job,
                                "${{ github.event.pull_request.base.sha }}"))
    if not gitlab:
        check_github_boundary(relative, text, jobs, problems)
    check_pipeline_runtime(relative, jobs, problems)


def permissions_readonly(text: str, *, required: bool = False) -> bool:
    found = re.search(r"^([ \t]*)permissions:[ \t]*([^\n]*)", text, re.M)
    if not found:
        return not required
    inline = found.group(2).partition("#")[0].strip()
    if inline:
        return inline in ("read-all", "{}")
    indent = len(found.group(1))
    entries = 0
    for line in text[found.end():].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if not re.fullmatch(r"\s+[a-z-]+:[ \t]*(?:read|none)[ \t]*(?:#.*)?", line):
            return False
        entries += 1
    return entries > 0


def check_github_boundary(relative: str, text: str, jobs: dict[str, str], problems: list[str]) -> None:
    root = gitlab_blocks(text)
    events = root.get("on", "")
    allowed_events = {"pull_request_target", "issue_comment", "workflow_run"}
    declared_events = set(re.findall(r"^  ([a-z_]+):", events, re.M))
    if "pull_request_target" not in declared_events or declared_events - allowed_events:
        problems.append(msg("pipeline_boundary_trigger", relative))
    if not permissions_readonly(root.get("permissions", ""), required=True):
        problems.append(msg("pipeline_boundary_permissions", relative, "permissions"))
    calls = round_jobs(jobs)
    guard = calling(calls, "guard")
    if guard and not re.search(r"^[ \t]+REVIEW_TRUSTED_PUBLISHER:[ \t]*['\"]?true['\"]?[ \t]*$", jobs[guard], re.M):
        problems.append(msg("pipeline_boundary_publisher", relative, guard))
    for job, body in jobs.items():
        commands = PIPELINE_CALL.findall(body)
        is_audit = any(command == "audit" for command, _ in commands)
        is_pass = any(command == "pass" for command, _ in calls[job])
        if (is_pass or is_audit) and not permissions_readonly(body):
            problems.append(msg("pipeline_boundary_permissions", relative, job))
        steps = re.search(r"^[ \t]+steps:[ \t]*\n", body, re.M)
        if not steps:
            continue
        for step in include_items(body[steps.end():]):
            if (re.search(r"uses:[ \t]*actions/download-artifact@", step)
                    and re.search(r"^[ \t]+(?:repository|run-id|github-token):", step, re.M)):
                problems.append(msg("pipeline_boundary_artifacts", relative, job))


def check_pipeline_runtime(relative: str, jobs: dict[str, str], problems: list[str]) -> None:
    commands = [call for body in jobs.values() for call in PIPELINE_CALL.findall(body)]
    audit = any(command == "audit" for command, _ in commands)
    ready = any(command == "check-ready" and re.search(r"--publish-context[= \t]+pipeline/ready\b", args)
                for command, args in commands)
    for command, present in (("audit", audit), ("check-ready --publish-context pipeline/ready", ready)):
        if not present:
            problems.append(msg("pipeline_boundary_runtime", relative, command))


def resource_group(body: str) -> str:
    found = RESOURCE_GROUP.search(body)
    return found.group(1) if found else ""


def check_deferred(relative: str, jobs: dict[str, str], calls: dict[str, list[tuple[str, str]]], prepare: str,
                   guard: str, problems: list[str]) -> None:
    sessions = {job for job, found in calls.items() if any(command == "pass" for command, _ in found)}
    deferred = {job for job in sessions if DEFER.search(jobs[job])}
    collecting = bool(guard) and COLLECT.search(jobs[guard]) is not None
    if deferred and deferred != sessions:
        problems.append(msg("pipeline_defer_mixed", relative, ", ".join(sorted(sessions - deferred))))
    if deferred and guard and not collecting:
        problems.append(msg("pipeline_defer_guard", relative, guard))
    if collecting and not deferred:
        problems.append(msg("pipeline_defer_unused", relative, guard))
    if not deferred or relative.startswith(".github/"):
        return
    for job in (prepare, guard):
        if job and resource_group(jobs[job]) != PUBLISH_GROUP:
            problems.append(msg("pipeline_publish_group", relative, job, PUBLISH_GROUP))


def check_workflow(problems: list[str]) -> None:
    relative = get("processes.review.workflow", "", file=METHODOLOGY)
    path = os.path.join(ROOT, relative)
    if not relative or not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    gitlab = not relative.startswith(".github/")
    if gitlab and not KEEP_ON_COMMIT.search(gitlab_blocks(text).get("workflow", "")):
        problems.append(msg("pipeline_round_auto_cancel", relative))
    jobs = ci_jobs(relative)
    if not jobs:
        problems.append(msg("pipeline_workflow_no_jobs", relative))
        return

    calls = round_jobs(jobs)
    prepare, guard = calling(calls, "prepare"), calling(calls, "guard")
    for command, job in (("prepare", prepare), ("guard", guard)):
        if not job:
            problems.append(msg("pipeline_round_job_missing", relative, command))
    waited = guard_needs(jobs.get(guard, ""))
    named = PREPARE_JOB.search(jobs.get(guard, ""))
    if guard and prepare and (not named or named.group(1) != prepare):
        problems.append(msg("pipeline_guard_prepare_name", guard, named.group(1) if named else "-", prepare))
    if guard and prepare and prepare not in waited:
        problems.append(msg("pipeline_guard_no_prepare", guard, prepare))

    declared = {entry.get("id") for entry in passes()}
    for job, found in calls.items():
        for command, name in found:
            if command == "pass" and name not in declared:
                problems.append(msg("pipeline_pass_unknown", relative, job, name))
    check_deferred(relative, jobs, calls, prepare, guard, problems)

    for entry in passes():
        name, marker = entry.get("id"), entry.get("marker", "")
        job = entry.get("job")
        if job not in jobs:
            problems.append(msg("pipeline_pass_no_job", name, job))
        elif ("pass", name) not in calls[job]:
            problems.append(msg("pipeline_pass_not_called", name, job, name))
        if not gitlab and prepare and not re.search(rf"^\s+run_{re.escape(name)}:", jobs[prepare], re.M):
            problems.append(msg("pipeline_prepare_no_run", name, prepare, name))
        if job and guard and job not in waited:
            problems.append(msg("pipeline_guard_not_waiting", name, guard, job))
        if marker and marker in text:
            problems.append(msg("pipeline_workflow_marker_copy", name))

    if prepare:
        expected_outputs = {"run_audit", *(f"run_{entry.get('id')}" for entry in passes())}
        for extra in sorted(set(re.findall(r"^\s+(run_[a-z0-9_-]+):", jobs[prepare], re.M)) - expected_outputs):
            problems.append(msg("pipeline_prepare_extra_output", prepare, extra))
    for extra in sorted(waited - {prepare} - {entry.get("job") for entry in passes()}):
        problems.append(msg("pipeline_guard_extra_job", guard, extra))
    for extra in sorted(set(re.findall(r"^\s+((?:RUN|RESULT)_[A-Z0-9_]+):", jobs.get(guard, ""), re.M))):
        problems.append(msg("pipeline_guard_env_pass", guard, extra, prepare))

    label = get("processes.review.label", "", file=METHODOLOGY)
    if not gitlab and label and label not in text:
        problems.append(msg("pipeline_workflow_literal_missing", relative, msg("pipeline_what_label"), label))

    check_trusted(relative, text, jobs, gitlab, problems)
    if gitlab:
        folder = os.path.join(ROOT, ".gitlab", "ci")
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            parent = f".gitlab/ci/{name}"
            if parent == relative or not name.endswith(".yml"):
                continue
            for job, body in ci_jobs(parent).items():
                if f"local: {relative}" in body and not GITLAB_GATE.search(body):
                    problems.append(msg("pipeline_gitlab_rules", parent, job))


def check_review(problems: list[str]) -> None:
    for entry in passes():
        declared_prose = entry.get("prose")
        if declared_prose is None:
            continue
        where = msg("pipeline_where_pass", entry.get('id'))
        if not isinstance(declared_prose, dict) or not declared_prose.get("suffixes"):
            problems.append(msg("pipeline_prose_not_record", where, repr(declared_prose)))
            continue
        for suffix in sorted(declared_prose.get("suffixes", [])):
            if not isinstance(suffix, str) or not suffix.startswith(".") or len(suffix) < 2:
                problems.append(msg("pipeline_prose_suffix", where, repr(suffix)))
        for item in sorted(declared_prose.get("except", [])):
            if not isinstance(item, str) or not item:
                problems.append(msg("pipeline_prose_except_form", where, repr(item)))
            elif not os.path.exists(os.path.join(ROOT, item)):
                problems.append(msg("pipeline_prose_except_missing", where, item))

    chars = get("processes.review.decidedChars", None, file=METHODOLOGY)
    if not isinstance(chars, int) or isinstance(chars, bool) or chars < 200:
        problems.append(msg("pipeline_decided_chars", repr(chars)))

    chars = get("processes.review.contextChars", None, file=METHODOLOGY)
    if not isinstance(chars, int) or isinstance(chars, bool) or chars < 200:
        problems.append(msg("pipeline_context_chars", repr(chars)))

    chars = get("processes.review.bodyChars", None, file=METHODOLOGY)
    if not isinstance(chars, int) or isinstance(chars, bool) or chars < 200:
        problems.append(msg("pipeline_body_chars", repr(chars)))

    maps = get_record("processes.review.maps", {}, file=METHODOLOGY)
    for key in ("entries", "places", "frequent"):
        value = maps.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(msg("pipeline_review_maps", key, repr(value)))

    opened = OPEN_TOOLS.search(get("processes.review.allowedTools", "", file=METHODOLOGY))
    if opened:
        problems.append(msg("pipeline_review_tools_forge", opened.group(0)))

    stall = get("processes.review.stallRounds", None, file=METHODOLOGY)
    budgets = [entry.get("budget") for entry in passes() if isinstance(entry.get("budget"), int)]
    if not isinstance(stall, int) or isinstance(stall, bool) or stall < 1:
        problems.append(msg("pipeline_stall_form", repr(stall)))
    elif budgets and stall <= max(budgets):
        problems.append(msg("pipeline_stall_low", stall, max(budgets)))


def diverged(problems: list[str]) -> int:
    print(msg("pipeline_diverged"), file=sys.stderr)
    for line in problems:
        print(f"  x {line}", file=sys.stderr)
    return 1


def check_agent_tools(name: str, runner: dict, problems: list[str]) -> None:
    for key in ("allowedTools", "disallowedTools"):
        if not isinstance(runner.get(key), str) or not runner[key].strip():
            problems.append(msg("pipeline_agent_tools_unset", name, key))


def check_prompts(problems: list[str]) -> None:
    for kind, spec in KINDS.items():
        if spec.get("commonPrompt"):
            check_target(spec["commonPrompt"], msg("pipeline_where_agent_prompt", kind), problems)
    for item in agent_prompts():
        check_target(item.get("prompt", ""), msg("pipeline_where_agent_prompt", item.get("id")), problems)
        if "commonPrompt" in item:
            check_target(item["commonPrompt"], msg("pipeline_where_agent_prompt", item.get("id")), problems)
        if "runner" not in item:
            continue
        check_agent_tools(item.get("id"), tools(item["runner"]), problems)
    for item in phrases():
        if not isinstance(item.get("text"), str) or not item.get("text").strip():
            problems.append(msg("pipeline_phrase_empty", item.get("id")))
            continue
        try:
            fill(item["text"], {"ChangeRef": ""})
        except ValueError as error:
            problems.append(f"{item.get('id')}: {error}")
    for name in ("decided-missing", "input-missing"):
        if not phrase(name):
            problems.append(msg("pipeline_phrase_missing", name))

    words = [get(f"processes.review.verdicts.{state}", "", file=METHODOLOGY) for state in ("approve", "changes")]
    if not all(words) or words[0] == words[1] or any(w != w.strip().lower() for w in words):
        problems.append(msg("pipeline_verdict_words", repr(words)))
