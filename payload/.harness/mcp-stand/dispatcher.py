#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from messages import msg
from policy import DOCKER_URL, NoRedirect, environment, load, name, validate_read

MAX_RESPONSE = 32 * 1024 * 1024
MAX_REQUEST = 64 * 1024
TIMEOUT = 45
DEADLINE = 55
CONFIG = Path(__file__).resolve().with_name("stand.json")
SINCE = re.compile(r"[1-9][0-9]{0,5}[smh]")


def run(*args: str, include_stderr: bool = False, check: bool = True) -> str:
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        streams = {process.stdout: bytearray(), process.stderr: bytearray()}
        deadline, total = time.monotonic() + TIMEOUT, 0
        try:
            with selectors.DefaultSelector() as selector:
                for stream in streams:
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, TIMEOUT)
                    for key, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_RESPONSE:
                            raise ValueError(msg("stand_result_large"))
                        streams[key.fileobj].extend(chunk)
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if check and process.returncode:
                raise subprocess.CalledProcessError(process.returncode, args)
            body = streams[process.stdout]
            if include_stderr:
                body += streams[process.stderr]
            return body.decode("utf-8", errors="replace")
        finally:
            if process.poll() is None:
                process.kill()


def endpoint(entry: dict, config: dict) -> str:
    docker = DOCKER_URL.fullmatch(entry["url"])
    if not docker:
        return entry["url"].rstrip("/")
    container, port = docker.groups()
    inspected = json.loads(run("docker", "inspect", "--type", "container", container))[0]
    networks = inspected.get("NetworkSettings", {}).get("Networks") or {}
    address = (networks.get(config["docker_network"]) or {}).get("IPAddress")
    if not address:
        raise ValueError(msg("stand_network_missing", container, config["docker_network"]))
    return f"http://{address}:{port}"


def telemetry(request: dict, config: dict, env: dict[str, str]) -> str:
    service, path, params = request.get("service"), request.get("path"), request.get("params") or {}
    entry = validate_read(config, service, path, params)
    suffix = urllib.parse.urlencode(params, doseq=True)
    url = endpoint(entry, config) + path + ("?" + suffix if suffix else "")
    headers = {"Accept": "application/json"}
    token = env.get(entry["token_env"]) if entry.get("token_env") else None
    if token:
        headers["Authorization"] = "Bearer " + token
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError(msg("stand_result_large"))
    return body.decode("utf-8", errors="replace")


def host_status(config: dict) -> dict:
    status = {"uptime": run("uptime"), "memory": run("free", "-b"), "disk": run("df", "-h")}
    units = config.get("host", {}).get("units", [])
    if units:
        status["services"] = run("systemctl", "is-active", "--", *units, check=False)
    return status


def workloads(config: dict) -> dict:
    setup = config.get("workloads", {})
    found: dict = {}
    if setup.get("docker"):
        found["containers"] = run("docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}")
    kubernetes = setup.get("kubernetes")
    if kubernetes:
        found["pods"] = {space: run(*kubernetes["command"], "get", "pods", "-n", space, "-o", "wide")
                         for space in kubernetes["namespaces"]}
    if not found:
        raise ValueError(msg("stand_section_off", "workloads"))
    return found


def backup_status(config: dict) -> dict:
    backups = config.get("host", {}).get("backups")
    if not backups:
        raise ValueError(msg("stand_section_off", "backup_status"))
    directory = Path(backups["directory"])
    return {mark: (directory / mark).read_text(encoding="utf-8", errors="replace").strip()
            if (directory / mark).is_file() else None for mark in map(name, backups["marks"])}


def log_window(request: dict) -> tuple[str, str, str]:
    target, lines, since = name(request.get("target")), request.get("lines", 200), request.get("since", "1h")
    if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 10000:
        raise ValueError(msg("stand_log_lines"))
    if not isinstance(since, str) or not SINCE.fullmatch(since):
        raise ValueError(msg("stand_log_period"))
    return target, str(lines), since


def logs(request: dict, config: dict) -> str:
    target, lines, since = log_window(request)
    setup = config.get("workloads", {})
    kind = request.get("kind")
    if kind == "docker" and setup.get("docker"):
        known = run("docker", "ps", "-a", "--format", "{{.Names}}").splitlines()
        if target not in known:
            raise ValueError(msg("stand_container_missing"))
        return run("docker", "logs", "--tail", lines, "--since", since, target, include_stderr=True)
    kubernetes = setup.get("kubernetes")
    if kind == "kubernetes" and kubernetes:
        space = request.get("namespace") or kubernetes.get("default", kubernetes["namespaces"][0])
        if space not in kubernetes["namespaces"]:
            raise ValueError(msg("stand_namespace_denied", name(space)))
        args = [*kubernetes["command"], "logs", "-n", space, target, "--tail", lines, "--since", since]
        if request.get("container"):
            args += ["-c", name(request["container"])]
        if request.get("previous") is True:
            args.append("--previous")
        return run(*args)
    raise ValueError(msg("stand_operation_denied"))


SECTIONS = {"host_status": host_status, "workloads": workloads, "backup_status": backup_status}


def execute(request: dict, config: dict, env: dict[str, str]) -> object:
    if config.get("allow_write") is not False:
        raise ValueError(msg("stand_write_disabled"))
    operation = request.get("operation")
    if operation == "telemetry":
        return telemetry(request, config, env)
    if operation == "logs":
        return logs(request, config)
    if isinstance(operation, str) and operation in SECTIONS:
        return SECTIONS[operation](config)
    raise ValueError(msg("stand_operation_denied"))


def expired(signum: int, frame: object) -> None:
    raise TimeoutError(msg("stand_timeout", DEADLINE))


def handle(data: bytes, path: Path) -> dict:
    if len(data) > MAX_REQUEST:
        return {"error": msg("stand_request_large")}
    try:
        request = json.loads(data)
        if not isinstance(request, dict):
            return {"error": msg("stand_request_object")}
        return {"result": execute(request, load(path), environment(path))}
    except urllib.error.HTTPError as error:
        error.close()
        return {"error": msg("stand_upstream_http", error.code)}
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        return {"error": str(error)}


def main() -> int:
    args = sys.argv[1:]
    if args and (len(args) != 2 or args[0] != "--config") or \
            os.environ.get("SSH_ORIGINAL_COMMAND", "") not in ("", "diagnostics"):
        response = {"error": msg("stand_command_denied")}
    else:
        data = sys.stdin.buffer.read(MAX_REQUEST + 1)
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(DEADLINE)
        response = handle(data, Path(args[1]) if args else CONFIG)
        signal.alarm(0)
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
    return 0 if "result" in response else 1


if __name__ == "__main__":
    sys.exit(main())
