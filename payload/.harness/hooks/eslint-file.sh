#!/usr/bin/env bash
set -uo pipefail

HOOK_NAME=eslint-file
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/../lib/hook.sh"

FILE=$(hook_input_file)
PARSE=$?
[ "$PARSE" -eq 0 ] || hook_skip @hook_input_unparsed "$PARSE"
[ -n "$FILE" ] || hook_skip @hook_no_file_path

case "$FILE" in
  *.ts | *.tsx | *.mts | *.cts | *.js | *.jsx | *.mjs | *.cjs) ;;
  *) exit 0 ;;
esac

[ -f "$FILE" ] || hook_skip @hook_no_file "$FILE"

walk_up() {
  local dir="$1" parent
  while :; do
    "$2" "$dir" && { printf '%s' "$dir"; return 0; }
    parent=$(dirname "$dir")
    [ "$parent" = "$dir" ] && return 1
    dir="$parent"
  done
}

is_app() { [ -f "$1/package.json" ] && ls "$1"/eslint.config.* >/dev/null 2>&1; }
is_eslint_root() { [ -f "$1/node_modules/eslint/bin/eslint.js" ]; }

APP=$(walk_up "$(dirname "$FILE")" is_app) \
  || hook_skip @hook_eslint_no_app "$FILE"

ESLINT_ROOT=$(walk_up "$APP" is_eslint_root) \
  || hook_skip @hook_eslint_missing "$APP"

OUT=$(cd "$APP" && node "$ESLINT_ROOT/node_modules/eslint/bin/eslint.js" --fix --no-warn-ignored "$FILE" 2>&1)
STATUS=$?
[ "$STATUS" -eq 0 ] && exit 0
[ "$STATUS" -eq 1 ] || hook_gate_unusable "$STATUS" "$OUT"

hook_block "$OUT" @hook_eslint_block "$FILE"
