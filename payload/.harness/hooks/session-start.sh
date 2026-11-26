#!/bin/bash
set -uo pipefail

# shellcheck source=../lib/messages.sh
. "$(dirname "${BASH_SOURCE[0]}")/../lib/messages.sh"

PLATFORM_SAY_PREFIX='session-start: '

REPO_ROOT="${AGENT_PROJECT_DIR:-}"
if [ -z "$REPO_ROOT" ]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
cd "$REPO_ROOT" || exit 1

harness_python "$(dirname "${BASH_SOURCE[0]}")/../lib/agent_links.py" "$REPO_ROOT" || exit 1

(harness_python .harness/scripts/pipeline/task-worktree.py cleanup </dev/null >/dev/null 2>&1 &)

if [ "${AGENT_REMOTE:-}" != "true" ]; then
  exit 0
fi

solution=""
lib="$REPO_ROOT/.harness/lib/manifest.sh"
if [ -r "$lib" ]; then
  # shellcheck disable=SC1090
  . "$lib"
  solution="$(manifest_get '.modules.backend.extensions.dotnet.solution' '')"
fi

scripts=.harness/provisioning/scripts
status=0
failed=0

common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
log="$common/harness/session-start.log"
if [ -z "$common" ] || ! { mkdir -p "$common/harness" && : > "$log"; } 2>/dev/null; then
  log="$(mktemp)"
fi

run() {
  local title="$1" failure="$2" before out=1
  shift 2
  [ -z "$title" ] || platform_say "$title" >> "$log"
  before="$(wc -l < "$log")"
  "$@" >> "$log" 2>&1 </dev/null && return 0
  failed=1
  local say=("$failure")
  if [ "$failure" = hard ]; then
    status=1
    out=2
    say=(@session_failed "$(basename "$1")")
  fi
  { platform_say "${say[@]}"; tail -n "+$((before + 1))" "$log" | tail -n 15; } >&"$out"
}

if [ -n "$solution" ] && [ -x "$scripts/ensure-dotnet-sdk.sh" ]; then
  run @session_dotnet hard "$scripts/ensure-dotnet-sdk.sh" --repo "$(dirname "$solution")"
  run @session_pwsh @session_pwsh_failed "$scripts/ensure-pwsh.sh"
fi

if [ -x "$scripts/ensure-vale.sh" ]; then
  run @session_vale @session_vale_failed "$scripts/ensure-vale.sh"
fi

if [ -f .node-version ] && [ -x "$scripts/ensure-node.sh" ]; then
  run @session_node @session_node_failed "$scripts/ensure-node.sh"
  run @session_npm @session_npm_failed "$scripts/ensure-npm.sh"
fi

run "" hard "$scripts/ensure-toolchain-profile.sh"

write_session_env() { "$scripts/session-env.sh" --export >> "$AGENT_ENV_FILE"; }
if [ -n "${AGENT_ENV_FILE:-}" ]; then
  run "" @session_env_failed write_session_env
fi

if [ -x "$scripts/ensure-lsp.sh" ]; then
  run @session_lsp @session_lsp_failed "$scripts/ensure-lsp.sh"
fi

run @session_deps hard "$scripts/ensure-repo-deps.sh" --repo "$REPO_ROOT"
run "" @session_step_failed "$scripts/ensure-repo-deps.sh" --repo "$REPO_ROOT" --only-steps

if [ "$status" -ne 0 ]; then
  platform_say @session_log "$log" >&2
elif [ "$failed" -ne 0 ]; then
  platform_say @session_log "$log"
else
  platform_say @session_ready "$log"
fi

exit "$status"
