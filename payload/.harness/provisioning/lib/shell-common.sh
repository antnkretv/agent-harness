#!/usr/bin/env bash

[ -z "${_HARNESS_SHELL_COMMON:-}" ] || return 0
_HARNESS_SHELL_COMMON=1

# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/../messages.env"
# shellcheck source=../../lib/forge.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../lib/forge.sh"

msg() { platform_msg "$@"; }

msg_text() {
  case "${1:-}" in
    @*) msg "$@" ;;
    *) printf '%s' "$*" ;;
  esac
}

say() {
  [ "${QUIET:-0}" = "1" ] || printf '%s%s\n' "${HARNESS_SAY_PREFIX:-}" "$(msg_text "$@")" >&"${HARNESS_SAY_FD:-1}"
}
say_err() { printf '%s%s\n' "${HARNESS_SAY_PREFIX:-}" "$(msg_text "$@")" >&2; }
warn() { printf '%s%s: %s\n' "${HARNESS_SAY_PREFIX:-}" "$(msg label_warn)" "$(msg_text "$@")" >&2; }
die() { printf '%s%s\n' "${HARNESS_SAY_PREFIX:-}" "$(msg_text "$@")" >&2; exit 1; }
usage_die() { printf '%s%s\n' "${HARNESS_SAY_PREFIX:-}" "$(msg_text "$@")" >&2; exit 2; }
reject_arg() { usage_die @unknown_arg "$1"; }
require_env() { local v; for v; do [ -n "${!v:-}" ] || die @need_var "$v"; done; }
have() { command -v "$1" >/dev/null 2>&1; }
require_command() {
  local command=$1
  shift
  have "$command" && return 0
  [ $# -gt 0 ] || die @no_command "$command"
  die @no_command_for "$command" "$(msg_text "$@")"
}
require_root() { [ "$(id -u)" -eq 0 ] || die @need_root; }
ci_env() { local pair; for pair; do forge_export_env "$pair" || die @not_in_ci; done; }
ci_path() { forge_add_path "$@"; }
fetch_file() { curl -fsSL --retry 8 --retry-all-errors --retry-max-time 300 --connect-timeout 30 "$1" -o "$2"; }

host_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf amd64 ;;
    aarch64|arm64) printf arm64 ;;
    *) die @tool_unknown_arch "$(uname -m)" ;;
  esac
}

check_fail() { say_err @check_failed "$(msg_text "$@")"; FAIL=$((FAIL + 1)); }
check_verdict() { [ "$FAIL" -eq 0 ] || die @check_mismatches "$FAIL"; say "$@"; }

wait_every() {
  local deadline=$(( $(date +%s) + $1 )) pause=$2 left
  shift 2
  until "$@" >/dev/null 2>&1; do
    left=$(( deadline - $(date +%s) ))
    [ "$left" -gt 0 ] || return 1
    sleep "$(( left < pause ? left : pause ))"
  done
  return 0
}

wait_until() { local timeout=$1; shift; wait_every "$timeout" 2 "$@"; }


print_header_help() {
  awk 'NR==1 { next }
       /^#/  { sub(/^# ?/, ""); print; next }
       { exit }' "${1:?}"
}
