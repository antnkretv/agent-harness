#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$(command -v python3 || command -v python)" "$REPO/.harness/ah.py" install >&2

HARNESS_SAY_PREFIX="start: "
# shellcheck source=../.harness/provisioning/lib/shell-common.sh
. "$REPO/.harness/provisioning/lib/shell-common.sh"

"$REPO/.harness/hooks/python.cmd" "$REPO/.harness/lib/agent_links.py" "$REPO" || true

bash "$REPO/.harness/provisioning/scripts/ensure-repo-deps.sh" --repo "$REPO" "$@" \
  || say @cursor_deps_failed >&2

say @cursor_ready
