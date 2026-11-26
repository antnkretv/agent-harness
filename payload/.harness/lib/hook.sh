#!/usr/bin/env bash

# shellcheck source=messages.sh
. "$(dirname "${BASH_SOURCE[0]}")/messages.sh"

hook_root() {
  local root="${AGENT_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"
  [ -n "$root" ] || root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
  printf '%s' "$root"
}

hook_input_file() {
  harness_python -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(3)
value = data.get('tool_input', {}).get('file_path')
sys.stdout.write(str(value or ''))
"
}

hook_skip() {
  platform_warn @hook_skip "$HOOK_NAME" "$(platform_msg "$@")"
  exit 0
}

hook_gate_unusable() {
  platform_warn @hook_gate_unusable "$HOOK_NAME" "$1"
  printf '%s\n' "$2" >&2
  exit 0
}

hook_block() {
  local out="$1"
  shift
  platform_warn "$@"
  printf '\n%s\n' "$out" >&2
  exit 2
}
