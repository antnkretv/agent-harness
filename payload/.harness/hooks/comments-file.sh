#!/usr/bin/env bash
set -uo pipefail

HOOK_NAME=comments-file
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/../lib/hook.sh"

FILE=$(hook_input_file)
PARSE=$?
[ "$PARSE" -eq 0 ] || hook_skip @hook_input_unparsed "$PARSE"
[ -n "$FILE" ] || hook_skip @hook_no_file_path

ROOT="$(hook_root)"
[ -n "$ROOT" ] || hook_skip @hook_no_root

GATE="$ROOT/.harness/scripts/check/check-comments.py"
[ -f "$GATE" ] || hook_skip @hook_no_gate "$GATE"
[ -f "$FILE" ] || hook_skip @hook_no_file "$FILE"

OUT=$(cd "$ROOT" && harness_python "$GATE" --files "$FILE" 2>&1)
STATUS=$?
[ "$STATUS" -eq 0 ] && exit 0
[ "$STATUS" -eq 1 ] || hook_gate_unusable "$STATUS" "$OUT"

hook_block "$OUT" @hook_comments_block "$FILE"
