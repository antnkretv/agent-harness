#!/usr/bin/env bash
set -uo pipefail

repo="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [ "${OS:-}" = "Windows_NT" ]; then
  repo="$(cygpath -u "$repo")"
fi

export AGENT_PROJECT_DIR="$repo"
export AGENT_REMOTE="${CLAUDE_CODE_REMOTE:-false}"
export AGENT_ENV_FILE="${CLAUDE_ENV_FILE:-}"
"$(command -v python3 || command -v python)" "$repo/.harness/ah.py" install >&2
exec bash "$repo/.harness/hooks/session-start.sh"
