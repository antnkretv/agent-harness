from __future__ import annotations

import base64
import re
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timezone

import rest
import tracker
from manifest import secrets, setting
from platform_messages import msg

EXTENSION = "tracker/jira"
FIELDS = "summary,description,labels,created,status,assignee"
CLOUD_SEARCH = "search/jql"
KEY = r"[A-Z][A-Z0-9_]+-\d+"
KEYWORDS = r"clos(?:e|es|ed|ing)|fix(?:|es|ed|ing)|resolv(?:e|es|ed|ing)|implement(?:s|ed|ing)?"
REFERENCE = re.compile(rf"(?:\b((?i:{KEYWORDS}))\s+|\b)({KEY})\b")


def configured(key: str, default: str = "") -> str:
    value = setting(EXTENSION, key, default)
    return value if isinstance(value, str) else default


def quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def issue_path(key: str, *parts: str) -> str:
    if re.fullmatch(KEY, str(key)) is None:
        raise tracker.TrackerError(msg("jira_bad_key", key))
    return "/".join(["issue", urllib.parse.quote(key, safe=""), *parts])


def transition_names(transitions: list[dict]) -> str:
    return ", ".join(str(item.get("name") or item.get("id")) for item in transitions) or msg("jira_value_empty")


def author(person: dict) -> str:
    return str(person.get("accountId") or person.get("name") or person.get("emailAddress")
               or person.get("displayName") or "")


class JiraTracker(tracker.Tracker):
    name = "jira"
    html_comments = False
    key = KEY

    def __init__(self, settings: dict):
        self.settings = settings
        self.cloud = (urllib.parse.urlsplit(self.site()).hostname or "").lower().endswith(".atlassian.net")
        self.search = CLOUD_SEARCH if self.cloud else "search"
        self.account = ""

    def site(self) -> str:
        return rest.literal(self.settings, "url").rstrip("/")

    def projects(self) -> list[str]:
        value = self.settings.get("projects")
        return [str(item) for item in (value if isinstance(value, list) else [value] if value else [])]

    def missing(self) -> str:
        if not re.fullmatch(r"https?://\S+", self.site()):
            return msg("jira_bad_url", self.site() or msg("jira_value_empty"))
        if not self.projects():
            return msg("jira_no_project")
        if self.cloud and not configured("user"):
            return msg("jira_cloud_user")
        return "" if configured("token") else msg("jira_no_token", (secrets(EXTENSION) or ["JIRA_TOKEN"])[0])

    def repo(self) -> str:
        return self.site()

    def _own(self, key: str) -> bool:
        return key.split("-", 1)[0] in self.projects()

    def issue_url(self, number: int | str) -> str:
        return f"{self.site()}/browse/{number}" if self.site() else ""

    def references(self, text: str) -> list[tracker.Reference]:
        return [tracker.Reference(found.group(2), bool(found.group(1)), found.group(0)) for found in REFERENCE.finditer(text)
                if self._own(found.group(2))]

    def sample_key(self) -> str:
        return f"{(self.projects() or ['PROJ'])[0]}-1"

    def branch_issue(self, branch: str) -> str | None:
        found = super().branch_issue(branch)
        return found if found and self._own(found) else None

    def issue_branches(self, number: str) -> tracker.IssueBranches | None:
        raise tracker.TrackerUnavailable(msg("jira_branch_link"))

    def link_branch(self, issue: str, sha: str, name: str) -> tuple[str, str]:
        raise tracker.TrackerUnavailable(msg("jira_branch_link"))

    def unlink_branch(self, link: str) -> None:
        raise tracker.TrackerUnavailable(msg("jira_branch_link"))

    def _auth(self) -> str:
        token, user = configured("token"), configured("user")
        if not user:
            return f"Bearer {token}"
        return "Basic " + base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")

    def _request(self, method: str, path: str, query: dict | None = None, payload=None, ok: tuple[int, ...] = (200,)):
        if self.missing():
            raise tracker.TrackerUnavailable(self.missing())
        url = f"{self.site()}/rest/api/2/{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Accept": "application/json", "Authorization": self._auth(), "User-Agent": "harness-jira-adapter"}
        return rest.request(method, url, headers, tracker.TrackerError, payload, ok)

    def _offset_pages(self, path: str, query: dict, field: str) -> list[dict]:
        found: list[dict] = []
        while True:
            page = self._request("GET", path, {**query, "startAt": len(found), "maxResults": rest.PAGE})
            items = page.get(field) or []
            found += items
            if not items or len(found) >= int(page.get("total") or 0):
                return found

    def _token_pages(self, query: dict) -> list[dict]:
        found: list[dict] = []
        token = ""
        while True:
            paged = {**query, "maxResults": rest.PAGE, **({"nextPageToken": token} if token else {})}
            page = self._request("GET", CLOUD_SEARCH, paged)
            found += page.get("issues") or []
            token = page.get("nextPageToken") or ""
            if not token or page.get("isLast"):
                return found

    def _search(self, jql: str) -> list[dict]:
        query = {"jql": jql, "fields": FIELDS}
        try:
            return self._found(query)
        except tracker.TrackerError as error:
            if error.status not in (404, 410):
                raise
            self.search = "search" if self.search == CLOUD_SEARCH else CLOUD_SEARCH
            return self._found(query)

    def _found(self, query: dict) -> list[dict]:
        if self.search == CLOUD_SEARCH:
            return self._token_pages(query)
        return self._offset_pages("search", query, "issues")

    def _as_issue(self, data: dict) -> tracker.Issue:
        fields = data.get("fields") or {}
        category = ((fields.get("status") or {}).get("statusCategory") or {}).get("key")
        key = str(data.get("key") or "")
        return tracker.Issue(key=key, title=fields.get("summary") or "", body=fields.get("description") or "",
                             url=self.issue_url(key), labels=tuple(map(str, fields.get("labels") or ())),
                             created=rest.utc(fields.get("created") or ""), open=category != "done",
                             assignees=tuple(filter(None, (author(fields.get("assignee") or {}),))))

    def issue(self, key: str) -> tracker.Issue | None:
        try:
            return self._as_issue(self._request("GET", issue_path(key), {"fields": FIELDS}))
        except tracker.TrackerError as error:
            if error.status == 404:
                return None
            raise

    def _close(self, key: str) -> None:
        transitions = self._request("GET", issue_path(key, "transitions")).get("transitions") or []
        named = rest.literal(self.settings, "doneTransition")
        chosen = [item for item in transitions if (item.get("name") == named if named else
                  ((item.get("to") or {}).get("statusCategory") or {}).get("key") == "done")]
        if not chosen and named:
            raise tracker.TrackerError(msg("jira_transition_missing", key, named, transition_names(transitions)))
        if not chosen:
            raise tracker.TrackerError(msg("jira_no_done_transition", key, transition_names(transitions)))
        if len(chosen) > 1:
            raise tracker.TrackerError(msg("jira_done_ambiguous", key, transition_names(chosen)))
        self._request("POST", issue_path(key, "transitions"), payload={"transition": {"id": str(chosen[0].get("id"))}},
                      ok=(200, 204))

    def find_issues(self, label: str, open_only: bool = True, closed_pages: int | None = None) -> list[tracker.Issue]:
        clauses = [f"project in ({', '.join(map(quoted, self.projects()))})", f"labels = {quoted(label)}"]
        if open_only:
            clauses.append("statusCategory != Done")
        return [self._as_issue(item) for item in self._search(" AND ".join(clauses) + " ORDER BY created DESC")]

    def create_issue(self, title: str, body: str, labels: Sequence[str]) -> tracker.Issue:
        fields = {"project": {"key": self.projects()[0]}, "summary": title, "description": body,
                  "issuetype": {"name": configured("issueType", "Task") or "Task"}, "labels": list(labels)}
        created = self._request("POST", "issue", payload={"fields": fields}, ok=(200, 201))
        key = str(created.get("key") or "")
        if not key:
            raise tracker.TrackerError(rest.detail(created))
        return tracker.Issue(key=key, title=title, body=body, url=self.issue_url(key), labels=tuple(labels),
                             created=datetime.now(timezone.utc).strftime(rest.STAMP), open=True)

    def update_issue(self, key: str, body: str, reopen: bool = False) -> None:
        if reopen:
            raise tracker.TrackerUnavailable(msg("jira_reopen_unsupported"))
        self._request("PUT", issue_path(key), payload={"fields": {"description": body}}, ok=(200, 204))

    def comment_issue(self, key: str, body: str) -> None:
        self._request("POST", issue_path(key, "comment"), payload={"body": body}, ok=(200, 201))

    def assign(self, key: str) -> None:
        self._request("PUT", issue_path(key, "assignee"), payload={"accountId" if self.cloud else "name": self.login()},
                      ok=(200, 204))

    def issue_notes(self, key: str, since: str = "") -> list[tracker.IssueNote]:
        items = self._offset_pages(issue_path(key, "comment"), {"orderBy": "created"}, "comments")
        return [tracker.IssueNote(id=str(item.get("id") or ""), author=author(item.get("author") or {}),
                                  created=rest.utc(item.get("created") or ""), body=item.get("body") or "",
                                  bot=(item.get("author") or {}).get("accountType") == "app",
                                  system=bool(item.get("system")))
                for item in items]

    def login(self) -> str:
        if not self.account:
            self.account = author(self._request("GET", "myself"))
            if not self.account:
                raise tracker.TrackerError(msg("jira_login_unknown"))
        return self.account
