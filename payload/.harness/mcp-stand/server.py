#!/usr/bin/env python3
"""MCP-сервер чтения одного стенда.

    python3 server.py --config stand.json                     # streamable HTTP: OAuth владельца и токены CI
    python3 server.py --config stand.json --transport stdio   # агент на машине экземпляра
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import shutil
import signal
import sys
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
import uvicorn

from messages import msg
from oauth import SCOPE, OwnerOAuth, static_digests
from policy import environment, load, validate_read

PAGE = 24000
RESULTS = 8
RESULT_TTL = 600
RESULT_MEMORY = 128 * 1024 * 1024
MAX_SOURCE = 32 * 1024 * 1024
DISPATCH_TIMEOUT = 60
DISPATCHER = Path(__file__).resolve().with_name("dispatcher.py")
SECTIONS = ("host_status", "workloads", "backup_status")
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

REPOSITORY_HELP = ("Read the tracked source snapshot of the stand repository or list paths by prefix. Returns "
                   "repository, ref and commit. No working tree files, Git credentials or write operations are "
                   "exposed. Binary content uses base64; large content uses read_result.")
TELEMETRY_HELP = ("Read stand telemetry using GET. Services (name: kind): %s. Kinds: prometheus (PromQL), loki "
                  "(LogQL), tempo (TraceQL), grafana, alertmanager, collector. Use their read API paths; writes "
                  "and arbitrary URLs are forbidden. Large responses have result_id for read_result.")
STATUS_HELP = ("Read host_status (uptime, memory, disk, service state), workloads (Docker containers and "
               "Kubernetes pods) or backup_status (backup marks) of the stand.")
LOGS_HELP = ("Read logs of a named Docker container or Kubernetes pod. kind=docker|kubernetes; Kubernetes "
             "namespaces: %s; since uses s/m/h; lines=1..10000. Loki queries provide archived logs.")
RESULT_HELP = ("Read the next 24000 characters of a large diagnostic response. Cache expires after 10 minutes "
               "or 8 subsequent large results; rerun the original query if expired.")


def private_key(config: dict) -> str:
    source = Path(config["access"]["key"])
    info = source.stat()
    if info.st_uid == os.getuid() and not info.st_mode & 0o077:
        return str(source)
    target = Path(config.get("state", "/state")) / "id_ssh"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous = os.umask(0o077)
    try:
        shutil.copyfile(source, target)
    finally:
        os.umask(previous)
    os.chmod(target, 0o600)
    return str(target)


def ssh_command(access: dict, key: str) -> list[str]:
    return ["ssh", "-T", "-F", "none", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + access["known_hosts"], "-o", "ConnectTimeout=10",
            "-o", "IdentitiesOnly=yes", "-i", key, "-p", str(access.get("port", 22)), "-l", access["user"],
            "--", access["host"], "diagnostics"]


def snapshot_info(archive: zipfile.ZipFile) -> dict:
    comment = archive.comment.decode("utf-8", errors="replace").strip()
    try:
        info = json.loads(comment)
    except ValueError:
        info = None
    return info if isinstance(info, dict) else {"commit": comment}


class Stand:
    def __init__(self, config: dict, path: Path):
        self.config = config
        self.env = environment(path)
        self.results: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.gate = asyncio.Semaphore(4)
        if config["access"]["mode"] == "ssh":
            self.command = ssh_command(config["access"], private_key(config))
        else:
            self.command = [sys.executable, str(DISPATCHER), "--config", str(path.resolve())]
        self.archive = zipfile.ZipFile(config["repository"]) if config.get("repository") else None
        self.info = snapshot_info(self.archive) if self.archive else {}

    def paged(self, body: str) -> dict:
        if len(body) <= PAGE:
            return {"content": body, "complete": True}
        now = time.time()
        for key in list(self.results):
            if self.results[key][0] < now - RESULT_TTL:
                del self.results[key]
        while self.results and (len(self.results) >= RESULTS or sum(sys.getsizeof(item[1]) for item in
                                self.results.values()) + sys.getsizeof(body) > RESULT_MEMORY):
            self.results.popitem(last=False)
        key = secrets.token_urlsafe(24)
        self.results[key] = (now, body)
        return {"result_id": key, "total_characters": len(body), "content": body[:PAGE],
                "next_offset": PAGE, "complete": False, "expires_in_seconds": RESULT_TTL}

    async def dispatch(self, request: dict) -> dict:
        process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), DISPATCH_TIMEOUT)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        try:
            response = await asyncio.to_thread(json.loads, output) if output else None
        except ValueError:
            response = None
        if not isinstance(response, dict):
            raise ToolError(msg("stand_dispatcher_unavailable"))
        if process.returncode and "error" not in response:
            return {"error": msg("stand_diagnostic_error")}
        return response

    async def diagnostic(self, request: dict) -> dict:
        async with self.gate:
            try:
                response = await self.dispatch(request)
            except asyncio.TimeoutError as error:
                raise ToolError(msg("stand_timeout", DISPATCH_TIMEOUT)) from error
        if "error" in response:
            raise ToolError(str(response["error"]))
        result = response.get("result")
        return self.paged(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))

    async def repository_read(self, path: str | None = None, prefix: str = "") -> dict:
        if self.archive is None:
            raise ToolError(msg("stand_repository_off"))
        if path is None:
            names = [item for item in self.archive.namelist() if item.startswith(prefix) and not item.endswith("/")]
            return {**self.info, **self.paged(json.dumps(names, ensure_ascii=False)), "encoding": "json"}
        try:
            info = self.archive.getinfo(path)
        except KeyError as error:
            raise ToolError(msg("stand_source_file_missing")) from error
        if info.is_dir():
            raise ToolError(msg("stand_source_file_missing"))
        if info.file_size > MAX_SOURCE:
            raise ToolError(msg("stand_source_file_large"))
        content = await asyncio.to_thread(self.archive.read, info)
        try:
            body, encoding = content.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            body, encoding = base64.b64encode(content).decode(), "base64"
        return {**self.info, "path": path, "encoding": encoding, **self.paged(body)}

    async def telemetry_read(self, service: str, path: str, params: dict | None = None) -> dict:
        try:
            validate_read(self.config, service, path, params or {})
        except ValueError as error:
            raise ToolError(str(error)) from error
        return await self.diagnostic({"operation": "telemetry", "service": service, "path": path,
                                      "params": params or {}})

    async def stand_status(self, section: str = "host_status") -> dict:
        if section not in SECTIONS:
            raise ToolError(msg("stand_section_invalid"))
        return await self.diagnostic({"operation": section})

    async def workload_logs(self, kind: str, target: str, namespace: str | None = None,
                            container: str | None = None, lines: int = 200, since: str = "1h",
                            previous: bool = False) -> dict:
        return await self.diagnostic({"operation": "logs", "kind": kind, "target": target, "namespace": namespace,
                                      "container": container, "lines": lines, "since": since,
                                      "previous": previous})

    async def read_result(self, result_id: str, offset: int = 0) -> dict:
        item = self.results.get(result_id)
        if not item or item[0] < time.time() - RESULT_TTL:
            raise ToolError(msg("stand_result_expired"))
        body = item[1]
        if not 0 <= offset <= len(body):
            raise ToolError(msg("stand_offset_invalid"))
        end = min(len(body), offset + PAGE)
        return {"content": body[offset:end], "total_characters": len(body),
                "next_offset": end if end < len(body) else None, "complete": end == len(body)}


def descriptions(config: dict) -> dict[str, str]:
    services = ", ".join(f"{key}: {entry['kind']}" for key, entry in sorted(config.get("telemetry", {}).items()))
    spaces = ", ".join(config.get("workloads", {}).get("kubernetes", {}).get("namespaces", [])) or "none"
    return {"repository_read": REPOSITORY_HELP, "telemetry_read": TELEMETRY_HELP % (services or "none"),
            "stand_status": STATUS_HELP, "workload_logs": LOGS_HELP % spaces, "read_result": RESULT_HELP}


def register(server: MCPServer, stand: Stand) -> None:
    for tool, description in descriptions(stand.config).items():
        server.add_tool(getattr(stand, tool), name=tool, description=description, annotations=READ)


def identity(config: dict) -> dict:
    title = config.get("title", config["stand"])
    return {"name": "mcp-stand", "title": title,
            "instructions": f"Read-only diagnostics of the stand {title}: repository snapshot, telemetry, "
                            "host state, workloads and logs."}


def http_app(config: dict, stand: Stand):
    for field in ("public_url", "listen", "oauth"):
        if field not in config:
            raise ValueError(msg("stand_config_field", field))
    origin = config["public_url"].rstrip("/")
    state = Path(config.get("state", "/state"))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    provider = OwnerOAuth(config, str(state / "oauth.sqlite"), static_digests(config, stand.env))
    server = MCPServer(**identity(config), auth_server_provider=provider,
                       auth=AuthSettings(issuer_url=origin, resource_server_url=origin + "/mcp",
                                         validate_token_resource=True, required_scopes=[SCOPE],
                                         client_registration_options=ClientRegistrationOptions(
                                             enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
                                         revocation_options=RevocationOptions(enabled=True)))
    register(server, stand)
    server.custom_route("/login", methods=["GET", "POST"])(provider.login)

    async def health(request):
        return JSONResponse({"status": "ok", "stand": config["stand"], "allow_write": False})

    server.custom_route("/healthz", methods=["GET"])(health)
    listen = config["listen"]
    hosts = config.get("allowed_hosts") or [urlsplit(origin).netloc, f"{listen['host']}:{listen['port']}"]
    origins = config.get("allowed_origins") or [origin]
    return server.streamable_http_app(stateless_http=True, json_response=True, host=listen["host"],
                                      transport_security=TransportSecuritySettings(
                                          enable_dns_rebinding_protection=True,
                                          allowed_hosts=hosts, allowed_origins=origins))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.environ.get("STAND_CONFIG", "/config/stand.json"))
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        if config.get("allow_write") is not False:
            raise ValueError(msg("stand_write_disabled"))
        stand = Stand(config, Path(args.config))
        app = None if args.transport == "stdio" else http_app(config, stand)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        return 1
    if app is None:
        server = MCPServer(**identity(config))
        register(server, stand)
        server.run("stdio")
        return 0
    uvicorn.run(app, host=config["listen"]["host"], port=config["listen"]["port"], access_log=False,
                proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
