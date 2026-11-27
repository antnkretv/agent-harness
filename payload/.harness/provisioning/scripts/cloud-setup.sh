#!/usr/bin/env bash
# Setup script окружения облачного агента: ставит toolchain в снапшот окружения.
# Набор шагов задает манифест .harness/project/manifest.json; разделение с SessionStart-хуком
# и сниппет для поля в UI - справочник .harness/docs/environment.md в репозитории комплекта.
set -uo pipefail

HARNESS_SAY_PREFIX="web-setup: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

case "${1:-}" in
  -h|--help) print_header_help "$0"; exit 0 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT" || { say_err @cd_failed "$REPO_ROOT"; exit 1; }
say @web_setup_repo "$REPO_ROOT"

scripts=.harness/provisioning/scripts
solution=""
steps=""
if [ -r .harness/lib/manifest.sh ]; then
  # shellcheck source=../../lib/manifest.sh
  . .harness/lib/manifest.sh
  solution="$(manifest_get '.modules.backend.extensions.dotnet.solution' '')"
  steps="$(manifest_list '.setup.snapshot' '')"
fi

failed=0

if [ -x "$scripts/ensure-vale.sh" ]; then
  say @web_setup_vale
  "$scripts/ensure-vale.sh" >/dev/null || { say_err @check_failed ensure-vale.sh; failed=1; }
fi

if [ -x "$scripts/ensure-shellcheck.sh" ]; then
  say @web_setup_shellcheck
  "$scripts/ensure-shellcheck.sh" >/dev/null || { say_err @check_failed ensure-shellcheck.sh; failed=1; }
fi

if [ -n "$solution" ] && [ -x "$scripts/ensure-dotnet-sdk.sh" ]; then
  say @web_setup_dotnet
  "$scripts/ensure-dotnet-sdk.sh" --repo "$(dirname "$solution")" || { say_err @check_failed ensure-dotnet-sdk.sh; failed=1; }
  say @web_setup_pwsh
  "$scripts/ensure-pwsh.sh" >/dev/null || warn @web_setup_optional_failed ensure-pwsh.sh
fi

if [ -f .node-version ] && [ -x "$scripts/ensure-node.sh" ]; then
  say @web_setup_node
  "$scripts/ensure-node.sh" || { say_err @check_failed ensure-node.sh; failed=1; }
  say @web_setup_npm
  "$scripts/ensure-npm.sh" || { say_err @check_failed ensure-npm.sh; failed=1; }
fi

if [ -x "$scripts/ensure-lsp.sh" ]; then
  say @web_setup_lsp
  "$scripts/ensure-lsp.sh" || warn @web_setup_optional_failed ensure-lsp.sh
fi

"$scripts/ensure-toolchain-profile.sh" || { say_err @check_failed ensure-toolchain-profile.sh; failed=1; }

while IFS= read -r step; do
  [ -n "$step" ] || continue
  say @web_setup_step "$step"
  bash -c "$step" </dev/null || { say_err @check_failed "$step"; failed=1; }
done <<<"$steps"

if [ -x "$scripts/ensure-docker-mcp.sh" ] && have docker; then
  say @web_setup_docker_mcp
  "$scripts/ensure-docker-mcp.sh" >/dev/null || warn @web_setup_optional_failed ensure-docker-mcp.sh
fi

if [ "$failed" = "1" ]; then
  say_err @web_setup_partial
fi

exit "$failed"
