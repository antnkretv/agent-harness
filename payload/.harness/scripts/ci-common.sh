#!/usr/bin/env bash

# shellcheck source=messages.env
. "$(dirname "${BASH_SOURCE[0]}")/messages.env"

# shellcheck source=../lib/forge.sh
. "$(dirname "${BASH_SOURCE[0]}")/../lib/forge.sh"

msg() { platform_msg "$@"; }

say() { printf '%s\n' "$(msg "$@")"; }
warn() { platform_warn "$@"; }
die() { platform_die "$@"; }

annotate() {
  local level="$1"
  shift
  forge_annotation "$level" "$(msg "$@")"
}
