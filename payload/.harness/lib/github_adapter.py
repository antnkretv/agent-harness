from __future__ import annotations

from github_transport import Api, REPO, SERVER, TOKEN, rest_login
from github_evidence import ReviewEvidence

import dataclasses
import json
import os
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timezone

import forge
import rest
import tracker
from manifest import get
from platform_messages import msg

MAX_DESCRIPTION = 140
CHANGE_IN_SUBJECT = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+) from ")
KEYWORDS = r"clos(?:e|es|ed)|fix(?:|es|ed)|resolv(?:e|es|ed)"
REFERENCE = re.compile(rf"\b({KEYWORDS}|refs)\s+({REPO.pattern})?#(\d+)\b", re.I)
DECISIONS = {"approve": "APPROVE", "changes": "REQUEST_CHANGES", "comment": "COMMENT"}
REVIEW_STATES = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes", "COMMENTED": "commented"}
MEMBERS = ("OWNER", "MEMBER")
CONCLUSIONS = {
    "success": "success", "neutral": "success",
    "failure": "failure", "action_required": "failure", "startup_failure": "failure",
    "cancelled": "cancelled", "timed_out": "cancelled", "stale": "cancelled",
    "skipped": "skipped",
}
JOB = re.compile(r"^(\d+)$|/jobs?/(\d+)")
CCR = "ccr"

TOOLS = forge.AgentTools(
    prefix="mcp__github__",
    comments=(
        "add_issue_comment",
        "add_reply_to_pull_request_comment",
        "add_comment_to_pending_review",
        "pull_request_review_write",
    ),
    documents=("issue_write",),
    merge=forge.MergeTool("merge_pull_request", "pullNumber", "commit_message", "merge_method", "rebase"),
    opens=("create_pull_request",),
    changes=("create_pull_request", "update_pull_request"),
    close=forge.CloseTool("update_pull_request", "state", "closed"),
)

THREADS = """query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          path
          line
          isResolved
          isOutdated
          comments(first: 1) { nodes { author { __typename login } body url } }
        }
      }
    }
  }
}"""

REPLY = """mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: {pullRequestReviewThreadId: $thread, body: $body}
  ) { comment { url } }
}"""

RESOLVE = """mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
}"""

READY = """mutation($id: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } }
}"""

ISSUE_BRANCHES = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      linkedBranches(first: 50) { nodes { id ref { name repository { nameWithOwner } } } }
    }
  }
}
"""

REPOSITORY = """
query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }
"""

LINK = """
mutation($issueId: ID!, $oid: GitObjectID!, $name: String!, $repositoryId: ID!) {
  createLinkedBranch(input: {issueId: $issueId, oid: $oid, name: $name, repositoryId: $repositoryId}) {
    linkedBranch { id ref { name } }
  }
}
"""

UNLINK = """
mutation($linkedBranchId: ID!) {
  deleteLinkedBranch(input: {linkedBranchId: $linkedBranchId}) {
    issue { number }
  }
}
"""


def same_repo(left: str, right: str) -> bool:
    return left.lower() == right.lower()


def in_repo(node: dict, repo: str) -> bool:
    return same_repo((node.get("repository") or {}).get("nameWithOwner", ""), repo)


def names(labels) -> tuple[str, ...]:
    return tuple(label.get("name") or "" for label in labels or [])


def command_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def ccr_comment(thread: str) -> tuple[str, str] | None:
    kind, _, rest_id = thread.partition(":")
    number, _, comment = rest_id.partition(":")
    return (number, comment) if kind == CCR and number and comment else None


def as_thread(node: dict) -> forge.Thread:
    comments = (node.get("comments") or {}).get("nodes") or [{}]
    first = comments[0] or {}
    return forge.Thread(id=node["id"], path=node.get("path") or "", line=node.get("line"),
                        resolved=bool(node.get("isResolved")), outdated=bool(node.get("isOutdated")),
                        author=rest_login(first.get("author") or {}), body=first.get("body") or "", url=first.get("url") or "")


def as_issue(item: dict) -> tracker.Issue:
    return tracker.Issue(key=str(item["number"]), title=item.get("title") or "", body=item.get("body") or "",
                         url=item.get("html_url") or "", labels=names(item.get("labels")),
                         created=item.get("created_at") or "", open=item.get("state") == "open",
                         assignees=tuple(person.get("login") or "" for person in item.get("assignees") or []),
                         author=(item.get("user") or {}).get("login") or "")


def as_change(pull: dict) -> forge.Change:
    number, head, files = int(pull["number"]), pull.get("head") or {}, pull.get("changed_files")
    state = "merged" if pull.get("merged_at") else "open" if pull.get("state") == "open" else "closed"
    return forge.Change(number=number, title=pull.get("title") or "", body=pull.get("body") or "", state=state,
                        draft=bool(pull.get("draft")), labels=names(pull.get("labels")),
                        base=(pull.get("base") or {}).get("ref") or "", head=head.get("ref") or "",
                        head_sha=head.get("sha") or "", fetch_ref=f"refs/pull/{number}/head",
                        author=(pull.get("user") or {}).get("login") or "", url=pull.get("html_url") or "",
                        files=files if isinstance(files, int) else None,
                        base_sha=(pull.get("base") or {}).get("sha") or "", merge_sha=pull.get("merge_commit_sha") or "", created=pull.get("created_at") or "",
                        cross_repo=(head.get("repo") or {}).get("full_name") != ((pull.get("base") or {}).get("repo") or {}).get("full_name"))


def said(item: dict) -> dict:
    return {"author": (item.get("user") or {}).get("login") or "",
            "member": item.get("author_association") in MEMBERS, "body": item.get("body") or ""}


def happened(payload: dict, kind: str, **fields) -> forge.Event:
    sender = payload.get("sender") or {}
    actor = os.environ.get("GITHUB_ACTOR") or sender.get("login") or ""
    event = {"target": "", "number": None, "author": sender.get("login") or actor, "member": False, "body": "",
             "labels": (), "sha": "", "run": "", **fields}
    return forge.Event(kind=kind, bot=actor.endswith("[bot]") or sender.get("type") == "Bot", **event)


def on_label(payload: dict) -> forge.Event | None:
    pull, label = payload.get("pull_request") or {}, (payload.get("label") or {}).get("name")
    if payload.get("action") != "labeled" or not pull or not label:
        return None
    return happened(payload, "label", target="change", number=pull.get("number"), member=True, labels=(label,),
                    sha=(pull.get("head") or {}).get("sha") or "")


def on_comment(payload: dict) -> forge.Event | None:
    issue = payload.get("issue") or {}
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment") or {}
    return happened(payload, "note", target="change" if issue.get("pull_request") else "issue",
                    number=issue.get("number"), id=str(comment.get("id") or ""), **said(comment))


def on_review(payload: dict, field: str, action: str) -> forge.Event | None:
    pull, item = payload.get("pull_request") or {}, payload.get(field) or {}
    if payload.get("action") != action:
        return None
    return happened(payload, "review", target="change", number=pull.get("number"),
                    id=f"{field}:{item.get('id') or ''}",
                    sha=item.get("commit_id") or (pull.get("head") or {}).get("sha") or "", **said(item))


def round_title(number: int | str) -> str:
    return f"code-review {forge.review_label()} #{number}"


def on_round(payload: dict) -> forge.Event | None:
    run = payload.get("workflow_run") or {}
    if payload.get("action") != "completed" or run.get("conclusion") == "cancelled":
        return None
    pulls = run.get("pull_requests") or [{}]
    number = pulls[0].get("number")
    if number is None:
        title, prefix = run.get("display_title") or "", round_title("")
        number = int(title[len(prefix):]) if title.startswith(prefix) and title[len(prefix):].isdigit() else None
    return happened(payload, "round", target="change" if number else "", number=number,
                    sha=run.get("head_sha") or "", run=str(run.get("id") or ""))


EVENTS = {
    "pull_request": on_label,
    "pull_request_target": on_label,
    "issue_comment": on_comment,
    "pull_request_review": lambda payload: on_review(payload, "review", "submitted"),
    "pull_request_review_comment": lambda payload: on_review(payload, "comment", "created"),
    "workflow_run": on_round,
    "schedule": lambda payload: happened(payload, "schedule"),
    "workflow_dispatch": lambda payload: happened(payload, "manual", member=True),
}


class GitHubForge(forge.Forge):
    def round_title(self, number: int | str) -> str:
        return round_title(number)

    name = "github"
    tools = TOOLS
    git_user = "x-access-token"
    token_names = (TOKEN,)

    def __init__(self, settings: dict):
        self.settings = settings
        self.api = Api(self.repo(), forge.ForgeError, forge.ForgeUnavailable, self._server())
        self.evidence = ReviewEvidence(self)

    def review_run(self, number: int, url: str, snapshot: dict) -> dict:
        return self.evidence.review_run(number, url, snapshot)

    def review_artifact(self, url: str, name: str) -> dict[str, bytes]:
        return self.evidence.review_artifact(url, name)

    def review_jobs(self, url: str) -> list:
        return self.evidence.review_jobs(url)

    def review_requested(self, number: int, head: str) -> bool:
        return self.evidence.review_requested(number, head)

    def missing(self) -> str:
        return self.api.missing()

    def _call(self, method: str, *parts: str, payload=None):
        body = None if payload is None else json.dumps(payload)
        output = self.api.gh("api", "-X", method, self.api.path(*parts), *(["--input", "-"] if body else []),
                             payload=body, read_only=method == "GET")
        return json.loads(output) if output.strip() else {}

    def _items(self, *parts: str, key: str = "", params: dict | None = None, token: str | None = None) -> list:
        def page(path: str):
            found = json.loads(self.api.gh("api", path, read_only=True, token=token) or "{}")
            return (found.get(key) or []) if key and isinstance(found, dict) else found

        path = self.api.path(*parts)
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return rest.pages(page, path, forge.ForgeError)

    def repo(self) -> str:
        return os.environ.get("GITHUB_REPOSITORY") or rest.literal(self.settings, "repo")

    def _server(self) -> str:
        return (os.environ.get("GITHUB_SERVER_URL") or rest.literal(self.settings, "url") or SERVER).rstrip("/")

    def _url(self, *parts: object) -> str:
        base = f"{self.api.server}/{self.repo()}" if self.repo() else ""
        return "/".join([base, *map(str, parts)]) if base else ""

    def change_url(self, number: int | str) -> str:
        return self._url("pull", number)

    def compare_url(self, base: str, head: str) -> str:
        return self._url("compare", f"{base}...{head}")

    def file_url(self, ref: str, path: str) -> str:
        return self._url("blob", ref, path)

    def run_url(self) -> str:
        return self._url("actions", "runs", self.run_id()) if self.run_id() else ""

    def change_ref(self, number: int | str) -> str:
        return f"#{number}"

    def change_in_subject(self, subject: str) -> int | None:
        found = CHANGE_IN_SUBJECT.search(subject)
        return int(found.group(1) or found.group(2)) if found else None

    def clone_url(self) -> str:
        return f"{self.api.server}/{'/'.join(self.api.slug())}.git"

    def default_branch(self) -> str:
        return str(self._call("GET").get("default_branch") or "")

    def owner_steps(self, base: str, release: str) -> str:
        return msg("github_owner_steps", base, release)

    def ci_files(self) -> list[str]:
        return [".github/workflows/*.yml"]

    def in_ci(self) -> bool:
        return os.environ.get("GITHUB_ACTIONS") == "true"

    def run_id(self) -> str:
        return os.environ.get("GITHUB_RUN_ID", "")

    def run_attempt(self) -> str:
        return os.environ.get("GITHUB_RUN_ATTEMPT", "")

    def workspace(self) -> str:
        return os.environ.get("GITHUB_WORKSPACE", "")

    def annotation(self, level: str, text: str) -> str:
        if not self.in_ci():
            return text
        return f"::{level}::" + command_data(text)

    def group(self, title: str) -> str | None:
        return f"::group::{title}" if self.in_ci() else None

    def end_group(self) -> str | None:
        return "::endgroup::" if self.in_ci() else None

    def _append(self, variable: str, text: str, required: bool = True) -> None:
        target = os.environ.get(variable)
        if not target:
            if required:
                raise forge.ForgeUnavailable(msg("forge_no_ci"))
            return
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")

    def step_summary(self, text: str) -> None:
        self._append("GITHUB_STEP_SUMMARY", text, required=False)

    def export_env(self, name: str, value: str) -> None:
        self._append("GITHUB_ENV", f"{name}={value}")

    def add_path(self, folder: str) -> None:
        self._append("GITHUB_PATH", folder, required=False)

    def set_output(self, name: str, value: str) -> None:
        import secrets

        delimiter = f"harness_{secrets.token_hex(16)}"
        self._append("GITHUB_OUTPUT", f"{name}<<{delimiter}\n{value}\n{delimiter}")

    def mask(self, value: str) -> None:
        if not self.in_ci():
            return
        for line in value.splitlines():
            if line.strip():
                print(f"::add-mask::{command_data(line)}", file=sys.stderr)

    def run_jobs(self, run: str = "") -> dict[str, str]:
        if run:
            jobs = self._items("actions", "runs", run, "jobs", key="jobs")
        elif not self.run_id():
            raise forge.ForgeUnavailable(msg("forge_no_ci"))
        else:
            jobs = self._items("actions", "runs", self.run_id(), "attempts", self.run_attempt() or "1", "jobs", key="jobs")
        return {job.get("name") or "": CONCLUSIONS.get(job.get("conclusion") or "", "") for job in jobs}

    def run_of(self, url: str) -> str:
        found = re.search(r"/actions/runs/(\d+)", url or "")
        return found.group(1) if found else ""

    def run_path(self, run: str) -> str:
        return str(self._call("GET", "actions", "runs", run).get("path") or "")

    def run_finished(self, run: str) -> bool:
        try:
            return self._call("GET", "actions", "runs", run).get("status") == "completed"
        except forge.ForgeError as error:
            if error.status == 404:
                return True
            raise

    def checks(self, sha: str) -> list[forge.Check]:
        found = []
        for run in sorted(self._items("commits", sha, "check-runs", key="check_runs",
                                      token=os.environ.get("GITHUB_CHECKS_TOKEN")), key=lambda item: item["id"]):
            app = (run.get("app") or {}).get("slug") or ""
            native = app == "github-actions"
            found.append(forge.Check(run.get("name") or "", CONCLUSIONS.get(run.get("conclusion") or "", "pending"),
                                     run.get("html_url") or "", "github-actions[bot]" if native else app,
                                     "job" if native else "check"))
        found.extend(dataclasses.replace(status, state="failure" if status.state == "error" else status.state or "pending")
                     for status in self.commit_results(sha).values())
        return found

    def job_log(self, job: str) -> str:
        found = JOB.search(job.strip())
        if not found:
            raise forge.ForgeError(msg("github_job_unknown", job))
        log = self.api.gh("api", self.api.path("actions", "jobs", found.group(1) or found.group(2), "logs"), read_only=True)
        return "\n".join(log.splitlines()[-forge.LOG_LINES:])

    def event(self) -> forge.Event | None:
        path, read = os.environ.get("GITHUB_EVENT_PATH", ""), EVENTS.get(os.environ.get("GITHUB_EVENT_NAME", ""))
        if not self.in_ci() or not path or read is None:
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as error:
            raise forge.ForgeError(msg("github_event_unread", path, error)) from None
        return read(payload)

    def change(self, number: int) -> forge.Change | None:
        try:
            return as_change(self._call("GET", "pulls", str(number)))
        except forge.ForgeError as error:
            if error.status == 404:
                return None
            raise

    def historical_heads(self, pull: forge.Change, before: int) -> list[str]:
        if pull.cross_repo:
            return []
        heads = {forge.observed_head(item.get("commit_id"), item.get("submitted_at"), before)
                 for item in self._items("pulls", str(pull.number), "reviews")}
        params = {"branch": pull.head, "created": "<" + datetime.fromtimestamp(before, timezone.utc).strftime(rest.STAMP)}
        for run in self._items("actions", "runs", key="workflow_runs", params=params):
            if (run.get("head_branch") == pull.head
                    and same_repo((run.get("repository") or {}).get("full_name", ""), self.repo())
                    and same_repo((run.get("head_repository") or {}).get("full_name", ""), self.repo())):
                heads.add(forge.observed_head(run.get("head_sha"), run.get("created_at"), before))
        return sorted(heads - {""})

    def review_input_change(self, run: str) -> forge.Change | None:
        if not run.isdecimal() or os.environ.get("GITHUB_EVENT_NAME") != "workflow_run":
            raise forge.ForgeError(msg("event_input_run_untrusted"))
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
            event = json.load(handle)
        source = self._call("GET", "actions", "runs", run)
        repository = source.get("repository") or {}
        if (event.get("action") != "completed" or str((event.get("workflow_run") or {}).get("id")) != run
                or str(source.get("id")) != run or not repository.get("id") or repository.get("full_name") != self.repo()
                or source.get("path") != ".github/workflows/review-inputs.yml"
                or source.get("event") not in ("pull_request_review", "pull_request_review_comment")
                or source.get("status") != "completed" or source.get("conclusion") != "success"):
            raise forge.ForgeError(msg("event_input_run_untrusted"))
        workflow = self._call("GET", "actions", "workflows", str(source.get("workflow_id") or ""))
        if workflow.get("path") != ".github/workflows/review-inputs.yml":
            raise forge.ForgeError(msg("event_input_run_untrusted"))
        pulls = source.get("pull_requests") or []
        event_pulls = (event.get("workflow_run") or {}).get("pull_requests") or []
        if len(pulls) != 1 or len(event_pulls) != 1:
            raise forge.ForgeError(msg("event_input_run_untrusted"))
        associated = pulls[0]
        if (not isinstance(associated.get("number"), int) or associated["number"] <= 0
                or event_pulls[0].get("number") != associated["number"]
                or ((associated.get("base") or {}).get("repo") or {}).get("id") != repository.get("id")):
            raise forge.ForgeError(msg("event_input_run_untrusted"))
        pull = self._call("GET", "pulls", str(associated.get("number") or ""))
        change = as_change(pull)
        snapshot = (source.get("head_sha"), (associated.get("head") or {}).get("sha"),
                    (associated.get("base") or {}).get("sha"))
        if (change.number != associated.get("number")
                or not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) for sha in snapshot)):
            raise forge.ForgeError(msg("event_input_run_untrusted"))
        if (change.state != "open" or (associated.get("head") or {}).get("sha") != change.head_sha
                or (associated.get("base") or {}).get("sha") != change.base_sha
                or source.get("head_sha") not in (change.head_sha, pull.get("merge_commit_sha"))):
            print(msg("event_input_run_stale", run, change.number))
            return None
        return change

    def publisher(self, number: int, head: str, base: str) -> bool:
        why = self.publisher_refusal(number, head, base)
        if why:
            print(msg("github_publisher_refused", why), file=sys.stderr)
        return not why

    def publisher_refusal(self, number: int, head: str, base: str) -> str:
        event_name = os.environ.get("GITHUB_EVENT_NAME", "")
        if event_name not in ("pull_request_target", "issue_comment", "workflow_run") or not self.run_id().isdecimal():
            return msg("github_publisher_event", event_name)
        try:
            run = self._call("GET", "actions", "runs", self.run_id())
            with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
                event = json.load(handle)
            if (str(run.get("id")) != self.run_id() or run.get("event") != event_name
                    or (run.get("repository") or {}).get("full_name") != self.repo()
                    or str(run.get("run_attempt")) != self.run_attempt()
                    or run.get("path") != get("processes.review.workflow", "", file="methodology.json")):
                return msg("github_publisher_run", self.run_id())
            if event_name == "pull_request_target":
                pull = event.get("pull_request") or {}
                stamp = os.environ.get("GITHUB_WORKFLOW_SHA", "")
                default = self.default_branch()
                workflow = get("processes.review.workflow", "", file="methodology.json")
                checkout = subprocess.run(["git", "-C", os.path.join(os.path.dirname(__file__), "..", ".."),
                                           "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=10)
                if not (event.get("action") in ("labeled", "edited")
                        and bool(head and base)
                        and pull.get("number") == number and (pull.get("head") or {}).get("sha") == head
                        and (pull.get("base") or {}).get("sha") == base and run.get("head_sha") in (head, base)
                        and re.fullmatch(r"[0-9a-f]{40}", stamp) and os.environ.get("GITHUB_SHA") == stamp
                        and default and os.environ.get("GITHUB_WORKFLOW_REF") ==
                        f"{self.repo()}/{workflow}@refs/heads/{default}"
                        and checkout.returncode == 0 and checkout.stdout.strip() == base):
                    return msg("github_publisher_target", number)
                if (pull.get("base") or {}).get("ref") == default:
                    return "" if stamp == base else msg("github_publisher_stamp", stamp[:7], base[:7])
                current = self.branch_head(default)
                if not re.fullmatch(r"[0-9a-f]{40}", current):
                    return msg("github_publisher_default_unread", default)
                if current == stamp:
                    return ""
                compared = self._call("GET", "compare", f"{stamp}...{current}?per_page=1")
                if (compared.get("status") in ("ahead", "identical")
                        and (compared.get("merge_base_commit") or {}).get("sha") == stamp):
                    return ""
                return msg("github_publisher_behind", stamp[:7], default)
            if run.get("head_branch") != self.default_branch():
                return msg("github_publisher_branch", run.get("head_branch") or "")
            if event_name == "issue_comment":
                issue = event.get("issue") or {}
                if (event.get("action") in ("created", "edited", "deleted")
                        and issue.get("pull_request") and issue.get("number") == number):
                    return ""
                return msg("github_publisher_comment", number)
            source = str((event.get("workflow_run") or {}).get("id") or "")
            change = self.review_input_change(source)
            if change is not None and change.number == number and change.head_sha == head and change.base_sha == base:
                return ""
            return msg("github_publisher_input", source)
        except (KeyError, OSError, ValueError, subprocess.SubprocessError, forge.ForgeError) as error:
            return msg("github_publisher_unchecked", error)

    def change_files(self, number: int) -> list[str] | None:
        try:
            change = self.change(number)
            if change is None:
                return None
            files = [item.get("filename") or "" for item in self._items("pulls", str(number), "files")]
        except forge.ForgeError:
            return None
        return files if change.files == len(files) else None

    def open_change(self, head: str, base: str, title: str, body: str, draft: bool = True,
                    labels: Sequence[str] = ()) -> forge.Change:
        change = as_change(self._call("POST", "pulls", payload={"head": head, "base": base, "title": title,
                                                                "body": body, "draft": draft}))
        if not labels:
            return change
        added = self._call("POST", "issues", str(change.number), "labels", payload={"labels": list(labels)})
        return dataclasses.replace(change, labels=names(added))

    def edit_change(self, number: int, body: str) -> None:
        self._call("PATCH", "pulls", str(number), payload={"body": body})

    def mark_ready(self, number: int) -> None:
        pull = self._call("GET", "pulls", str(number))
        if pull.get("draft"):
            self.api.gh_graphql_or(lambda: self.api.gh_graphql(READY, id=pull["node_id"]),
                                   lambda: self._call("POST", "pulls", str(number), CCR, "ready_for_review"))

    def close_change(self, number: int) -> None:
        self._call("PATCH", "pulls", str(number), payload={"state": "closed"})

    def add_label(self, number: int, name: str) -> None:
        self._call("POST", "issues", str(number), "labels", payload={"labels": [name]})

    def remove_label(self, number: int, name: str) -> None:
        from urllib.parse import quote

        try:
            self._call("DELETE", "issues", str(number), "labels", quote(name, safe=""))
        except forge.ForgeError as error:
            if error.status != 404:
                raise

    def notes(self, number: int) -> list[forge.Note]:
        return [forge.Note(id=str(item.get("id") or ""), created=item.get("created_at") or "",
                           bot=(item.get("user") or {}).get("type") == "Bot", **said(item))
                for item in self._items("issues", str(number), "comments")]

    def task_notes(self, number: int) -> list[forge.Note]:
        return self.notes(number) + [forge.Note(
            id="review:" + str(item.get("id") or ""), created=item.get("created_at") or "",
            bot=(item.get("user") or {}).get("type") == "Bot", path=item.get("path") or "",
            line=item.get("line") or item.get("original_line"), **said(item))
            for item in self._items("pulls", str(number), "comments")] + [forge.Note(
            id="summary:" + str(item.get("id") or ""), created=item.get("submitted_at") or "",
            bot=(item.get("user") or {}).get("type") == "Bot", **said(item))
            for item in self._items("pulls", str(number), "reviews")]

    def reviews(self, number: int) -> list[forge.Review]:
        return [forge.Review(created=item.get("submitted_at") or "", state=REVIEW_STATES[item.get("state")],
                             sha=item.get("commit_id") or "", author=(item.get("user") or {}).get("login") or "",
                             body=item.get("body") or "")
                for item in self._items("pulls", str(number), "reviews") if item.get("state") in REVIEW_STATES]

    def review_threads(self, number: int) -> list[forge.Thread] | None:
        return self.api.gh_graphql_or(lambda: self._graphql_threads(number), lambda: self._ccr_threads(number))

    def _ccr_threads(self, number: int) -> list[forge.Thread]:
        first = {str(item.get("id")): item for item in self._items("pulls", str(number), "comments")}
        found = []
        for node in self._items("pulls", str(number), CCR, "review_threads"):
            ids = [str(item) for item in node.get("comment_ids") or []]
            comment = first.get(ids[0], {}) if ids else {}
            user = comment.get("user") or {}
            found.append(forge.Thread(
                id=f"{CCR}:{number}:{ids[0] if ids else ''}", path=node.get("path") or "", line=node.get("line"),
                resolved=bool(node.get("resolved")), outdated=bool(node.get("outdated")),
                author=rest_login({"__typename": user.get("type") or "", "login": user.get("login") or ""}),
                body=comment.get("body") or "", url=comment.get("html_url") or ""))
        return found

    def _graphql_threads(self, number: int) -> list[forge.Thread] | None:
        owner, name = self.api.slug()
        found: list[forge.Thread] = []
        after = None
        while True:
            data = self.api.gh_graphql(THREADS, read_only=True, owner=owner, name=name, number=number, after=after)
            pull = (data.get("repository") or {}).get("pullRequest")
            if pull is None:
                return None
            page = pull["reviewThreads"]
            found += [as_thread(node) for node in page["nodes"]]
            if not page["pageInfo"]["hasNextPage"]:
                return found
            after = page["pageInfo"]["endCursor"]

    def reply_thread(self, thread: str, body: str) -> str:
        found = ccr_comment(thread)
        if found:
            return self._call("POST", "pulls", found[0], "comments", found[1], "replies",
                              payload={"body": body}).get("html_url") or ""
        return self.api.gh_graphql(REPLY, thread=thread, body=body)["addPullRequestReviewThreadReply"]["comment"]["url"]

    def resolve_thread(self, thread: str) -> str:
        found = ccr_comment(thread)
        if found:
            self._call("POST", "pulls", found[0], CCR, "comments", found[1], "resolve")
            return thread
        return self.api.gh_graphql(RESOLVE, thread=thread)["resolveReviewThread"]["thread"]["id"]

    def closing_references(self, numbers: set[int], repo: str) -> set[str]:
        if not numbers:
            return set()
        owner, name = self.api.slug()
        fields = " ".join(
            f"p{number}: issueOrPullRequest(number: {number}) {{ ... on PullRequest {{"
            f" closingIssuesReferences(first: 20) {{ nodes {{ number repository {{ nameWithOwner }} }} }} }} }}"
            for number in sorted(numbers)
        )
        query = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"

        def linked() -> set[str]:
            pulls = self.api.gh_graphql(query, read_only=True, owner=owner, name=name).get("repository") or {}
            return {str(node["number"]) for pull in pulls.values() if pull
                    for node in pull.get("closingIssuesReferences", {}).get("nodes", [])
                    if node and in_repo(node, repo)}

        def from_body() -> set[str]:
            from markdown_text import without_code

            print(msg("github_closing_from_body"), file=sys.stderr)
            return {reference.number for number in sorted(numbers)
                    for reference in tracker.references(without_code(self.change_body(number))) if reference.closing}

        return self.api.gh_graphql_or(linked, from_body)

    def change_body(self, number: int) -> str:
        return self.api.gh("api", self.api.path("pulls", str(number)), "--jq", '.body // ""', read_only=True).rstrip("\n")

    def commit_results(self, sha: str) -> dict[str, forge.Check]:
        found = {}
        for status in self._items("commits", sha, "statuses", token=os.environ.get("GITHUB_CHECKS_TOKEN")):
            name = status.get("context") or ""
            if name not in found:
                found[name] = forge.Check(name, status.get("state") or "", status.get("target_url") or "",
                                          (status.get("creator") or {}).get("login") or "", "status",
                                          status.get("description") or "")
        return found

    def set_commit_state(self, sha: str, context: str, state: str, description: str, url: str = "") -> None:
        payload = {"state": state, "context": context, "description": description[:MAX_DESCRIPTION]}
        if url:
            payload["target_url"] = url
        self._call("POST", "statuses", sha, payload=payload)

    def submit_review(self, number: int, sha: str, decision: str, body: str,
                      comments: Sequence[forge.InlineComment] = (), rejected=None) -> None:
        payload = {"commit_id": sha, "event": DECISIONS[decision], "body": body}
        inline = [{"path": item.path, "line": item.line, "side": item.side,
                   "body": item.body + ("\n\n" + item.key if item.key else "")} for item in comments]
        target = ("pulls", str(number), "reviews")
        try:
            self._call("POST", *target, payload={**payload, "comments": inline} if inline else payload)
        except forge.ForgeError as error:
            if error.status != 422 or not inline:
                raise
            text = rejected(comments) if rejected else forge.inline_fallback(body, comments)
            self._call("POST", *target, payload={**payload, "body": text})

    def login(self) -> str:
        return self.api.login(gh=True)

    def _own_comments(self, number: int, marker: str) -> list[str]:
        rows = [row.split(" ", 1) for row in self.api.gh(
            "api", self.api.path("issues", str(number), "comments"), "--paginate", "--jq",
            f'.[] | select(.body | contains({json.dumps(marker)})) | "\\(.id) \\(.user.login)"', read_only=True).splitlines()]
        login = self.login() if rows else ""
        return [row[0] for row in rows if row[1:] == [login]]

    def comment(self, number: int, body: str, marker: str = "", last: bool = False) -> None:
        found = self._own_comments(number, marker) if marker else []
        payload = {"body": body}
        created = None
        if last or not found:
            answer = self._call("POST", "issues", str(number), "comments", payload=payload)
            created = str(answer.get("id") or "")
            if not marker:
                return
            self.api.account = (answer.get("user") or {}).get("login") or self.api.account
            if last:
                self._drop(found)
                return
            found = self._own_comments(number, marker)
        if found and found[0] != created:
            self._call("PATCH", "issues", "comments", found[0], payload=payload)
        self._drop(found[1:])

    def _drop(self, comments: list[str]) -> None:
        for extra in comments:
            try:
                self._call("DELETE", "issues", "comments", extra)
            except forge.ForgeError as error:
                if error.status != 404:
                    raise

    def branch_head(self, branch: str) -> str:
        body = self.api.rest("GET", self.api.path("git", "ref", "heads", branch))
        return str((body.get("object") or {}).get("sha") or "")

    def open_changes(self, branch: str) -> list[int]:
        owner, _ = self.api.slug()
        numbers: set[int] = set()
        for field, value in (("head", f"{owner}:{branch}"), ("base", branch)):
            query = urllib.parse.urlencode({field: value, "state": "open"})
            body = self.api.rest("GET", self.api.path("pulls") + "?" + query)
            if not isinstance(body, list):
                raise forge.ForgeError(rest.detail(body))
            numbers |= {int(item["number"]) for item in body if item.get("number")}
        return sorted(numbers)

    def merged_changes(self, branch: str) -> list[int]:
        owner, _ = self.api.slug()
        return sorted(int(item["number"]) for item in self._items("pulls", params={"head": f"{owner}:{branch}", "state": "closed"})
                      if item.get("merged_at"))

    def create_branch(self, branch: str, sha: str) -> None:
        self.api.rest("POST", self.api.path("git", "refs"), {"ref": f"refs/heads/{branch}", "sha": sha}, ok=(200, 201))

    def delete_branch(self, branch: str) -> None:
        self.api.rest("DELETE", self.api.path("git", "refs", "heads", branch), ok=(200, 204))

    def create_tag(self, tag: str, sha: str) -> None:
        self._call("POST", "git", "refs", payload={"ref": f"refs/tags/{tag}", "sha": sha})

    def labels(self) -> set[str]:
        return set(self.api.gh("api", self.api.path("labels"), "--paginate", "--jq", ".[].name",
                               read_only=True).splitlines())

    def create_label(self, name: str, description: str) -> None:
        self.api.gh("label", "create", name, "--repo", "/".join(self.api.slug()), "--description", description)

    def secret_names(self, environment: str = "") -> set[str]:
        scope = ("--env", environment) if environment else ()
        return set(self.api.gh("secret", "list", "--repo", "/".join(self.api.slug()), *scope,
                               "--json", "name", "--jq", ".[].name", read_only=True).splitlines())


class GitHubIssues(tracker.Tracker):
    name = "github-issues"
    key = r"\d+"

    def __init__(self, settings: dict):
        self.settings = settings
        self.api = Api(self.repo(), tracker.TrackerError, tracker.TrackerUnavailable, self._server())
        self.code_repository = ""

    def missing(self) -> str:
        return self.api.missing() or ("" if self.api.token() else msg("forge_no_token", TOKEN))

    def repo(self) -> str:
        return rest.literal(self.settings, "repo")

    def _code(self) -> GitHubForge | None:
        code = forge.active()
        return code if isinstance(code, GitHubForge) else None

    def _server(self) -> str:
        code = self._code()
        return code._server() if code else SERVER

    def _code_repo(self) -> str:
        code = self._code()
        return code.repo() if code else ""

    def issue_url(self, number: int | str) -> str:
        return f"{self.api.server}/{self.repo()}/issues/{number}" if self.repo() else ""

    def issue_ref(self, number: int | str) -> str:
        return f"#{number}" if same_repo(self._code_repo(), self.repo()) else f"{self.repo()}#{number}"

    def references(self, text: str) -> list[tracker.Reference]:
        code, own = self._code_repo(), self.repo()
        return [tracker.Reference(found.group(3), found.group(1).lower() != "refs", found.group(0)) for found in REFERENCE.finditer(text)
                if same_repo(found.group(2) or code, own)]

    def sample_key(self) -> str:
        return "1"

    def closing_issues(self, changes: set[int]) -> set[str]:
        code = self._code()
        return code.closing_references(changes, self.repo()) if code else super().closing_issues(changes)

    def issue_branches(self, number: str) -> tracker.IssueBranches | None:
        owner, name = self.api.slug()
        issue = (self.api.graphql(ISSUE_BRANCHES, {"owner": owner, "name": name, "number": int(number)}, read_only=True)
                 .get("repository") or {}).get("issue")
        if not issue:
            return None
        linked, code = {}, self._code_repo()
        for node in (issue.get("linkedBranches") or {}).get("nodes") or []:
            ref = node.get("ref") or {}
            if ref.get("name") and in_repo(ref, code):
                linked[ref["name"]] = node.get("id") or ""
        return tracker.IssueBranches(issue.get("id") or "", linked)

    def _code_repository(self) -> str:
        if not self.code_repository:
            owner, name = Api(self._code_repo(), tracker.TrackerError, tracker.TrackerUnavailable).slug()
            found = (self.api.graphql(REPOSITORY, {"owner": owner, "name": name}, read_only=True).get("repository") or {}).get("id")
            if not found:
                raise tracker.TrackerError(msg("github_repo_unknown", f"{owner}/{name}"))
            self.code_repository = found
        return self.code_repository

    def link_branch(self, issue: str, sha: str, name: str) -> tuple[str, str]:
        variables = {"issueId": issue, "oid": sha, "name": name, "repositoryId": self._code_repository()}
        created = (self.api.graphql(LINK, variables).get("createLinkedBranch") or {}).get("linkedBranch") or {}
        return (created.get("ref") or {}).get("name") or "", created.get("id") or ""

    def unlink_branch(self, link: str) -> None:
        self.api.graphql(UNLINK, {"linkedBranchId": link})

    def _issue(self, key: str, *parts: str) -> str:
        if not re.fullmatch(self.key, key):
            raise tracker.TrackerError(msg("github_issue_key", key))
        return self.api.path("issues", key, *parts)

    def issue(self, key: str) -> tracker.Issue | None:
        try:
            item = self.api.rest("GET", self._issue(key))
        except tracker.TrackerError as error:
            if error.status in (404, 410):
                return None
            raise
        return None if "pull_request" in item else as_issue(item)

    def find_issues(self, label: str, open_only: bool = True, closed_pages: int | None = None) -> list[tracker.Issue]:
        query = {"labels": label, "sort": "created", "direction": "desc"}
        if open_only or closed_pages is None:
            items = self.api.listing(self.api.path("issues"), **query, state="open" if open_only else "all")
        else:
            items = self.api.listing(self.api.path("issues"), **query, state="open") + [
                item for page in rest.each_page(lambda url: self.api.rest("GET", url), self.api.path("issues") + "?"
                                                + urllib.parse.urlencode({**query, "state": "closed"}),
                                                tracker.TrackerError, closed_pages) for item in page]
        return [as_issue(item) for item in items if "pull_request" not in item]

    def search_issues(self, label: str, text: str) -> tuple[list[tracker.Issue], bool]:
        query = f"repo:{self.repo()} is:issue label:{label} in:body {json.dumps(text)}"
        found = self.api.rest("GET", "search/issues?" + urllib.parse.urlencode({"q": query, "per_page": rest.PAGE}))
        items = found.get("items") or []
        complete = not found.get("incomplete_results") and int(found.get("total_count") or 0) <= rest.PAGE
        return [as_issue(item) for item in items if "pull_request" not in item], complete

    def create_issue(self, title: str, body: str, labels: Sequence[str]) -> tracker.Issue:
        payload = {"title": title, "body": body, "labels": list(labels)}
        return as_issue(self.api.rest("POST", self.api.path("issues"), payload, ok=(201,)))

    def update_issue(self, key: str, body: str, reopen: bool = False) -> None:
        self.api.rest("PATCH", self._issue(key), {"state": "open", "body": body} if reopen else {"body": body})

    def _close(self, key: str) -> None:
        self.api.rest("PATCH", self._issue(key), {"state": "closed", "state_reason": "completed"})

    def closes_natively(self, base: str) -> bool:
        code = self._code()
        return code is not None and same_repo(code.repo(), self.repo()) and code.default_branch() == base

    def comment_issue(self, key: str, body: str) -> None:
        self.api.rest("POST", self._issue(key, "comments"), {"body": body}, ok=(201,))

    def assign(self, key: str) -> None:
        self.api.rest("POST", self._issue(key, "assignees"), {"assignees": [self.login()]}, ok=(201,))

    def issue_notes(self, key: str, since: str = "") -> list[tracker.IssueNote]:
        return [tracker.IssueNote(id=str(item.get("id") or ""), author=(item.get("user") or {}).get("login") or "",
                                  created=item.get("created_at") or "", body=item.get("body") or "",
                                  bot=(item.get("user") or {}).get("type") == "Bot", updated=item.get("updated_at") or "")
                for item in self.api.listing(self._issue(key, "comments"), **({"since": since} if since else {}))]

    def login(self) -> str:
        return self.api.login(gh=False)
