from __future__ import annotations

import calendar
import dataclasses
import functools
import json
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Callable, Sequence

import forge
import rest
from manifest import get
from platform_messages import msg

SERVER = "https://gitlab.com"
PROJECT = re.compile(r"[\w.-]+(?:/[\w.-]+)+")
LABEL_COLOR = "#428BCA"
MAX_DESCRIPTION = 255
DEVELOPER = 30
HOOK = "harness-events"
TOKEN_MIN = 20
DOTENV = "harness.env"
CARRIED = ("api", "web", "pipeline")
CARRIED_BODY = 1000
EVENT_ID = "HARNESS_EVENT_ID"
AGENT_TOKEN = "AGENT_GITLAB_TOKEN"
MARK = "HARNESS_PIPELINE"
EVENT_PIPELINE = "HARNESS_EVENT_PIPELINE"
EVENT_PIPELINE_AT = "HARNESS_EVENT_PIPELINE_AT"
REPEAT_LIMIT = 1000
REPEAT_SKEW = 60
IDLE_PIPELINES = ("canceled", "skipped")
TRUSTED_VARIABLE_ROLES = ("maintainer", "owner")
READY = "pipeline/ready"
HOOK_TIME = "%Y-%m-%d %H:%M:%S UTC"

VERDICT = re.compile(r"^<!-- harness-review: (approve|changes|comment) ([0-9a-f]{7,64}) -->[ \t]*$", re.MULTILINE)
COMMAND = re.compile(r"^([ \t]*)/", re.MULTILINE)
REVIEW_STATES = {"approve": "approved", "changes": "changes", "comment": "commented"}
CHANGE_STATES = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "closed"}
COMMIT_STATES = {"success": "success", "failed": "failure", "canceled": "error", "canceling": "error",
                 "skipped": "error"}
SET_STATES = {"pending": "running", "success": "success", "failure": "failed", "error": "canceled"}
JOB_STATES = {"success": "success", "failed": "failure", "canceled": "cancelled", "canceling": "cancelled",
              "skipped": "skipped", "manual": "skipped"}
FENCE = re.compile(r"^\s*(```|~~~)")
BLOCK = re.compile(r"^ {0,3}([#|<]|(-{3,}|={3,}|\*{3,}|_{3,})\s*$)|^( {4}|\t)")
OPENS = re.compile(r"^ {0,3}([#>|<]|[-*+] |\d+[.)] |(-{3,}|={3,}|\*{3,}|_{3,})\s*$)|^( {4}|\t)")
DRAFT = re.compile(r"\s*(?:draft:|\[draft\]|\(draft\)|wip:)\s*", re.IGNORECASE)
JOB_URL = re.compile(r"/-/jobs/(\d+)/?$")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BOT = re.compile(r"_bot(?:_[0-9a-f]+)?$")
MARKUP = re.compile(r"section_(?:start|end):\d+:[\w.-]+(?:\[[^\]]*\])?\r?|\x1b\[[0-9;]*[A-Za-z]")
CHANGE_IN_SUBJECT = re.compile(r"\((?P<project>[\w.-]+(?:/[\w.-]+)+)?!(?P<number>\d+)\)\s*$")
CHANGE_IN_BODY = re.compile(r"^See merge request (?P<project>[\w.-]+(?:/[\w.-]+)+)?!(?P<number>\d+)\s*$", re.MULTILINE)

TOOLS = forge.AgentTools(
    prefix="mcp__gitlab__",
    comments=(
        "create_merge_request_note",
        "create_note",
        "create_merge_request_thread",
        "create_merge_request_discussion_note",
        "create_issue_note",
    ),
    documents=("create_issue", "update_issue"),
    opens=("create_merge_request",),
    changes=("create_merge_request", "update_merge_request"),
    close=forge.CloseTool("update_merge_request", "state_event", "close"),
)


def quoted(value: object) -> str:
    return urllib.parse.quote(str(value), safe="")


def query(path: str, **params: object) -> str:
    return f"{path}?{urllib.parse.urlencode(params)}" if params else path


def inert(text: str) -> str:
    return COMMAND.sub(r"\1\\/", text)


def undrafted(title: str) -> str:
    while found := DRAFT.match(title):
        title = title[found.end():]
    return title


def continued(lines: list[str]) -> list[int]:
    found, fence = [], False
    for index, line in enumerate(lines[:-1]):
        if FENCE.match(line):
            fence = not fence
            continue
        after = lines[index + 1]
        if not (fence or not line.strip() or not after.strip() or BLOCK.match(line)
                or OPENS.match(after) or FENCE.match(after)):
            found.append(index)
    return found


def hard_breaks(text: str) -> str:
    lines = text.split("\n")
    for index in continued(lines):
        if not lines[index].endswith(("\\", "  ")):
            lines[index] += "\\"
    return "\n".join(lines)


def soft_breaks(text: str) -> str:
    lines = text.split("\n")
    for index in continued(lines):
        if lines[index].endswith("\\"):
            lines[index] = lines[index][:-1]
    return "\n".join(lines)


def declared(value: object) -> int | None:
    text = "" if value is None else str(value)
    return int(text) if text.isdigit() else None


def label_names(items) -> tuple[str, ...]:
    names = (item if isinstance(item, str) else (item or {}).get("name") for item in items or [])
    return tuple(name for name in names if name)


def latest(rows: list[dict]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in rows:
        name = row.get("name")
        if name and (name not in found or row.get("id", 0) > found[name].get("id", 0)):
            found[name] = row
    return found


def plain_log(text: str) -> str:
    return "\n".join(MARKUP.sub("", line).rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))


def hook_time(text: str) -> float | None:
    stamp = rest.utc(text)
    for form in (rest.STAMP, HOOK_TIME):
        try:
            return calendar.timegm(time.strptime(stamp, form))
        except ValueError:
            continue
    return None


def carried(event: forge.Event) -> str:
    fields = dataclasses.asdict(dataclasses.replace(event, body=(event.body or "")[:CARRIED_BODY]))
    return json.dumps(fields, ensure_ascii=False).replace("$", "\\u0024")


class Member:
    def __init__(self, check: Callable[[], bool]):
        self.check = check

    def __bool__(self) -> bool:
        return self.check()

    def __eq__(self, other: object) -> bool:
        return bool(self) == other

    def __hash__(self) -> int:
        return hash(bool(self))

    def __repr__(self) -> str:
        return repr(bool(self))

    def __deepcopy__(self, memo: dict) -> bool:
        return bool(self)


class GitLabForge(forge.Forge):
    name = "gitlab"
    tools = TOOLS
    git_user = "oauth2"

    token_names = ("GITLAB_TOKEN",)

    def __init__(self, settings: dict):
        self.settings = settings
        self.sections: list[str] = []
        self.user = ""
        self.agent = ""
        self.members: dict[int, bool] = {}
        self.project: dict | None = None
        self.merge_requests: dict[int, dict] = {}
        self.status_pipeline_api_ready = False
        self.protected: dict[str, bool] = {}

    def missing(self) -> str:
        project = self.repo()
        return "" if PROJECT.fullmatch(project) else msg("gitlab_bad_project", project or msg("gitlab_value_empty"))

    def repo(self) -> str:
        return os.environ.get("CI_PROJECT_PATH") or rest.literal(self.settings, "project")

    def server(self) -> str:
        return (os.environ.get("CI_SERVER_URL") or rest.literal(self.settings, "url") or SERVER).rstrip("/")

    def _url(self, *parts: object) -> str:
        return "/".join([f"{self.server()}/{self.repo()}/-", *map(str, parts)]) if self.repo() else ""

    def change_url(self, number: int | str) -> str:
        return self._url("merge_requests", number)

    def compare_url(self, base: str, head: str) -> str:
        return self._url("compare", f"{base}...{head}")

    def file_url(self, ref: str, path: str) -> str:
        return self._url("blob", ref, path)

    def run_url(self) -> str:
        if os.environ.get("CI_PIPELINE_URL"):
            return os.environ["CI_PIPELINE_URL"]
        return self._url("pipelines", self.run_id()) if self.run_id() else ""

    def change_ref(self, number: int | str) -> str:
        return f"!{number}"

    def _own(self, found: re.Match | None) -> int | None:
        if not found or (found.group("project") or self.repo()).lower() != self.repo().lower():
            return None
        return int(found.group("number"))

    def change_in_subject(self, subject: str) -> int | None:
        return self._own(CHANGE_IN_SUBJECT.search(subject))

    def change_in_message(self, message: str) -> int | None:
        subject, _, body = message.strip().partition("\n")
        found = self.change_in_subject(subject)
        return found if found is not None else self._own(CHANGE_IN_BODY.search(body))

    def clone_url(self) -> str:
        if self.missing():
            raise forge.ForgeUnavailable(self.missing())
        return f"{self.server()}/{self.repo()}.git"

    def _project(self) -> dict:
        if self.project is None:
            self.project = self._request("GET", "")
        return self.project

    def trusted_branch(self, event: forge.Event | None = None) -> str:
        if event is not None and event.target == "change" and event.number is not None:
            found = self._merge_request(event.number)
            if found is None:
                raise forge.ForgeError(msg("gitlab_change_unknown", event.number), 404)
            branch = str(found.get("target_branch") or "")
            if not self._protected(branch):
                raise forge.ForgeError(msg("gitlab_target_unprotected", self.change_ref(event.number), branch or "-"))
            return branch
        return str(get("git.baseBranch", "develop"))

    def _protected(self, branch: str) -> bool:
        if branch and branch not in self.protected:
            try:
                self.protected[branch] = self._request("GET", f"repository/branches/{quoted(branch)}").get("protected") is True
            except forge.ForgeError as error:
                if error.status != 404:
                    raise
                self.protected[branch] = False
        return bool(branch) and self.protected[branch]

    def _reaches(self, checkout: str, base: str) -> bool:
        if not checkout or not base:
            return False
        if checkout == base:
            return True
        found = self._request("GET", "repository/merge_base?" + urllib.parse.urlencode([("refs[]", checkout), ("refs[]", base)]))
        return str(found.get("id") or "") == checkout

    def publisher(self, number: int, head: str, base: str) -> bool:
        branch, checkout = os.environ.get("CI_COMMIT_BRANCH", ""), os.environ.get("CI_COMMIT_SHA", "")
        target = str((self._merge_request(number) or {}).get("target_branch") or "")
        if not target or target != branch or not self._protected(branch) or not self._reaches(checkout, base):
            print(msg("gitlab_publisher_refused", self.change_ref(number), branch or "-", checkout[:7] or "-",
                      target or "-", base[:7] or "-"), file=sys.stderr)
            return False
        return True

    def login(self) -> str:
        if not self.user:
            self.user = str(self._call("GET", "user").get("username") or "")
            if not self.user:
                raise forge.ForgeError(msg("gitlab_login_unknown"))
        return self.user

    def agent_login(self) -> str:
        token = os.environ.get(AGENT_TOKEN, "")
        if token and not self.agent:
            self.agent = str(self._call("GET", "user", token=token).get("username") or "")
            if not self.agent:
                raise forge.ForgeError(msg("gitlab_login_unknown"))
        return self.agent

    def setup_problems(self, base: str, release: str) -> list[str]:
        project = self._project()
        problems = []
        role = str(project.get("ci_pipeline_variables_minimum_override_role") or "")
        if role not in TRUSTED_VARIABLE_ROLES:
            problems.append(msg("gitlab_setup_variable_role", role or "-"))
        for branch in dict.fromkeys((base, release)):
            if not self._protected(branch):
                problems.append(msg("gitlab_setup_unprotected", branch))
        if not project.get("only_allow_merge_if_pipeline_succeeds"):
            problems.append(msg("gitlab_setup_pipeline_gate"))
        return problems

    def owner_steps(self, base: str, release: str) -> str:
        return msg("gitlab_owner_steps", base, release)

    def ci_files(self) -> list[str]:
        return [".gitlab-ci.yml", ".gitlab/ci/*.yml"]

    def in_ci(self) -> bool:
        return os.environ.get("GITLAB_CI") == "true"

    def run_id(self) -> str:
        return os.environ.get("CI_PIPELINE_ID", "")

    def run_attempt(self) -> str:
        return os.environ.get("CI_JOB_ID", "")

    def workspace(self) -> str:
        return os.environ.get("CI_PROJECT_DIR", "")

    def annotation(self, level: str, text: str) -> str:
        if not self.in_ci():
            return text
        color = {"error": "31", "warning": "33"}.get(level, "36")
        return f"\x1b[{color};1m{level.upper()}\x1b[0m {text}"

    def group(self, title: str) -> str | None:
        if not self.in_ci():
            return None
        name = f"harness_{len(self.sections)}"
        self.sections.append(name)
        return f"\x1b[0Ksection_start:{int(time.time())}:{name}[collapsed=true]\r\x1b[0K{title}"

    def end_group(self) -> str | None:
        if not self.in_ci() or not self.sections:
            return None
        return f"\x1b[0Ksection_end:{int(time.time())}:{self.sections.pop()}\r\x1b[0K"

    def _dotenv(self) -> str:
        if os.environ.get("HARNESS_DOTENV"):
            return os.environ["HARNESS_DOTENV"]
        if os.environ.get("CI_PROJECT_DIR"):
            return os.path.join(os.environ["CI_PROJECT_DIR"], DOTENV)
        raise forge.ForgeUnavailable(msg("forge_no_ci"))

    def set_output(self, name: str, value: str) -> None:
        if not NAME.fullmatch(name):
            raise forge.ForgeError(msg("gitlab_output_name", name))
        if "\n" in value or "\r" in value:
            raise forge.ForgeError(msg("gitlab_output_multiline", name))
        path = self._dotenv()
        try:
            with open(path, encoding="utf-8") as handle:
                kept = [line for line in handle.read().splitlines() if line.partition("=")[0] != name]
        except FileNotFoundError:
            kept = []
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join([*kept, f"{name}={value}"]) + "\n")

    def export_env(self, name: str, value: str) -> None:
        self.set_output(name, value)

    def run_jobs(self, run: str = "") -> dict[str, str]:
        pipeline = run or self.run_id()
        if not pipeline:
            raise forge.ForgeUnavailable(msg("forge_no_ci"))
        return {job["name"]: JOB_STATES.get(job.get("status"), "")
                for job in self._pages(f"pipelines/{quoted(pipeline)}/jobs") if job.get("name")}

    def checks(self, sha: str) -> list[forge.Check]:
        number = self._status_change(sha)
        if number is None:
            pipelines = self._request("GET", query("pipelines", sha=sha, order_by="id", sort="desc", per_page=1))
            pipeline = pipelines[0] if isinstance(pipelines, list) and pipelines else None
        else:
            pipeline = self._status_pipeline(sha, number)
        jobs = self._pages(f"pipelines/{pipeline['id']}/jobs") if pipeline is not None else []
        found = [forge.Check(job.get("name") or "", JOB_STATES.get(job.get("status"), "pending"),
                             job.get("web_url") or "", (job.get("user") or {}).get("username") or "", "job") for job in jobs]
        known = {job.get("id") for job in jobs}
        statuses = self._external_statuses(sha, pipeline) if number is not None else self._statuses(sha)
        external = [row for row in statuses
                    if row.get("id") not in known and not JOB_URL.search(row.get("target_url") or "")]
        found += [forge.Check(name, JOB_STATES.get(row.get("status"), "pending"), row.get("target_url") or "",
                              (row.get("author") or {}).get("username") or "", "status", row.get("description") or "")
                  for name, row in latest(external).items()]
        if number is not None:
            self._same_status_pipeline(sha, pipeline)
        return found

    def job_log(self, job: str) -> str:
        text, found = job.strip(), JOB_URL.search(job.strip())
        number = text if text.isdigit() else found.group(1) if found else ""
        if not number:
            raise forge.ForgeError(msg("gitlab_bad_job", job))
        trace = self._request("GET", f"jobs/{number}/trace", raw=True)
        return "\n".join(plain_log(trace).splitlines()[-forge.LOG_LINES:])

    def start_pipeline(self, variables: dict[str, str], event: forge.Event | None = None) -> str:
        variables = {**variables, MARK: "1"}
        branch = self.trusted_branch(event)
        if event is not None:
            variables["HARNESS_EVENT"] = carried(event)
            if event.id:
                variables[EVENT_ID] = event.id
        found = self._started(event, branch)
        if found:
            return found
        payload = {"ref": branch,
                   "variables": [{"key": key, "value": value, "variable_type": "env_var"}
                                 for key, value in variables.items()]}
        try:
            return str(self._request("POST", "pipeline", payload, ok=(200, 201)).get("web_url") or "")
        except forge.ForgeError as error:
            if error.status is not None and error.status < 500:
                raise
            try:
                found = self._started(event, branch)
            except forge.ForgeError:
                raise forge.ForgeUnsettled(str(error), error.status) from error
            if found:
                return found
            if event is None or not event.id:
                raise forge.ForgeUnsettled(str(error), error.status) from error
            raise

    def earlier_run(self) -> str:
        carried = os.environ.get("HARNESS_EVENT", "")
        own = os.environ.get(EVENT_PIPELINE) or self.run_id()
        made = os.environ.get(EVENT_PIPELINE_AT) or os.environ.get("CI_PIPELINE_CREATED_AT", "")
        if not carried or not own.isdigit():
            return ""
        return self._started(self._carried(carried), os.environ.get("CI_COMMIT_BRANCH") or self.trusted_branch(),
                             below=int(own), before=made)

    def _started(self, event: forge.Event | None, branch: str, below: int = 0, before: str = "") -> str:
        if event is None or not event.id:
            return ""
        stamp = hook_time(event.at)
        floor = int(event.run) if event.run.isdigit() else 0
        if stamp is None and not floor:
            raise forge.ForgeError(msg("gitlab_event_unbounded", event.id))
        params = {"ref": branch, "source": "api", "username": self.login(), "order_by": "id",
                  "sort": "desc"}
        if stamp is not None:
            params["created_after"] = time.strftime(rest.STAMP, time.gmtime(stamp - REPEAT_SKEW))
        made = hook_time(before) if before else None
        if made is not None:
            params["created_before"] = time.strftime(rest.STAMP, time.gmtime(made + REPEAT_SKEW))
        complete = False
        for listed in rest.each_page(lambda url: self._request("GET", url), query("pipelines", **params),
                                     forge.ForgeError, REPEAT_LIMIT // rest.PAGE):
            for pipeline in listed:
                number = int(pipeline.get("id") or 0)
                if number <= floor:
                    return ""
                if below and number >= below:
                    continue
                if pipeline.get("status") in IDLE_PIPELINES:
                    continue
                found = self._request("GET", f"pipelines/{quoted(pipeline.get('id'))}/variables")
                if any(item.get("key") == EVENT_ID and item.get("value") == event.id
                       for item in found if isinstance(found, list) and isinstance(item, dict)):
                    return str(pipeline.get("web_url") or "")
            complete = len(listed) < rest.PAGE
        if not complete:
            raise forge.ForgeError(msg("gitlab_event_scan_incomplete", event.id, REPEAT_LIMIT))
        return ""

    def event(self) -> forge.Event | None:
        if not self.in_ci():
            return None
        source = os.environ.get("CI_PIPELINE_SOURCE", "")
        if source == "trigger":
            return self._hook_event()
        carried = os.environ.get("HARNESS_EVENT", "")
        if carried and source in CARRIED:
            return self._carried(carried)
        if source == "schedule":
            return self._run_event("schedule")
        if source == "web":
            return self._run_event("manual")
        return None

    def _carried(self, text: str) -> forge.Event:
        try:
            fields = json.loads(text)
            fields["labels"] = tuple(fields.get("labels") or ())
            return forge.Event(**fields)
        except (ValueError, TypeError, AttributeError) as error:
            raise forge.ForgeError(msg("gitlab_bad_event", error)) from None

    def _run_event(self, kind: str) -> forge.Event:
        return forge.Event(kind=kind, target="", number=None, author=os.environ.get("GITLAB_USER_LOGIN", ""),
                           member=kind == "manual", bot=False, body="", labels=(), sha="", run="")

    def _hook_event(self) -> forge.Event | None:
        path = os.environ.get("TRIGGER_PAYLOAD", "")
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                hook = json.load(handle)
        except (OSError, ValueError) as error:
            raise forge.ForgeError(msg("gitlab_bad_payload", error)) from None
        if not isinstance(hook, dict):
            raise forge.ForgeError(msg("gitlab_bad_payload", type(hook).__name__))
        if hook.get("object_kind") == "merge_request":
            return self._label_event(hook)
        if hook.get("object_kind") == "note":
            return self._note_event(hook)
        return None

    def _label_event(self, hook: dict) -> forge.Event | None:
        change = hook.get("object_attributes") or {}
        changes = hook.get("changes") or {}
        if any(field in changes for field in ("title", "description", "target_branch")):
            updated = str(change.get("updated_at") or "")
            key = f"mr-inputs:{change.get('iid')}:{updated}" if updated else ""
            return self._hook_made(hook, "inputs", "change", change.get("iid"),
                                   sha=(change.get("last_commit") or {}).get("id") or "", key=key, at=updated)
        labels = changes.get("labels") or {}
        before = {item.get("title") for item in labels.get("previous") or []}
        if labels:
            current = labels.get("current")
        elif change.get("action") == "open":
            current = hook.get("labels")
        else:
            current = []
        added = tuple(title for item in current or [] if (title := item.get("title")) and title not in before)
        if not added:
            return None
        updated = str(change.get("updated_at") or "")
        key = f"mr:{change.get('iid')}:{change.get('action') or 'update'}:{updated}" if updated else ""
        return self._hook_made(hook, "label", "change", change.get("iid"), labels=added,
                               sha=(change.get("last_commit") or {}).get("id") or "", key=key, at=updated)

    def _note_event(self, hook: dict) -> forge.Event | None:
        note = hook.get("object_attributes") or {}
        if note.get("action", "create") != "create":
            return None
        body = soft_breaks(note.get("note") or "")
        key = f"note:{note['id']}" if note.get("id") else ""
        at = str(note.get("created_at") or "")
        if note.get("noteable_type") == "MergeRequest":
            change = hook.get("merge_request") or {}
            kind = "review" if note.get("type") == "DiffNote" or VERDICT.search(body) else "note"
            return self._hook_made(hook, kind, "change", change.get("iid"), body=body,
                                   sha=(change.get("last_commit") or {}).get("id") or "", key=key, at=at)
        return None

    def _hook_made(self, hook: dict, kind: str, target: str, number, body: str = "", labels: tuple[str, ...] = (),
                   sha: str = "", key: str = "", at: str = "") -> forge.Event:
        user = hook.get("user") or {}
        author = str(user.get("username") or "")
        calls = bool(labels) or forge.MENTION.match(body) is not None
        bot = bool(user.get("bot")) or bool(BOT.search(author)) or (calls and author == self.login())
        return forge.Event(kind=kind, target=target, number=None if number is None else int(number), author=author,
                           member=calls and self._member(user.get("id")), bot=bot, body=body, labels=labels, sha=sha,
                           run="", id=key, at=at)

    def ensure_event_hook(self) -> str:
        branch = self.trusted_branch()
        target = f"{self._api()}/projects/{self._project()['id']}/ref/{quoted(branch)}/trigger/pipeline?token="
        marked = "&" + urllib.parse.urlencode({f"variables[{MARK}]": "1"})
        trigger = next((item for item in self._pages("triggers") if item.get("description") == HOOK
                        and len(str(item.get("token") or "")) >= TOKEN_MIN), None)
        project = f"{self._api()}/projects/{self._project()['id']}/ref/"
        own = f"/trigger/pipeline?token={trigger['token']}" if trigger else ""
        hooks = [item for item in self._pages("hooks") if str(item.get("url") or "").startswith(target)
                 or own and str(item.get("url") or "").startswith(project) and own in str(item.get("url") or "")]
        wanted = {"merge_requests_events": True, "note_events": True, "push_events": False,
                  "enable_ssl_verification": True}
        kept = next((hook for hook in hooks if trigger and hook.get("url") == target + trigger["token"] + marked
                     and all(hook.get(key) == value for key, value in wanted.items())), None)
        if kept is None:
            if trigger is None:
                trigger = self._request("POST", "triggers", {"description": HOOK}, ok=(200, 201))
            payload = {"url": target + str(trigger.get("token") or "") + marked, **wanted}
            if hooks:
                kept = self._request("PUT", f"hooks/{hooks[0]['id']}", payload)
            else:
                kept = self._request("POST", "hooks", payload, ok=(200, 201))
            status = msg("gitlab_hook_made", branch, kept.get("id"), trigger.get("id"))
        else:
            status = msg("gitlab_hook_kept", branch, kept.get("id"), trigger.get("id"))
        for hook in hooks:
            if hook.get("id") != kept.get("id"):
                self._request("DELETE", f"hooks/{hook['id']}", ok=(200, 204))
        return status

    def _api(self) -> str:
        return (os.environ.get("CI_API_V4_URL") or f"{self.server()}/api/v4").rstrip("/")

    def _call(self, method: str, resource: str, payload=None, ok: tuple[int, ...] = (200,), raw: bool = False,
              token: str = ""):
        if self.missing():
            raise forge.ForgeUnavailable(self.missing())
        headers = {"PRIVATE-TOKEN": token or self._token(), "User-Agent": "harness-gitlab-adapter"}
        return rest.request(method, f"{self._api()}/{resource}", headers, forge.ForgeError, payload, ok, raw=raw)

    def _request(self, method: str, path: str, payload=None, ok: tuple[int, ...] = (200,), raw: bool = False):
        project = f"projects/{quoted(self.repo())}"
        return self._call(method, f"{project}/{path}" if path else project, payload, ok, raw)

    def _pages(self, path: str) -> list:
        return rest.pages(lambda url: self._request("GET", url), path, forge.ForgeError)

    def _member(self, user) -> bool:
        if not isinstance(user, int):
            return False
        if user not in self.members:
            try:
                level = self._request("GET", f"members/all/{user}").get("access_level") or 0
            except forge.ForgeError as error:
                if error.status != 404:
                    raise
                level = 0
            self.members[user] = int(level) >= DEVELOPER
        return self.members[user]

    def _merge_request(self, number: int | str) -> dict | None:
        number = int(number)
        try:
            self.merge_requests[number] = self._request("GET", f"merge_requests/{number}")
        except forge.ForgeError as error:
            if error.status == 404:
                self.merge_requests.pop(number, None)
                return None
            raise
        return self.merge_requests[number]

    def _changed(self, number: int | str) -> None:
        self.merge_requests.pop(int(number), None)

    def _as_change(self, item: dict) -> forge.Change:
        number = int(item["iid"])
        return forge.Change(
            number=number, title=item.get("title") or "", body=soft_breaks(item.get("description") or ""),
            state=CHANGE_STATES.get(str(item.get("state")), "closed"),
            draft=bool(item.get("work_in_progress") if item.get("draft") is None else item["draft"]),
            labels=label_names(item.get("labels")), base=item.get("target_branch") or "",
            head=item.get("source_branch") or "",
            head_sha=(item.get("diff_refs") or {}).get("head_sha") or item.get("sha") or "",
            fetch_ref=f"refs/merge-requests/{number}/head", author=(item.get("author") or {}).get("username", ""),
            url=item.get("web_url") or self.change_url(number), files=declared(item.get("changes_count")),
            base_sha=self.branch_head(item.get("target_branch") or "") if item.get("state") == "opened" else "",
            merge_sha=item.get("merge_commit_sha") or item.get("squash_commit_sha") or "",
            created=item.get("created_at") or "",
            cross_repo=item.get("source_project_id") != item.get("target_project_id"),
            squash_sha=item.get("squash_commit_sha") or "")

    def change(self, number: int) -> forge.Change | None:
        found = self._merge_request(number)
        return self._as_change(found) if found is not None else None

    def historical_heads(self, pull: forge.Change, before: int) -> list[str]:
        if pull.cross_repo:
            return []
        return sorted({forge.observed_head(item.get("head_commit_sha"), item.get("created_at"), before)
                       for item in self._pages(f"merge_requests/{pull.number}/versions")} - {""})

    def change_files(self, number: int) -> list[str] | None:
        try:
            found = self._merge_request(number)
            files = declared(found.get("changes_count")) if found else None
            diffs = self._pages(f"merge_requests/{int(number)}/diffs") if files is not None else []
        except forge.ForgeError:
            return None
        paths = [(item.get("old_path") if item.get("deleted_file") else item.get("new_path")) or "" for item in diffs]
        return paths if len(paths) == files else None

    def readable(self, text: str) -> str:
        return soft_breaks(text)

    def merged_title(self, title: str) -> str:
        return undrafted(title)

    def change_body(self, number: int) -> str:
        found = self._merge_request(number)
        if found is None:
            raise forge.ForgeError(msg("gitlab_change_unknown", number), 404)
        return soft_breaks(found.get("description") or "")

    def open_change(self, head: str, base: str, title: str, body: str, draft: bool = True,
                    labels: Sequence[str] = ()) -> forge.Change:
        title = undrafted(title)
        payload = {"source_branch": head, "target_branch": base, "title": f"Draft: {title}" if draft else title,
                   "description": hard_breaks(body)}
        if labels:
            payload["labels"] = ",".join(labels)
        return self._as_change(self._request("POST", "merge_requests", payload, ok=(200, 201)))

    def edit_change(self, number: int, body: str) -> None:
        self._update(number, {"description": hard_breaks(body)})

    def mark_ready(self, number: int) -> None:
        found = self._merge_request(number)
        if found is None:
            raise forge.ForgeError(msg("gitlab_change_unknown", number), 404)
        title = undrafted(found.get("title") or "")
        if title != found.get("title"):
            self._update(number, {"title": title})

    def close_change(self, number: int) -> None:
        self._update(number, {"state_event": "close"})

    def _update(self, number: int, fields: dict) -> None:
        self._changed(number)
        self._request("PUT", f"merge_requests/{int(number)}", fields)

    def add_label(self, number: int, name: str) -> None:
        self._update(number, {"add_labels": name})

    def remove_label(self, number: int, name: str) -> None:
        self._update(number, {"remove_labels": name})

    def _notes(self, number: int) -> list[dict]:
        path = query(f"merge_requests/{int(number)}/notes", sort="asc", order_by="created_at")
        return [note for note in self._pages(path) if not note.get("system")]

    def _verdict(self, note: dict) -> re.Match | None:
        return VERDICT.search(note.get("body") or "")

    def notes(self, number: int) -> list[forge.Note]:
        found = []
        for note in self._notes(number):
            if note.get("type") == "DiffNote" or self._verdict(note):
                continue
            user = note.get("author") or {}
            found.append(forge.Note(id=str(note.get("id")), author=user.get("username", ""),
                                    member=Member(functools.partial(self._member, user.get("id"))),
                                    created=rest.utc(note.get("created_at") or ""), body=soft_breaks(note.get("body") or ""),
                                    bot=bool(user.get("bot")), system=bool(note.get("system"))))
        return found

    def task_notes(self, number: int) -> list[forge.Note]:
        records = {str(note.get("id")): note for note in self._notes(number)}
        for discussion in self._pages(f"merge_requests/{int(number)}/discussions"):
            notes = discussion.get("notes") or []
            position = next((note["position"] for note in notes if note.get("position")), {})
            for note in notes:
                records[str(note.get("id"))] = {**note, "position": note.get("position") or position}
        found = []
        for note in records.values():
            user, position = note.get("author") or {}, note.get("position") or {}
            found.append(forge.Note(id=str(note.get("id") or ""), author=user.get("username") or "", member=False,
                                   created=rest.utc(note.get("created_at") or ""), body=soft_breaks(note.get("body") or ""),
                                   bot=bool(user.get("bot")), system=bool(note.get("system")),
                                   path=position.get("new_path") or position.get("old_path") or "",
                                   line=position.get("new_line") or position.get("old_line")))
        return found

    def reviews(self, number: int) -> list[forge.Review]:
        found = []
        for note in self._notes(number):
            mark = self._verdict(note)
            if mark:
                found.append(forge.Review(created=rest.utc(note.get("created_at") or ""),
                                          state=REVIEW_STATES[mark.group(1)], sha=mark.group(2),
                                          author=(note.get("author") or {}).get("username") or "",
                                          body=soft_breaks((note.get("body") or "")[:mark.start()].rstrip())))
        return found

    def submit_review(self, number: int, sha: str, decision: str, body: str,
                      comments: Sequence[forge.InlineComment] = (), rejected=None) -> None:
        if decision not in REVIEW_STATES:
            raise forge.ForgeError(msg("gitlab_bad_decision", decision))
        if decision == "changes":
            self._unapprove(int(number))
        refused = self._inline(int(number), sha, comments) if comments else []
        text = inert(rejected(refused) if rejected and refused else forge.inline_fallback(body.rstrip(), refused))
        text += f"\n\n<!-- harness-review: {decision} {sha} -->"
        self._changed(number)
        self._request("POST", f"merge_requests/{int(number)}/notes", {"body": hard_breaks(text)}, ok=(200, 201))

    def _inline(self, number: int, sha: str, comments: Sequence[forge.InlineComment]) -> list[forge.InlineComment]:
        refs = (self._merge_request(number) or {}).get("diff_refs") or {}
        if not sha or not str(refs.get("head_sha") or "").startswith(sha):
            return list(comments)
        try:
            renamed = {item.get("new_path"): item.get("old_path")
                       for item in self._pages(f"merge_requests/{number}/diffs") if item.get("renamed_file")}
        except forge.ForgeError:
            renamed = {}
        posted = self._posted(number)
        rejected = []
        for item in comments:
            if item.key and any(item.key in body for body in posted.get((item.path, item.line), ())):
                continue
            position = {"position_type": "text", "base_sha": refs.get("base_sha"),
                        "start_sha": refs.get("start_sha"), "head_sha": refs.get("head_sha"),
                        "old_path": renamed.get(item.path, item.path), "new_path": item.path,
                        ("old_line" if item.side == forge.LEFT else "new_line"): item.line}
            try:
                text = inert(item.body) + (f"\n\n{item.key}" if item.key else "")
                self._request("POST", f"merge_requests/{number}/discussions",
                              {"body": hard_breaks(text), "position": position}, ok=(200, 201))
            except forge.ForgeError as error:
                if error.status not in (400, 422):
                    raise
                rejected.append(item)
        return rejected

    def _posted(self, number: int) -> dict[tuple[str, int | None], list[str]]:
        found: dict[tuple[str, int | None], list[str]] = {}
        for thread in self.review_threads(number) or ():
            if thread.author == self.login():
                found.setdefault((thread.path, thread.line), []).append(thread.body)
        return found

    def _approve(self, number: int, sha: str) -> None:
        try:
            self._request("POST", f"merge_requests/{number}/approve", {"sha": sha}, ok=(200, 201))
        except forge.ForgeError as error:
            if error.status not in (401, 403) or not self._approved(number):
                raise

    def _approved(self, number: int) -> bool:
        approvals = self._request("GET", f"merge_requests/{number}/approvals")
        return any((item.get("user") or {}).get("username") == self.login()
                   for item in approvals.get("approved_by") or [])

    def _unapprove(self, number: int) -> None:
        try:
            self._request("POST", f"merge_requests/{number}/unapprove", ok=(200, 201))
        except forge.ForgeError as error:
            if error.status not in (401, 403, 404):
                raise

    def review_threads(self, number: int) -> list[forge.Thread] | None:
        change = self._merge_request(number)
        if change is None:
            return None
        try:
            discussions = self._pages(f"merge_requests/{int(number)}/discussions")
        except forge.ForgeError as error:
            if error.status == 404:
                return None
            raise
        head = (change.get("diff_refs") or {}).get("head_sha") or change.get("sha") or ""
        found = []
        for discussion in discussions:
            notes = [note for note in discussion.get("notes") or [] if not note.get("system")]
            resolvable = [note for note in notes if note.get("resolvable")]
            if not resolvable:
                continue
            first = notes[0]
            position = first.get("position") or {}
            found.append(forge.Thread(
                id=f"{number}:{discussion['id']}",
                path=position.get("new_path") or position.get("old_path") or "",
                line=position.get("new_line") or position.get("old_line"),
                resolved=all(note.get("resolved") for note in resolvable),
                outdated=bool(position) and position.get("head_sha") not in ("", None, head),
                author=(first.get("author") or {}).get("username", ""),
                body=soft_breaks(first.get("body") or ""),
                url=f"{self.change_url(number)}#note_{first.get('id')}"))
        return found

    def _discussion(self, thread: str) -> str:
        number, _, discussion = thread.partition(":")
        if not number.isdigit() or not discussion:
            raise forge.ForgeError(msg("gitlab_bad_thread", thread))
        self._changed(number)
        return f"merge_requests/{number}/discussions/{discussion}"

    def reply_thread(self, thread: str, body: str) -> str:
        note = self._request("POST", f"{self._discussion(thread)}/notes", {"body": hard_breaks(body)}, ok=(200, 201))
        return f"{self.change_url(thread.partition(':')[0])}#note_{note.get('id')}"

    def resolve_thread(self, thread: str) -> str:
        self._request("PUT", f"{self._discussion(thread)}?resolved=true")
        return thread

    def _own_notes(self, number: int, marker: str) -> list[int]:
        notes = [note for note in self._notes(number) if marker in (note.get("body") or "")]
        login = self.login() if notes else ""
        return [note["id"] for note in notes if (note.get("author") or {}).get("username") == login]

    def comment(self, number: int, body: str, marker: str = "", last: bool = False) -> None:
        notes, body = f"merge_requests/{number}/notes", hard_breaks(body)
        found = self._own_notes(number, marker) if marker else []
        created = None
        if last or not found:
            self._changed(number)
            created = self._request("POST", notes, {"body": body}, ok=(200, 201)).get("id")
            if not marker:
                return
            if last:
                self._drop(notes, found)
                return
            found = self._own_notes(number, marker)
        self._changed(number)
        if found and found[0] != created:
            self._request("PUT", f"{notes}/{found[0]}", {"body": body})
        self._drop(notes, found[1:])

    def _drop(self, notes: str, ids: list[int]) -> None:
        for extra in ids:
            try:
                self._request("DELETE", f"{notes}/{extra}", ok=(200, 204))
            except forge.ForgeError as error:
                if error.status != 404:
                    raise

    def _statuses(self, sha: str, **params: object) -> list[dict]:
        return self._pages(query(f"repository/commits/{quoted(sha)}/statuses", **params))

    def _status_pipeline_api(self) -> None:
        if self.status_pipeline_api_ready:
            return
        version = str(self._call("GET", "version").get("version") or "")
        match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
        if not match or tuple(map(int, match.groups())) < (17, 9):
            raise forge.ForgeError(msg("gitlab_status_pipeline_api", version))
        self.status_pipeline_api_ready = True

    def _status_change(self, sha: str) -> int | None:
        context = [os.environ[key] for key in ("HARNESS_CHANGE", "HARNESS_REVIEW", "HARNESS_INVALIDATE",
                                               "CI_MERGE_REQUEST_IID") if os.environ.get(key)]
        event = self.event()
        if event is not None and event.target == "change" and event.number is not None:
            context.append(str(event.number))
        if any(not value.isdigit() or int(value) < 1 for value in context):
            raise forge.ForgeError(msg("gitlab_status_pipeline_invalid", sha[:7]))
        numbers = {int(value) for value in context}
        if not numbers:
            numbers = {number for number, item in self.merge_requests.items()
                       if item.get("state") == "opened"
                       and ((item.get("diff_refs") or {}).get("head_sha") or item.get("sha")) == sha}
        if not numbers:
            numbers = {int(item["iid"]) for item in self._pages(f"repository/commits/{quoted(sha)}/merge_requests")
                       if item.get("state") == "opened"}
        if not numbers:
            return None
        if len(numbers) != 1:
            raise forge.ForgeError(msg("gitlab_status_pipeline_invalid", sha[:7]))
        return next(iter(numbers))

    def _status_pipeline(self, sha: str, number: int | None = None) -> dict:
        self._status_pipeline_api()
        number = number if number is not None else self._status_change(sha)
        if number is None:
            raise forge.ForgeError(msg("gitlab_status_pipeline_invalid", sha[:7]))
        change = self._request("GET", f"merge_requests/{number}")
        if (change.get("state") != "opened"
                or ((change.get("diff_refs") or {}).get("head_sha") or change.get("sha")) != sha):
            raise forge.ForgeError(msg("gitlab_status_pipeline_invalid", sha[:7]))
        head = change.get("head_pipeline") or {}
        if not head.get("id"):
            raise forge.ForgeUnavailable(msg("gitlab_status_pipeline_missing", number, sha[:7]))
        pipeline = self._request("GET", f"pipelines/{quoted(head['id'])}")
        project = self._project().get("id")
        pipelines = self._request("GET", query(f"merge_requests/{number}/pipelines", page=1, per_page=100))
        if (not isinstance(pipeline.get("id"), int) or pipeline["id"] != head["id"]
                or not project or pipeline.get("project_id") != project or head.get("sha") != sha
                or pipeline.get("sha") != sha or pipeline.get("source") != "merge_request_event"
                or not pipeline.get("ref") or pipeline.get("ref") != head.get("ref")
                or not isinstance(pipelines, list)
                or not any(item.get("id") == pipeline["id"] and item.get("sha") == sha
                           for item in pipelines)):
            raise forge.ForgeError(msg("gitlab_status_pipeline_invalid", sha[:7]))
        return pipeline

    def _same_status_pipeline(self, sha: str, pipeline: dict) -> None:
        current = self._status_pipeline(sha)
        if (current["id"], current["ref"]) != (pipeline["id"], pipeline["ref"]):
            raise forge.ForgeError(msg("gitlab_status_pipeline_invalid", sha[:7]))

    def _external_statuses(self, sha: str, pipeline: dict, **params: object) -> list[dict]:
        jobs = self._pages(query(f"pipelines/{pipeline['id']}/jobs", include_retried="true"))
        native = {job.get("id") for job in jobs}
        return [row for row in self._statuses(sha, pipeline_id=pipeline["id"], ref=pipeline["ref"], **params)
                if row.get("id") not in native and not JOB_URL.search(row.get("target_url") or "")]

    def _post_status(self, sha: str, payload: dict) -> dict:
        return self._request("POST", f"statuses/{quoted(sha)}", payload, ok=(200, 201))

    def commit_results(self, sha: str) -> dict[str, forge.Check]:
        pipeline = self._status_pipeline(sha)
        found = {name: forge.Check(name, COMMIT_STATES.get(row.get("status"), "pending"), str(row.get("target_url") or ""),
                                 (row.get("author") or {}).get("username") or "", "status", row.get("description") or "")
                 for name, row in latest(self._external_statuses(sha, pipeline, all="true")).items()}
        self._same_status_pipeline(sha, pipeline)
        return found

    def set_commit_state(self, sha: str, context: str, state: str, description: str, url: str = "") -> None:
        if state not in SET_STATES:
            raise forge.ForgeError(msg("gitlab_bad_state", state))
        pipeline = self._status_pipeline(sha)
        payload = {"state": SET_STATES[state], "name": context, "description": description[:MAX_DESCRIPTION],
                   "pipeline_id": pipeline["id"], "ref": pipeline["ref"]}
        if url:
            payload["target_url"] = url
        try:
            self._post_status(sha, payload)
        except forge.ForgeError as error:
            if error.status != 400 or "Cannot transition status" not in str(error):
                raise
            row = latest(self._external_statuses(sha, pipeline, all="true", name=context)).get(context) or {}
            if row.get("status") != payload["state"]:
                raise
            if row.get("description") == payload["description"] and (not url or row.get("target_url") == url):
                self._same_status_pipeline(sha, pipeline)
                self._sync_ready(sha, context, state)
                return
            self._post_status(sha, {**payload, "state": SET_STATES["error"]})
            self._post_status(sha, payload)
        observed = latest(self._external_statuses(sha, pipeline, all="true", name=context)).get(context) or {}
        if (observed.get("status") != payload["state"] or observed.get("description") != payload["description"]
                or (url and observed.get("target_url") != url)):
            raise forge.ForgeError(msg("gitlab_status_unwritten", context, sha[:7]))
        self._same_status_pipeline(sha, pipeline)
        self._sync_ready(sha, context, state)

    def _sync_ready(self, sha: str, context: str, state: str) -> None:
        number = self._status_change(sha) if context == READY else None
        if number is not None and state == "success":
            self._approve(number, sha)
        elif number is not None:
            self._unapprove(number)

    def branch_head(self, branch: str) -> str:
        found = self._request("GET", f"repository/branches/{quoted(branch)}")
        return str((found.get("commit") or {}).get("id") or "")

    def open_changes(self, branch: str) -> list[int]:
        numbers: set[int] = set()
        for field in ("source_branch", "target_branch"):
            for item in self._pages(query("merge_requests", state="opened", **{field: branch})):
                if field == "source_branch" and item.get("source_project_id") != item.get("target_project_id"):
                    continue
                numbers.add(int(item["iid"]))
        return sorted(numbers)

    def merged_changes(self, branch: str) -> list[int]:
        return sorted(int(item["iid"]) for item in self._pages(query("merge_requests", state="merged", source_branch=branch))
                      if item.get("source_project_id") == item.get("target_project_id"))

    def create_branch(self, branch: str, sha: str) -> None:
        self._request("POST", "repository/branches", {"branch": branch, "ref": sha}, ok=(200, 201))

    def delete_branch(self, branch: str) -> None:
        self._request("DELETE", f"repository/branches/{quoted(branch)}", ok=(200, 204))

    def create_tag(self, tag: str, sha: str) -> None:
        self._request("POST", "repository/tags", {"tag_name": tag, "ref": sha}, ok=(200, 201))

    def labels(self) -> set[str]:
        return {item["name"] for item in self._pages("labels") if item.get("name")}

    def create_label(self, name: str, description: str) -> None:
        self._request("POST", "labels", {"name": name, "color": LABEL_COLOR, "description": description},
                      ok=(200, 201))

    def secret_names(self, environment: str = "") -> set[str]:
        return {item["key"] for item in self._pages("variables")
                if item.get("key") and (not environment or item.get("environment_scope") in ("*", environment))}
