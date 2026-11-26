#!/usr/bin/env bash

_toolchain_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)"

harness_npm_bin() {
  local prefix="$1"
  prefix="${prefix//$'\r'/}"
  case "${OS:-}:${OSTYPE:-}" in
    Windows_NT:*|*:msys*|*:cygwin*) cygpath -m "$prefix" ;;
    *) printf '%s/bin\n' "$prefix" ;;
  esac
}

harness_npm_fallback_prefix() {
  case "${OS:-}:${OSTYPE:-}" in
    Windows_NT:*|*:msys*|*:cygwin*) printf '%s/.local/bin\n' "$HOME" ;;
    *) printf '%s/.local\n' "$HOME" ;;
  esac
}

_toolchain_prepend() {
  case "$1" in [A-Za-z]:*) set -- "$(cygpath -u "$1")" ;; esac
  local rest="$PATH:" entry updated=""
  while [ -n "$rest" ]; do
    entry="${rest%%:*}"
    rest="${rest#*:}"
    [ "$entry" = "$1" ] || updated="$updated:$entry"
  done
  PATH="$1$updated"
}

harness_ensure_agent_cli() {
  local scripts output status=0
  scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  output="$(bash "$scripts/ensure-agent-cli.sh" "$@")" || status=$?
  printf '%s\n' "$output"
  [ "$status" -eq 0 ] || return "$status"
  # shellcheck source=/dev/null
  . "$scripts/toolchain-env.sh"
  _toolchain_prepend "$(dirname "${output##*$'\n'}")"
  export PATH
}

_toolchain_compute() {
  local version dir solution sdk node_version npm_bin vstest
  # shellcheck source=../../lib/manifest.sh
  . "$_toolchain_root/.harness/lib/manifest.sh"
  [ -d "$HOME/.local/bin" ] && _toolchain_prepend "$HOME/.local/bin"
  version="$( { tr -d '[:space:]' < "$_toolchain_root/.node-version"; } 2>/dev/null || true)"
  [ -n "$version" ] || version="$(manifest_get '.agents.cli.nodeVersion' '')"
  if [ -n "$version" ]; then
    for dir in "${NVM_DIR:-$HOME/.nvm}/versions/node/v$version/bin" "$HOME/.local/node/bin" "$HOME/.local/node"; do
      if [ -x "$dir/node" ] && node_version="$("$dir/node" --version 2>/dev/null)" && [ "$node_version" = "v$version" ]; then
        _toolchain_prepend "$dir"
        break
      fi
    done
  fi
  if command -v npm >/dev/null 2>&1 && npm_bin="$(npm prefix -g)" && npm_bin="$(harness_npm_bin "$npm_bin")"; then
    _toolchain_prepend "$npm_bin"
  fi
  npm_bin="$(harness_npm_bin "$(harness_npm_fallback_prefix)")"
  if [ -x "$npm_bin/npm" ] || [ -x "$npm_bin/npm.cmd" ]; then
    _toolchain_prepend "$npm_bin"
  fi
  if [ -r "$_toolchain_root/.harness/provisioning/lib/dotnet.sh" ]; then
    # shellcheck source=../lib/dotnet.sh
    . "$_toolchain_root/.harness/provisioning/lib/dotnet.sh"
    solution="$(manifest_get '.modules.backend.extensions.dotnet.solution' '')"
    dir="$_toolchain_root"
    [ -z "$solution" ] || dir="$_toolchain_root/$(dirname "$solution")"
    sdk="$(resolve_dotnet_sdk_root "$dir" "${DOTNET_INSTALL_DIR:-$HOME/.dotnet}")"
    [ -z "$sdk" ] || DOTNET_ROOT="$sdk"
    vstest="$(dotnet_vstest_artifacts_base)"
    [ -z "$vstest" ] || VSTEST_ARTIFACTS_BASE_DIRECTORY="$vstest"
  fi
  if [ -n "${DOTNET_ROOT:-}" ]; then
    _toolchain_prepend "$DOTNET_ROOT"
    [ -d "$DOTNET_ROOT/tools" ] && _toolchain_prepend "$DOTNET_ROOT/tools"
  fi
  return 0
}

_toolchain_compute
export PATH
[ -z "${DOTNET_ROOT:-}" ] || export DOTNET_ROOT
[ -z "${VSTEST_ARTIFACTS_BASE_DIRECTORY:-}" ] || export VSTEST_ARTIFACTS_BASE_DIRECTORY

if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  if [ "$#" -gt 1 ]; then
    . "$_toolchain_root/.harness/provisioning/lib/shell-common.sh"
    reject_arg "$2"
  fi
  case "${1:-}" in
    "") ;;
    --export)
      printf 'export PATH=%q\n' "$PATH"
      [ -z "${DOTNET_ROOT:-}" ] || printf 'export DOTNET_ROOT=%q\n' "$DOTNET_ROOT"
      [ -z "${VSTEST_ARTIFACTS_BASE_DIRECTORY:-}" ] \
        || printf 'export VSTEST_ARTIFACTS_BASE_DIRECTORY=%q\n' "$VSTEST_ARTIFACTS_BASE_DIRECTORY"
      ;;
    --ci)
      . "$_toolchain_root/.harness/lib/forge.sh"
      forge_add_path "$PATH" || exit $?
      [ -z "${DOTNET_ROOT:-}" ] || forge_export_env "DOTNET_ROOT=$DOTNET_ROOT" || exit $?
      [ -z "${VSTEST_ARTIFACTS_BASE_DIRECTORY:-}" ] \
        || forge_export_env "VSTEST_ARTIFACTS_BASE_DIRECTORY=$VSTEST_ARTIFACTS_BASE_DIRECTORY" || exit $?
      ;;
    *)
      . "$_toolchain_root/.harness/provisioning/lib/shell-common.sh"
      reject_arg "$1"
      ;;
  esac
fi
unset -f _toolchain_compute
unset _toolchain_root
