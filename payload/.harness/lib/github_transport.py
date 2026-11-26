from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import time
import rest
from platform_messages import msg

SERVER = "https://github.com"
TOKEN = "GH_TOKEN"
REPO = re.compile(r"[\w.-]+/[\w.-]+")

HTTP = re.compile(r"HTTP (\d{3})")

TIMEOUT = 60
READ_ATTEMPTS = 3
TRANSIENT_ERRORS = ("tls handshake timeout", "timed out", "i/o timeout", "connection reset",
                    "unexpected eof", "eof occurred", "temporary failure in name resolution",
                    "context deadline exceeded")

VIEWER = "query { viewer { __typename login } }"


class Api:
    def __init__(self, repo: str, refused: type, unavailable: type, server: str = SERVER):
        from urllib.parse import urlsplit

        self.repo, self.refused, self.unavailable, self.server = repo, refused, unavailable, server.rstrip("/")
        address = urlsplit(self.server)
        public = (address.hostname or "").lower() in ("github.com", "www.github.com")
        self.host = "github.com" if public else address.netloc.lower()
        self.rest_url = os.environ.get("GITHUB_API_URL") or ("https://api.github.com" if public else f"{self.server}/api/v3")
        self.graphql_url = os.environ.get("GITHUB_GRAPHQL_URL") or (
            "https://api.github.com/graphql" if public else f"{self.server}/api/graphql")
        self.account = ""

    def missing(self) -> str:
        return "" if REPO.fullmatch(self.repo) else msg("github_bad_repo", self.repo or msg("github_value_empty"))

    def slug(self) -> tuple[str, str]:
        if self.missing():
            raise self.unavailable(self.missing())
        owner, _, name = self.repo.partition("/")
        return owner, name

    def path(self, *parts: str) -> str:
        return "/".join(["repos", *self.slug(), *parts])

    def _request(self, call, read_only: bool):
        for attempt in range(READ_ATTEMPTS if read_only else 1):
            try:
                return call()
            except self.refused as error:
                status = getattr(error, "status", None)
                transient = status in (502, 503, 504) or (status is None and (
                    str(error) == msg("github_gh_timeout", TIMEOUT)
                    or any(part in str(error).lower() for part in TRANSIENT_ERRORS)))
                if not read_only or not transient or attempt == READ_ATTEMPTS - 1:
                    raise
                print(msg("github_read_retry", attempt + 2, READ_ATTEMPTS), file=sys.stderr, flush=True)
                time.sleep(2 ** attempt)

    def gh(self, *args: str, payload: str | None = None, read_only: bool = False, token: str | None = None) -> str:
        if shutil.which("gh") is None:
            raise self.unavailable(msg("github_no_gh"))
        env = {**os.environ, "GH_HOST": self.host}
        if token:
            env.update(GH_TOKEN=token, GH_ENTERPRISE_TOKEN=token)
        return self._request(lambda: self._gh(args, payload, env), read_only)

    def _gh(self, args: tuple[str, ...], payload: str | None, env: dict[str, str]) -> str:
        try:
            done = subprocess.run(["gh", *args], input=payload, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=TIMEOUT, check=False, env=env)
        except subprocess.TimeoutExpired:
            raise self.refused(msg("github_gh_timeout", TIMEOUT)) from None
        if done.returncode != 0:
            why = done.stderr.strip() or done.stdout.strip() or msg("github_gh_failed")
            code = HTTP.search(why)
            raise self.refused(why, int(code.group(1)) if code else None)
        return done.stdout

    def gh_graphql(self, query: str, *, read_only: bool = False, **variables: object) -> dict:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        return json.loads(self.gh(*args, read_only=read_only)).get("data") or {}

    def token(self) -> str:
        return os.environ.get(TOKEN, "")

    def _headers(self) -> dict[str, str]:
        token = self.token()
        if not token:
            raise self.unavailable(msg("forge_no_token", TOKEN))
        return {"Accept": "application/vnd.github+json", "Authorization": f"bearer {token}",
                "User-Agent": "harness-github-adapter"}

    def rest(self, method: str, path: str, payload=None, ok: tuple[int, ...] = (200,)):
        return self._request(lambda: rest.request(method, f"{self.rest_url.rstrip('/')}/{path}",
                                                  self._headers(), self.refused, payload, ok), method == "GET")

    def graphql(self, query: str, variables: dict, *, read_only: bool = False) -> dict:
        payload = self._request(lambda: rest.request("POST", self.graphql_url, self._headers(), self.refused,
                                                     {"query": query, "variables": variables}), read_only)
        problems = [f"{item.get('type') or 'ERROR'}: {item.get('message') or ''}".strip()
                    for item in (payload.get("errors") or [])]
        if problems:
            raise self.refused("; ".join(problems))
        return payload.get("data") or {}

    def gh_graphql_or(self, primary, fallback):
        try:
            return primary()
        except self.refused as error:
            if not (getattr(error, "status", None) == 403 and "/ccr/" in str(error)):
                raise
        return fallback()

    def rest_viewer(self, gh: bool) -> dict:
        user = json.loads(self.gh("api", "user", read_only=True) or "{}") if gh else self.rest("GET", "user")
        return {"viewer": {"__typename": user.get("type") or "", "login": user.get("login") or ""}}

    def login(self, gh: bool) -> str:
        if not self.account:
            viewer = (lambda: self.gh_graphql(VIEWER, read_only=True)) if gh else (lambda: self.graphql(VIEWER, {}, read_only=True))
            data = self.gh_graphql_or(viewer, lambda: self.rest_viewer(gh))
            self.account = rest_login(data.get("viewer") or {})
            if not self.account:
                raise self.refused(msg("github_login_unknown"))
        return self.account

    def listing(self, path: str, **query: str) -> list:
        from urllib.parse import urlencode

        return rest.pages(lambda url: self.rest("GET", url), f"{path}?{urlencode(query)}" if query else path,
                          self.refused)


def rest_login(actor: dict) -> str:
    login = str(actor.get("login") or "")
    return login + "[bot]" if login and actor.get("__typename") == "Bot" and not login.endswith("[bot]") else login
