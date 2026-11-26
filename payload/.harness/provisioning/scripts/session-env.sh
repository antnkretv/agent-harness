#!/usr/bin/env bash
# Окружение сессии агента: общий тулчейн, затем расширение проекта agents.envScript.
#   session-env.sh --export                      напечатать команды export
#   session-env.sh --exec <команда> [аргументы]  запустить команду в этом окружении
#
# Отказ расширения называется в stderr: --export печатает общий тулчейн и дает код 1,
# --exec запускает команду с общим тулчейном.
set -uo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPTS/../../.." && pwd)"
HARNESS_SAY_PREFIX="session-env: "
# shellcheck source=../lib/shell-common.sh
. "$SCRIPTS/../lib/shell-common.sh"
# shellcheck source=../../lib/manifest.sh
. "$ROOT/.harness/lib/manifest.sh"

mode="${1:-}"
case "$mode" in
  -h|--help) print_header_help "$0"; exit 0 ;;
  --export) [ $# -eq 1 ] || reject_arg "$2" ;;
  --exec) [ $# -gt 1 ] || usage_die @session_env_no_command ;;
  *) reject_arg "$mode" ;;
esac

base="$(bash "$SCRIPTS/toolchain-env.sh" --export)" || exit 1
eval "$base"

status=0
extra=""
extension="$(manifest_get '.modules.agents.settings.envScript' '')"
if [ -n "$extension" ] && ! extra="$(bash "$ROOT/$extension" --export)"; then
  warn @session_env_extension_failed "$extension"
  extra=""
  status=1
fi
eval "$extra"

if [ "$mode" = --export ]; then
  printf '%s\n' "$base"
  [ -z "$extra" ] || printf '%s\n' "$extra"
  exit "$status"
fi
shift
exec "$@"
