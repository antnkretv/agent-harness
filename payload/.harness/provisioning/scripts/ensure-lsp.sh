#!/usr/bin/env bash
# Языковые серверы и MCP-мост агентов; серверы и версии объявляет .lsp-mcp.json.
#   ensure-lsp.sh [--force]
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_SAY_PREFIX="lsp: "
# shellcheck source=../lib/shell-common.sh
. "$SCRIPTS/../lib/shell-common.sh"
case "${1:-}" in -h|--help) print_header_help "$0"; exit 0 ;; esac
# shellcheck source=toolchain-env.sh
. "$SCRIPTS/toolchain-env.sh"
require_command node
exec node "$SCRIPTS/lsp-mcp.mjs" --install "$@"
