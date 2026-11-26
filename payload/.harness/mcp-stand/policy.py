from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

from messages import msg

SECRETS_FILE = "stand.env"

STAND = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}")
SERVICE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
UNIT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.@:-]{0,252}")
NAMESPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
HEX = re.compile(r"(?:[0-9a-f]{2}){16,64}")
DIGEST = re.compile(r"[0-9a-f]{64}")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,31}")
PUBLIC_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?")
DOCKER_URL = re.compile(r"docker://([a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}):([0-9]{1,5})")
HTTP_URL = re.compile(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")

READ_PATHS = {
    "prometheus": r"/(?:metrics|-/(?:healthy|ready)|api/v1/(?:query|query_range|query_exemplars|series|labels|label/[^/]+/values|metadata|targets(?:/metadata)?|rules|alerts|status/(?:buildinfo|runtimeinfo|tsdb|flags)))",
    "loki": r"/(?:metrics|ready|loki/api/v1/(?:query|query_range|labels|label/[^/]+/values|series|index/(?:stats|volume|volume_range)|detected_(?:fields|labels)(?:/[^/]+/values)?|patterns)|loki/api/v1/status/buildinfo)",
    "tempo": r"/(?:metrics|ready|api/(?:v2/)?(?:traces/[0-9a-fA-F]{16,32}|search(?:/(?:tags|tag/[^/]+/values))?)|api/metrics/(?:query|query_range))",
    "grafana": r"/api/(?:health|search|dashboards/uid/[a-zA-Z0-9_-]+|folders(?:/[a-zA-Z0-9_-]+)?|datasources(?:/(?:uid/[a-zA-Z0-9_-]+|[0-9]+))?|alertmanager/grafana/api/v2/(?:alerts|silences)|ruler/grafana/api/v1/rules)",
    "alertmanager": r"/(?:metrics|-/(?:healthy|ready)|api/v2/(?:alerts(?:/groups)?|silences|silence/[a-zA-Z0-9-]+|status))",
    "collector": r"/(?:metrics)?",
}

FIELDS = {"stand", "title", "allow_write", "access", "telemetry", "docker_network", "host", "workloads",
          "repository", "state", "public_url", "listen", "allowed_hosts", "allowed_origins", "oauth",
          "static_tokens_env", "_"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def password_digest(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000).hex()


def name(value: object) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(msg("stand_invalid_name"))
    return value


def fits(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def names(pattern: re.Pattern[str], value: object, empty: bool = True) -> bool:
    return isinstance(value, list) and (empty or bool(value)) and all(fits(pattern, item) for item in value)


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def port(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536


def unknown(section: dict, known: set[str], prefix: str) -> list[str]:
    return [f"{prefix}.{key}" for key in sorted(set(section) - known)]


def access_fields(access: object) -> list[str]:
    if not isinstance(access, dict) or access.get("mode") not in ("local", "ssh"):
        return ["access.mode"]
    if access["mode"] == "local":
        return unknown(access, {"mode"}, "access")
    bad = unknown(access, {"mode", "host", "user", "port", "key", "known_hosts"}, "access")
    bad += [] if fits(HOST, access.get("host")) else ["access.host"]
    bad += [] if fits(USER, access.get("user")) else ["access.user"]
    bad += [] if port(access.get("port", 22)) else ["access.port"]
    bad += [f"access.{key}" for key in ("key", "known_hosts") if not text(access.get(key))]
    return bad


def telemetry_fields(config: dict) -> list[str]:
    telemetry = config.get("telemetry", {})
    if not isinstance(telemetry, dict):
        return ["telemetry"]
    bad, docker = [], False
    for service, entry in telemetry.items():
        field = f"telemetry.{service}"
        if not fits(SERVICE, service) or not isinstance(entry, dict):
            bad.append(field)
            continue
        bad += unknown(entry, {"kind", "url", "token_env"}, field)
        bad += [] if entry.get("kind") in READ_PATHS else [field + ".kind"]
        url = entry.get("url")
        docker = docker or fits(DOCKER_URL, url)
        bad += [] if fits(DOCKER_URL, url) or fits(HTTP_URL, url) else [field + ".url"]
        bad += [] if "token_env" not in entry or fits(ENV_NAME, entry["token_env"]) else [field + ".token_env"]
    if (docker or "docker_network" in config) and not fits(NAME, config.get("docker_network")):
        bad.append("docker_network")
    return bad


def host_fields(host: object) -> list[str]:
    if not isinstance(host, dict):
        return ["host"]
    bad = unknown(host, {"units", "backups"}, "host")
    bad += [] if names(UNIT, host.get("units", [])) else ["host.units"]
    backups = host.get("backups")
    if backups is not None:
        directory = backups.get("directory") if isinstance(backups, dict) else None
        if not isinstance(directory, str) or not directory.startswith("/"):
            bad.append("host.backups.directory")
        if not isinstance(backups, dict) or not names(NAME, backups.get("marks"), empty=False):
            bad.append("host.backups.marks")
    return bad


def workload_fields(workloads: object) -> list[str]:
    if not isinstance(workloads, dict):
        return ["workloads"]
    bad = unknown(workloads, {"docker", "kubernetes"}, "workloads")
    bad += [] if isinstance(workloads.get("docker", False), bool) else ["workloads.docker"]
    kubernetes = workloads.get("kubernetes")
    if kubernetes is None:
        return bad
    if not isinstance(kubernetes, dict):
        return bad + ["workloads.kubernetes"]
    bad += unknown(kubernetes, {"command", "namespaces", "default"}, "workloads.kubernetes")
    command = kubernetes.get("command")
    if not isinstance(command, list) or not command or not all(text(part) for part in command):
        bad.append("workloads.kubernetes.command")
    spaces = kubernetes.get("namespaces")
    if not names(NAMESPACE, spaces, empty=False):
        bad.append("workloads.kubernetes.namespaces")
    elif kubernetes.get("default", spaces[0]) not in spaces:
        bad.append("workloads.kubernetes.default")
    return bad


def http_fields(config: dict) -> list[str]:
    if "public_url" not in config:
        return []
    bad = [] if fits(PUBLIC_URL, config["public_url"]) else ["public_url"]
    listen = config.get("listen")
    if not isinstance(listen, dict) or not text(listen.get("host")) or not port(listen.get("port")) \
            or unknown(listen, {"host", "port"}, "listen"):
        bad.append("listen")
    for field in ("allowed_hosts", "allowed_origins"):
        values = config.get(field, [])
        if not isinstance(values, list) or not all(text(value) for value in values):
            bad.append(field)
    oauth = config.get("oauth")
    if not isinstance(oauth, dict):
        return bad + ["oauth"]
    bad += unknown(oauth, {"password_salt", "password_hash", "redirect_schemes"}, "oauth")
    bad += [] if fits(HEX, oauth.get("password_salt")) else ["oauth.password_salt"]
    bad += [] if fits(DIGEST, oauth.get("password_hash")) else ["oauth.password_hash"]
    bad += [] if names(SCHEME, oauth.get("redirect_schemes", [])) else ["oauth.redirect_schemes"]
    return bad


def problems(config: object) -> list[str]:
    if not isinstance(config, dict):
        return [msg("stand_config_object")]
    if config.get("allow_write") is not False:
        return [msg("stand_write_disabled")]
    bad = unknown(config, FIELDS, "config")
    bad += [] if fits(STAND, config.get("stand")) else ["stand"]
    bad += [] if "title" not in config or text(config["title"]) else ["title"]
    bad += access_fields(config.get("access"))
    bad += telemetry_fields(config)
    bad += host_fields(config.get("host", {}))
    bad += workload_fields(config.get("workloads", {}))
    bad += http_fields(config)
    bad += [field for field in ("repository", "state") if field in config and not text(config[field])]
    if "static_tokens_env" in config and not fits(ENV_NAME, config["static_tokens_env"]):
        bad.append("static_tokens_env")
    return [msg("stand_config_field", field) for field in bad]


def load(path: str | Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    found = problems(config)
    if found:
        raise ValueError(msg("stand_config_invalid", path, "; ".join(found)))
    return config


def environment(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    secrets = Path(path).with_name(SECRETS_FILE)
    if secrets.is_file():
        for line in secrets.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and ENV_NAME.fullmatch(key.strip()):
                values[key.strip()] = value.strip()
    values.update(os.environ)
    return values


def validate_read(config: dict, service: str, path: str, params: dict) -> dict:
    entry = config.get("telemetry", {}).get(service) if isinstance(service, str) else None
    if not isinstance(entry, dict) or not isinstance(path, str):
        raise ValueError(msg("stand_source_invalid"))
    if not re.fullmatch(READ_PATHS[entry["kind"]], path) or any(c in path for c in "%?#\\"):
        raise ValueError(msg("stand_path_denied"))
    if ".." in path or not isinstance(params, dict) or len(params) > 40:
        raise ValueError(msg("stand_params_invalid"))
    for key, value in params.items():
        if not isinstance(key, str) or len(key) > 100:
            raise ValueError(msg("stand_param_name"))
        values = value if isinstance(value, list) else [value]
        if len(values) > 100 or any(not isinstance(v, (str, int, float, bool)) for v in values):
            raise ValueError(msg("stand_param_value"))
    return entry
