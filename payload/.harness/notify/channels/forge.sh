#!/usr/bin/env bash
set -euo pipefail

[ -n "${NOTIFY_FORGE_CHANGE:-}" ] || exit 3

# shellcheck source=../../lib/python.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../lib/python.sh"

harness_python --exec "$(dirname "${BASH_SOURCE[0]}")/forge.py"
