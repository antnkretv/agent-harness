#!/usr/bin/env bash

harness_settings() {
  local file line key
  for file in "${BASH_SOURCE[0]%/*}/../settings.env" "${BASH_SOURCE[0]%/*}/../settings.defaults.env"; do
    [ -r "$file" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%$'\r'}"
      key="${line%%=*}"
      [[ "$line" == *=?* && "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      [ -z "${!key:-}" ] || continue
      export "$key=${line#*=}"
    done <"$file"
  done
}

[ -n "${_HARNESS_SETTINGS:-}" ] || { _HARNESS_SETTINGS=1; harness_settings; }

harness_python() {
  local PYTHONUTF8=1 PYTHONIOENCODING=utf-8
  export PYTHONUTF8 PYTHONIOENCODING
  local runner='command'
  if [ "${1:-}" = --exec ]; then runner='exec'; shift; fi
  if [ -n "${HARNESS_PYTHON:-}" ]; then
    "$runner" "$HARNESS_PYTHON" "$@"
    return $?
  fi
  case "${OS:-}:${OSTYPE:-}" in
    Windows_NT:*|*:msys*|*:cygwin*)
      if command -v py >/dev/null 2>&1; then "$runner" py -3 "$@"; return $?; fi
      local found
      found=$(type -ap python 2>/dev/null | grep -vi '/WindowsApps/' | head -n 1 || true)
      "$runner" "${found:-python}" "$@" ;;
    *) "$runner" python3 "$@" ;;
  esac
}
