#!/usr/bin/env bash
# shellcheck source=shell-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/shell-common.sh"

is_dotnet_root() {
  [ -n "${1:-}" ] && [ -x "$1/dotnet" ] && [ -d "$1/shared/Microsoft.NETCore.App" ]
}

dotnet_version_from_global_json() {
  local file="${1:-}"
  [ -f "$file" ] || return 1
  sed -nE 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$file" | head -1
}

dotnet_sdk_suitable() {
  is_dotnet_root "$1" || return 1
  ( cd "$2" && "$1/dotnet" --version >/dev/null 2>&1 )
}

resolve_dotnet_sdk_root() {
  local directory="$1" install_dir="$2" candidate
  for candidate in "$install_dir" "${DOTNET_ROOT:-}" /usr/share/dotnet /usr/lib/dotnet; do
    [ -n "$candidate" ] || continue
    if dotnet_sdk_suitable "$candidate" "$directory"; then printf '%s' "$candidate"; return 0; fi
  done
  if have dotnet; then
    candidate="$(dirname "$(readlink -f "$(command -v dotnet)")")"
    if dotnet_sdk_suitable "$candidate" "$directory"; then printf '%s' "$candidate"; return 0; fi
  fi
  return 0
}

dotnet_vstest_artifacts_base() {
  local dir="${RUNNER_TEMP:-${XDG_CACHE_HOME:-$HOME/.cache}}/vstest"
  [ -z "${VSTEST_ARTIFACTS_BASE_DIRECTORY:-}" ] && [ "$(uname -s)" = Linux ] || return 0
  [ ! -d "/run/user/$(id -u)" ] || return 0
  { mkdir -p "$dir" && chmod 700 "$dir"; } 2>/dev/null && printf '%s' "$dir"
  return 0
}
