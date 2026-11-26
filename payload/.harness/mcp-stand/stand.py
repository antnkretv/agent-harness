#!/usr/bin/env python3
"""Подготовка и проверка экземпляра mcp-stand.

    python3 stand.py check stand.json                         # конфигурация стенда
    python3 stand.py token                                    # токен CI и его sha256 для static_tokens_env
    python3 stand.py password                                 # пароль владельца и поля oauth конфигурации
    python3 stand.py snapshot --repository . --ref origin/main --out repository.zip
    python3 stand.py verify https://mcp.example.com --token-env STAND_TOKEN [--service prometheus]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from messages import msg
from policy import NoRedirect, load, password_digest

TOOLS = {"repository_read", "telemetry_read", "stand_status", "workload_logs", "read_result"}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


class VerifyError(RuntimeError):
    pass


def check(args: argparse.Namespace) -> int:
    try:
        config = load(args.config)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(msg("stand_check_ok", config["stand"], config["access"]["mode"]))
    return 0


def token(_: argparse.Namespace) -> int:
    value = secrets.token_urlsafe(32)
    print(json.dumps({"token": value, "sha256": hashlib.sha256(value.encode()).hexdigest()}))
    return 0


def password(_: argparse.Namespace) -> int:
    value, salt = secrets.token_urlsafe(32), secrets.token_bytes(32)
    digest = password_digest(value, salt)
    print(json.dumps({"password": value, "oauth": {"password_salt": salt.hex(), "password_hash": digest}}))
    return 0


def git(repository: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repository, *args], check=True, capture_output=True, text=True).stdout.strip()


def snapshot(args: argparse.Namespace) -> int:
    commit = git(args.repository, "rev-parse", "--verify", args.ref + "^{commit}")
    out = Path(args.out)
    git(args.repository, "archive", "--format=zip", "--output=" + str(out.resolve()), commit)
    info = {"repository": args.name or Path(args.repository).resolve().name, "ref": args.ref, "commit": commit}
    with zipfile.ZipFile(out, "a") as archive:
        archive.comment = json.dumps(info).encode()
    print(msg("stand_snapshot_ready", out, args.ref, commit))
    return 0


def request(opener, url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    try:
        with opener.open(urllib.request.Request(url, data=data, headers=headers or {}), timeout=75) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()


def expect(opener, url: str, status: int = 200, data: bytes | None = None, headers: dict | None = None) -> dict:
    code, body = request(opener, url, data, headers)
    if code != status:
        raise VerifyError(msg("stand_verify_http", code, url))
    return json.loads(body) if body else {}


def verify(args: argparse.Namespace) -> int:
    origin = args.url.rstrip("/")
    secret = os.environ.get(args.token_env, "")
    if not secret:
        print(msg("stand_verify_token_missing", args.token_env), file=sys.stderr)
        return 1
    opener = urllib.request.build_opener(NoRedirect())
    try:
        steps(opener, origin, {**MCP_HEADERS, "Authorization": "Bearer " + secret}, args.service)
    except (VerifyError, OSError, ValueError, KeyError) as error:
        print(msg("stand_verify_failed", error), file=sys.stderr)
        return 1
    print(msg("stand_verify_pass"))
    return 0


def steps(opener, origin: str, headers: dict, service: str | None) -> None:
    if expect(opener, origin + "/healthz").get("allow_write") is not False:
        raise VerifyError(msg("stand_verify_step", "healthz"))
    if expect(opener, origin + "/.well-known/oauth-authorization-server").get("issuer") != origin:
        raise VerifyError(msg("stand_verify_step", "oauth-authorization-server"))
    if expect(opener, origin + "/.well-known/oauth-protected-resource/mcp").get("resource") != origin + "/mcp":
        raise VerifyError(msg("stand_verify_step", "oauth-protected-resource"))
    if request(opener, origin + "/mcp", b"{}", MCP_HEADERS)[0] != 401:
        raise VerifyError(msg("stand_verify_auth_failed"))

    def rpc(method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        response = expect(opener, origin + "/mcp", data=body, headers=headers)
        if "error" in response:
            raise VerifyError(msg("stand_verify_step", method))
        return response["result"]

    def call(name: str, arguments: dict) -> bool:
        return not rpc("tools/call", {"name": name, "arguments": arguments}).get("isError")

    init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                              "clientInfo": {"name": "mcp-stand-verify", "version": "1"}})
    headers["MCP-Protocol-Version"] = init["protocolVersion"]
    tools = rpc("tools/list", {})["tools"]
    if {tool["name"] for tool in tools} != TOOLS or not all(tool["annotations"]["readOnlyHint"] for tool in tools):
        raise VerifyError(msg("stand_verify_step", "tools/list"))
    if call("repository_read", {"path": ".git/config"}):
        raise VerifyError(msg("stand_verify_step", "repository_read"))
    if not call("stand_status", {"section": "host_status"}):
        raise VerifyError(msg("stand_verify_step", "stand_status"))
    if service:
        if not call("telemetry_read", {"service": service, "path": "/api/v1/query", "params": {"query": "up"}}):
            raise VerifyError(msg("stand_verify_step", "telemetry_read"))
        if call("telemetry_read", {"service": service, "path": "/api/v1/admin/tsdb/delete_series"}):
            raise VerifyError(msg("stand_verify_step", "telemetry_read"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("config")
    commands.add_parser("token")
    commands.add_parser("password")
    archive = commands.add_parser("snapshot")
    archive.add_argument("--repository", required=True)
    archive.add_argument("--ref", required=True)
    archive.add_argument("--out", required=True)
    archive.add_argument("--name")
    probe = commands.add_parser("verify")
    probe.add_argument("url")
    probe.add_argument("--token-env", required=True)
    probe.add_argument("--service")
    args = parser.parse_args(argv)
    handlers = {"check": check, "token": token, "password": password, "snapshot": snapshot, "verify": verify}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
