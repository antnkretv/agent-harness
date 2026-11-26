#!/usr/bin/env bash

. "$(dirname "${BASH_SOURCE[0]}")/messages.sh"

_manifest_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd
}

_manifest_raw() {
  local expr root file out
  expr="$1"
  root="$(_manifest_root)"
  file="$root/.harness/${2:-project/manifest.json}"
  [ -r "$file" ] || return 1

  if command -v jq >/dev/null 2>&1; then
    out="$(jq -r "$expr | if type == \"array\" then .[] else . end // empty" "$file" 2>/dev/null | tr -d '\r')" || out=""
  else
    out="$(harness_python "$root/.harness/lib/manifest.py" "${2:-project/manifest.json}" "$expr" 2>/dev/null | tr -d '\r')" || out=""
  fi

  [ -n "$out" ] || return 1
  printf '%s\n' "$out"
}

manifest_get() {
  local out
  out="$(_manifest_raw "$1" "${3:-project/manifest.json}")" && { printf '%s\n' "$out" | head -n1; return 0; }
  printf '%s\n' "${2-}"
}

manifest_installed() {
  harness_python "$(_manifest_root)/.harness/lib/manifest.py" state/lock.json modules 2>/dev/null | tr -d '\r' | grep -qxF "$1"
}

manifest_list() {
  local out
  out="$(_manifest_raw "$1" "${3:-project/manifest.json}")" && { printf '%s\n' "$out"; return 0; }
  printf '%s\n' ${2-}
}
