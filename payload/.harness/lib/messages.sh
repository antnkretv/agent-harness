#!/usr/bin/env bash

# shellcheck source=../messages.env
. "$(dirname "${BASH_SOURCE[0]}")/../messages.env"
if [ -r "${BASH_SOURCE[0]%/*}/../project/messages.env" ]; then
  # shellcheck source=/dev/null
  . "${BASH_SOURCE[0]%/*}/../project/messages.env"
fi

# shellcheck source=python.sh
. "$(dirname "${BASH_SOURCE[0]}")/python.sh"

platform_msg() {
  local key="${1#@}" ref
  ref="MSG_$key"
  shift
  if [ -z "${!ref+set}" ]; then
    printf '@%s' "$key"
    [ $# -eq 0 ] || printf ' %s' "$@"
    return 0
  fi
  # shellcheck disable=SC2059
  printf -- "${!ref}" "$@"
}

platform_say() { printf '%s%s\n' "${PLATFORM_SAY_PREFIX:-}" "$(platform_msg "$@")"; }
platform_warn() { printf '%s\n' "$(platform_msg "$@")" >&2; }
platform_die() { platform_warn "$@"; exit 1; }
