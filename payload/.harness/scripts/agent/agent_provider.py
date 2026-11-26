from __future__ import annotations
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from ci_common import REVIEW_SCHEMA, ROOT, annotation, forge, msg
from executables import command as executable_command
from manifest import environ
from agent_models import Server, Session, SessionError
from permission_policy import codex_deny, cursor_allow, cursor_deny, executable_denies

HERE = Path(__file__).resolve().parent
SCHEMA = Path(ROOT) / REVIEW_SCHEMA

KINDS = {
    "claude-code": {"binary": "claude", "denies": (), "model": "claude-opus-5-5",
                    "efforts": {"medium": "medium", "high": "high", "max": "max"}},
    "codex": {"binary": "codex", "denies": ("Bash(gh pr merge:*)", "Bash(gh issue close:*)", "Bash(glab mr merge:*)", "Bash(glab issue close:*)"),
              "model": "gpt-6.1-sol", "efforts": {"medium": "medium", "high": "high", "max": "xhigh"},
              "commonPrompt": ".harness/prompts/agent/unattended-codex.md"},
    "cursor": {"binary": "cursor-agent", "denies": (), "model": "grok-4.7", "efforts": {}},
}

BINARIES = {kind: spec["binary"] for kind, spec in KINDS.items()}

CREDENTIALS = {
    "claude-code": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "codex": ("CODEX_API_KEY",),
    "cursor": ("CURSOR_API_KEY",),
}


def session_link() -> str:
    if url := forge.run_url():
        return url
    remote = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "")
    return "https://claude.ai/code/session_" + remote.removeprefix("cse_") if remote.startswith("cse_") else ""


def session_model(session: str = "") -> str:
    import session_cost

    if os.environ.get("HARNESS_AGENT_RUN"):
        return session_cost.label(os.environ.get("HARNESS_AGENT_MODEL", ""), os.environ.get("HARNESS_AGENT_EFFORT", ""))
    if not session:
        return ""
    import change_cost

    return session_cost.cost(change_cost.transcript(change_cost.state(session)))["SessionModel"]


def authorized(provider: str) -> bool:
    given = (os.environ.get("AGENT_SECRET") or "").strip().lower()
    if given:
        return given == "true"
    names = () if provider == "codex" else CREDENTIALS.get(provider, ())
    return not names or any(os.environ.get(name) for name in names)


FOREIGN = ("OPENAI_API_KEY",)

REVIEW_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TMP", "TEMP", "TERM", "CI",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
    "NVM_DIR", "DOTNET_ROOT", "HARNESS_PYTHON", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PATHEXT", "HOMEDRIVE", "HOMEPATH",
)

REVIEW_ENV_PREFIXES = ("LC_", "XDG_")

SESSION_MARK = {"HARNESS_AGENT_RUN": "1"}

CURSOR_WORKSPACE_CONFIG = (".cursor", ".claude/settings.json", ".claude/settings.local.json")

SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

AGENT_NAME = "harness-agent"
AGENT_EMAIL = "harness-agent@users.noreply.invalid"

IDENTITY = (
    ("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"), AGENT_NAME),
    ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"), AGENT_EMAIL),
)

PROBE_SECONDS = 30


def read_prompt(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise SessionError(msg("agent_run_prompt_unread", path, error.strerror or error)) from error
    if not text.strip():
        raise SessionError(msg("agent_run_no_prompt", path))
    return text


def allowed_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)


def read_servers(path: str | None) -> dict[str, Server]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SessionError(msg("agent_run_mcp_unread", path, error)) from error
    entries = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise SessionError(msg("agent_run_mcp_unread", path, "mcpServers"))
    servers = {}
    for name, entry in entries.items():
        entry = entry if isinstance(entry, dict) else {}
        token = entry.get("bearerTokenEnv", "")
        if not SERVER_NAME.match(name) or not allowed_url(entry.get("url")) \
                or not isinstance(token, str) or (token and not ENV_NAME.match(token)):
            raise SessionError(msg("agent_run_mcp_server_invalid", name))
        if token and not environ(token):
            raise SessionError(msg("agent_run_mcp_no_token", name, token))
        servers[name] = Server(entry["url"], token)
    return servers


def launched(args: argparse.Namespace) -> dict[str, str]:
    return {"HARNESS_AGENT_MODEL": args.model or "", "HARNESS_AGENT_EFFORT": args.effort or ""}


def environment(args: argparse.Namespace, servers: dict[str, Server]) -> dict[str, str]:
    bearer = {server.token: environ(server.token) for server in servers.values() if server.token}
    if args.mode != "agent":
        keep = set(REVIEW_ENV) | set(CREDENTIALS[args.provider])
        if os.name == "nt":
            keep = {name.upper() for name in keep}
        return {**{name: value for name, value in os.environ.items()
                   if (name.upper() if os.name == "nt" else name) in keep or name.startswith(REVIEW_ENV_PREFIXES)},
                **bearer, **SESSION_MARK, **launched(args)}

    drop = set(FOREIGN) | {name for kind, names in CREDENTIALS.items() if kind != args.provider for name in names}
    env = {**{name: value for name, value in os.environ.items() if name not in drop}, **bearer, **SESSION_MARK,
           **launched(args)}
    try:
        return forge.git_env(env)
    except forge.ForgeError as error:
        print(annotation("warning", "agent_run_no_git_env", error))
        return env


def configured(env: dict[str, str], workdir: Path, key: str) -> bool:
    try:
        done = subprocess.run(["git", "config", key], cwd=workdir, env=env, capture_output=True, text=True,
                              check=False)
    except OSError:
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


def identity(env: dict[str, str], workdir: Path) -> None:
    for key, names, default in IDENTITY:
        if all(env.get(name) for name in names) or configured(env, workdir, key):
            continue
        for name in names:
            if not env.get(name):
                env[name] = default


def write_private(path: Path, data: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def deny_hook(session: Session, kind: str) -> str:
    return shlex.join([sys.executable, str(HERE.parents[1] / "hooks" / "agent-deny.py"), kind,
                       session.args.disallowed_tools])


def claude_command(session: Session) -> list[str]:
    args, env = session.args, session.env
    if not any(env.get(name) for name in CREDENTIALS["claude-code"]):
        raise SessionError(msg("agent_run_no_auth_claude"))
    command = [*executable_command("claude", path=env.get("PATH")), "-p", "--output-format", "stream-json", "--verbose", "--model", args.model,
               "--permission-mode", "dontAsk", "--no-session-persistence", "--strict-mcp-config"]
    if args.effort:
        command += ["--effort", args.effort]
    if args.allowed_tools:
        command += ["--allowedTools", args.allowed_tools]
    disallowed = ",".join(filter(None, [args.disallowed_tools, "Bash" if args.mode == "reply" else ""]))
    if disallowed:
        command += ["--disallowedTools", disallowed]
    for folder in args.add_dir:
        command += ["--add-dir", folder]
    if session.readonly:
        command += ["--setting-sources", "user"]
    settings = session.temp / "settings.json"
    write_private(settings, {"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": deny_hook(session, "claude-code")}]}]}})
    command += ["--settings", str(settings)]
    if session.review:
        command += ["--json-schema", SCHEMA.read_text(encoding="utf-8")]
    if session.servers:
        config = session.temp / "mcp.json"
        write_private(config, {"mcpServers": {
            name: {"type": "http", "url": server.url,
                   **({"headers": {"Authorization": "Bearer " + env[server.token]}} if server.token else {})}
            for name, server in session.servers.items()}})
        command += ["--mcp-config", str(config)]
    return command


def codex_sandbox_options(home: Path) -> list[str]:
    if os.name != "nt":
        return []
    path = home / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        windows = data.get("windows", {})
        selected = windows.get("sandbox", "mxc") if isinstance(windows, dict) else None
        features = data.get("features", {})
        prefer_mxc = features.get("prefer_mxc", False) if isinstance(features, dict) else None
        if selected not in ("mxc", "elevated", "unelevated") or not isinstance(prefer_mxc, bool):
            raise ValueError
    except (OSError, ValueError):
        raise SessionError(msg("agent_run_codex_sandbox_config", path)) from None
    return ["--config", "features.prefer_mxc=" + json.dumps(prefer_mxc),
            "--config", "windows.sandbox=" + json.dumps(selected)]


def codex_command(session: Session) -> list[str]:
    args, env = session.args, session.env
    home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    chatgpt = (home / "auth.json").is_file()
    if chatgpt:
        env.pop("CODEX_API_KEY", None)
    elif not env.get("CODEX_API_KEY"):
        raise SessionError(msg("agent_run_no_auth_codex", home))
    env["CODEX_HOME"] = str(home)

    sandbox_options = codex_sandbox_options(home)
    command = [*executable_command("codex", path=env.get("PATH")), "exec", "--json", "--ephemeral", "--ignore-user-config", *sandbox_options, "--skip-git-repo-check",
               "--cd", str(session.workdir), "--model", args.model,
               "--config", 'approval_policy="never"',
               "--config", 'cli_auth_credentials_store="file"',
               "--sandbox", "read-only" if session.readonly else "danger-full-access",
               "--output-last-message", str(session.temp / "last-message.md"),
               "--dangerously-bypass-hook-trust",
               "--config", "hooks.PreToolUse=[{hooks=[{type=\"command\",command=%s}]}]" % json.dumps(deny_hook(session, "codex"))]
    if chatgpt:
        command += ["--config", 'forced_login_method="chatgpt"']
    if args.effort:
        command += ["--config", "model_reasoning_effort=" + json.dumps(args.effort)]
    for name, server in session.servers.items():
        command += ["--config", f"mcp_servers.{name}.url={json.dumps(server.url)}"]
        if server.token:
            command += ["--config", f"mcp_servers.{name}.bearer_token_env_var={json.dumps(server.token)}"]
    if session.review:
        command += ["--output-schema", str(SCHEMA), "--config", 'web_search="disabled"']
        codex_probe(env, session.workdir, sandbox_options)
    session.undo.append(codex_rules(home / "rules", executable_denies(args.disallowed_tools, session.readonly)))
    command.append("-")
    return command


def codex_rules(folder: Path, spec: str) -> Callable[[], None]:
    rules = codex_deny(spec)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"agent-run-{os.getpid()}.rules"
    path.write_text("\n".join(rules) + "\n", encoding="utf-8")
    return lambda: path.unlink(missing_ok=True)


def codex_probe(env: dict[str, str], workdir: Path, sandbox_options: list[str]) -> None:
    probe = [env["COMSPEC"], "/d", "/c", "exit", "0"] if os.name == "nt" else [shutil.which("true") or "/bin/true"]
    try:
        subprocess.run([*executable_command("codex", path=env.get("PATH")), "sandbox", *sandbox_options, "--config", 'sandbox_mode="read-only"', "--", *probe],
                       cwd=workdir, env=env, check=True, timeout=PROBE_SECONDS, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise SessionError(msg("agent_run_codex_sandbox", error)) from error


def cursor_command(session: Session) -> list[str]:
    args = session.args
    if not session.env.get("CURSOR_API_KEY"):
        raise SessionError(msg("agent_run_no_auth_cursor"))
    model = args.model
    if args.effort and "[" not in model and model != "auto":
        model += f"[effort={args.effort}]"
    home = cursor_home(session)
    session.env["CURSOR_CONFIG_DIR"] = str(home / ".cursor")
    if session.review:
        for name in CURSOR_WORKSPACE_CONFIG:
            hide(session, name)
    command = [*executable_command("cursor-agent", path=session.env.get("PATH")), "-p", "--output-format", "stream-json", "--model", model,
               "--workspace", str(session.workdir), "--trust", "--disable-project-configs"]
    if session.readonly:
        command += ["--mode", "ask"]
    elif not args.allowed_tools:
        command.append("--force")
    for folder in args.add_dir:
        command += ["--add-dir", folder]
    if session.servers:
        command.append("--approve-mcps")
    return command


def read_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cursor_home(session: Session) -> Path:
    real = Path(os.environ.get("HOME") or Path.home())
    home = session.temp / "home"
    config = home / ".cursor"
    config.mkdir(parents=True)
    if os.name == "nt":
        session.env["HOME"] = str(real)
        session.env["USERPROFILE"] = str(home)
    else:
        session.env["HOME"] = str(home)
        if real.is_dir():
            for entry in real.iterdir():
                if entry.name != ".cursor":
                    (home / entry.name).symlink_to(entry)

    xdg = os.environ.get("XDG_CONFIG_HOME")
    source = Path(os.environ.get("CURSOR_CONFIG_DIR") or (Path(xdg) / "cursor" if xdg else real / ".cursor"))
    settings = read_object(source / "cli-config.json")
    permissions = settings.get("permissions") if isinstance(settings.get("permissions"), dict) else {}
    deny = [item for item in permissions.get("deny") or [] if isinstance(item, str)]
    deny += cursor_deny(executable_denies(session.args.disallowed_tools, session.readonly, "cursor"))
    settings["permissions"] = {**permissions, "deny": list(dict.fromkeys(deny))}
    if session.args.allowed_tools and not session.readonly:
        settings["permissions"]["allow"] = cursor_allow(session.args.allowed_tools)
    settings.setdefault("version", 1)
    write_private(config / "cli-config.json", settings)
    write_private(config / "hooks.json", {"version": 1, "hooks": {"preToolUse": [
        {"command": deny_hook(session, "cursor"), "failClosed": True}]}})

    if session.servers:
        write_private(config / "mcp.json", {"mcpServers": {
            name: {"url": server.url,
                   **({"headers": {"Authorization": "Bearer ${env:" + server.token + "}"}} if server.token else {})}
            for name, server in session.servers.items()}})
    return home


def hide(session: Session, name: str) -> None:
    path = session.workdir / name
    if not os.path.lexists(path):
        return
    aside = session.temp / "workspace" / name
    aside.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(aside))

    def restore() -> None:
        if os.path.lexists(path):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(aside), str(path))

    session.undo.append(restore)
