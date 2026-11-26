from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import secrets
import sqlite3
import time
from urllib.parse import quote, urlparse

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse, Response

from messages import msg
from policy import DIGEST, password_digest

SCOPE = "stand:read"
LOOPBACK = ("localhost",)


def static_digests(config: dict, env: dict[str, str]) -> tuple[str, ...]:
    variable = config.get("static_tokens_env")
    if not variable:
        return ()
    digests = tuple(part.strip().lower() for part in env.get(variable, "").split(",") if part.strip())
    if not all(DIGEST.fullmatch(digest) for digest in digests):
        raise ValueError(msg("stand_digest_invalid", variable))
    return digests


def loopback(host: str | None) -> bool:
    if host in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


class OwnerOAuth:
    def __init__(self, config: dict, database: str, digests: tuple[str, ...] = ()):
        self.origin = config["public_url"].rstrip("/")
        self.resource = self.origin + "/mcp"
        self.title = config.get("title", config["stand"])
        oauth = config["oauth"]
        self.password_salt = bytes.fromhex(oauth["password_salt"])
        self.password_hash = oauth["password_hash"]
        self.schemes = tuple(oauth.get("redirect_schemes", ()))
        self.digests = digests
        self.db = sqlite3.connect(database)
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, payload TEXT, expires REAL, family TEXT, PRIMARY KEY(kind,id))")
        self.failures: list[float] = []

    @staticmethod
    def key(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def save(self, kind, key, payload, lifetime, family=None):
        with self.db:
            self.db.execute("DELETE FROM records WHERE expires < ?", (time.time(),))
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)",
                            (kind, self.key(key), json.dumps(payload), time.time() + lifetime, family))

    def get(self, kind, key, consume=False):
        with self.db:
            row = self.db.execute("SELECT payload,expires FROM records WHERE kind=? AND id=?",
                                  (kind, self.key(key))).fetchone()
            if consume:
                self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, self.key(key)))
        return json.loads(row[0]) if row and row[1] > time.time() else None

    def redirect_allowed(self, uri: str) -> bool:
        parsed = urlparse(uri)
        return parsed.scheme == "https" or parsed.scheme in self.schemes \
            or (parsed.scheme == "http" and loopback(parsed.hostname))

    async def get_client(self, client_id):
        data = self.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        if not all(self.redirect_allowed(str(uri)) for uri in client_info.redirect_uris or ()):
            raise RegistrationError("invalid_redirect_uri", msg("stand_redirect_invalid"))
        count = self.db.execute("SELECT count(*) FROM records WHERE kind='client' AND family IS NULL AND expires>?", (time.time(),)).fetchone()[0]
        if count >= 256:
            raise RegistrationError("invalid_client_metadata", msg("stand_registration_limit"))
        self.save("client", client_info.client_id, client_info.model_dump(mode="json"), 3600)

    async def authorize(self, client, params):
        if params.resource not in (None, self.resource):
            raise AuthorizeError("invalid_target", msg("stand_resource_invalid"))
        if params.scopes != [SCOPE]:
            raise AuthorizeError("invalid_scope", msg("stand_scope_invalid", SCOPE))
        count = self.db.execute("SELECT count(*) FROM records WHERE kind='pending' AND expires>?", (time.time(),)).fetchone()[0]
        if count >= 256:
            raise AuthorizeError("temporarily_unavailable", msg("stand_registration_limit"))
        request_id = secrets.token_urlsafe(32)
        self.save("pending", request_id, {"client_id": client.client_id,
                  "name": client.client_name or client.client_id,
                  "params": params.model_dump(mode="json"), "csrf": secrets.token_urlsafe(32)}, 600)
        return self.origin + "/login?request=" + request_id

    @staticmethod
    def form_target(redirect_uri: str) -> str:
        callback = urlparse(redirect_uri)
        if not callback.hostname:
            return callback.scheme + ":"
        host = f"[{callback.hostname}]" if ":" in callback.hostname else callback.hostname
        return f"{callback.scheme}://{host}" + (f":{callback.port}" if callback.port else "")

    async def login(self, request):
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            data = self.get("pending", request_id)
            if not data:
                return Response(msg("stand_auth_expired"), status_code=400)
            title = html.escape(self.title)
            content = msg("stand_login_page", title, title, html.escape(data["name"]),
                          html.escape(data["params"]["redirect_uri"]), html.escape(request_id), data["csrf"])
            target = quote(self.form_target(data["params"]["redirect_uri"]), safe=":/[]")
            policy = "default-src 'none'; form-action 'self' " + target + "; frame-ancestors 'none'"
            response = HTMLResponse(content, headers={"Cache-Control": "no-store", "Content-Security-Policy": policy})
            response.set_cookie("mcp_csrf", data["csrf"], secure=True, httponly=True, samesite="lax", max_age=600)
            return response
        self.failures = [stamp for stamp in self.failures if stamp > time.time() - 300]
        if len(self.failures) >= 20:
            return Response(msg("stand_login_rate"), status_code=429)
        form = await request.form()
        request_id = str(form.get("request", ""))
        data = self.get("pending", request_id)
        csrf = str(form.get("csrf", ""))
        if (not data or request.headers.get("origin") != self.origin
                or not hmac.compare_digest(csrf, data["csrf"])
                or not hmac.compare_digest(request.cookies.get("mcp_csrf", ""), csrf)):
            return Response(msg("stand_auth_invalid"), status_code=400)
        password = str(form.get("password", ""))
        digest = await asyncio.to_thread(password_digest, password, self.password_salt)
        if not hmac.compare_digest(digest, self.password_hash):
            self.failures.append(time.time())
            return Response(msg("stand_password_invalid"), status_code=403)
        data = self.get("pending", request_id, consume=True)
        if not data:
            return Response(msg("stand_auth_used"), status_code=400)
        client = await self.get_client(data["client_id"])
        if not client:
            return Response(msg("stand_auth_expired"), status_code=400)
        self.save("client", client.client_id, client.model_dump(mode="json"), 10 * 365 * 86400, "approved")
        params = AuthorizationParams.model_validate(data["params"])
        code = secrets.token_urlsafe(32)
        info = AuthorizationCode(code=code, scopes=[SCOPE], expires_at=time.time() + 120,
                                 client_id=data["client_id"], code_challenge=params.code_challenge,
                                 redirect_uri=params.redirect_uri,
                                 redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                                 resource=self.resource, subject="owner")
        self.save("code", code, info.model_dump(mode="json", exclude={"code"}), 120)
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code,
                                    state=params.state, iss=self.origin), status_code=303,
                                    headers={"Cache-Control": "no-store"})
        response.delete_cookie("mcp_csrf", secure=True, httponly=True, samesite="lax")
        return response

    def load(self, kind, token, model):
        data = self.get(kind, token)
        if data:
            data["code" if kind == "code" else "token"] = token
            return model.model_validate(data)
        return None

    async def load_authorization_code(self, client, authorization_code):
        return self.load("code", authorization_code, AuthorizationCode)

    def issue(self, client_id, scopes, family=None):
        access, refresh = (secrets.token_urlsafe(32) for _ in range(2))
        family = family or secrets.token_urlsafe(32)
        for kind, value, duration in (("access", access, 3600), ("refresh", refresh, 30 * 86400)):
            self.save(kind, value, {"client_id": client_id, "scopes": scopes,
                                   "resource": self.resource, "subject": "owner",
                                   "expires_at": int(time.time()) + duration}, duration, family)
        return OAuthToken(access_token=access, refresh_token=refresh, token_type="Bearer",
                          expires_in=3600, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        if not self.get("code", authorization_code.code, consume=True):
            raise TokenError("invalid_grant", msg("stand_code_used"))
        return self.issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        return self.load("refresh", refresh_token, RefreshToken)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        row = self.db.execute("SELECT family FROM records WHERE kind='refresh' AND id=?",
                              (self.key(refresh_token.token),)).fetchone()
        if not self.get("refresh", refresh_token.token, consume=True):
            raise TokenError("invalid_grant", msg("stand_refresh_used"))
        return self.issue(client.client_id, scopes, row[0])

    async def load_access_token(self, token):
        digest = self.key(token)
        if any(hmac.compare_digest(digest, known) for known in self.digests):
            return AccessToken(token=token, client_id="static", scopes=[SCOPE], resource=self.resource, subject="ci")
        return self.load("access", token, AccessToken)

    async def revoke_token(self, token):
        with self.db:
            row = self.db.execute("SELECT family FROM records WHERE id=? AND kind IN ('access','refresh')",
                                  (self.key(token.token),)).fetchone()
            if row:
                self.db.execute("DELETE FROM records WHERE family=?", (row[0],))
