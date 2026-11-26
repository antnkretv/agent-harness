#!/usr/bin/env bash

msg_file="$1"

root="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

# shellcheck source=../ci-common.sh
. "$root/.harness/scripts/ci-common.sh"

project="$(manifest_get '.project.name' '')"
[ -n "$project" ] || die @commit_no_identity

header="$(grep -vE '^[[:space:]]*#' "$msg_file" | grep -vE '^[[:space:]]*$' | head -n1)"

fault="$(harness_python "$root/.harness/lib/commit_text.py" --header "$header" 2>/dev/null | tr -d '\r')"
case "$fault" in
  exempt) exit 3 ;;
  format) die @commit_bad_header "$project" "${header:-$(msg @value_empty)}" ;;
  "long "*)
    if [ "${2-}" = --stored ]; then
      warn @commit_header_long_stored "${fault#long }"
    else
      die @commit_header_long "${fault#long }"
    fi ;;
  ok) ;;
  *) die @commit_header_unchecked "$fault" ;;
esac

prose_gate="$root/.harness/scripts/check/vale-lint.py"
if [ -f "$prose_gate" ]; then
  if ! grep -vE '^[[:space:]]*#' "$msg_file" \
    | harness_python "$prose_gate" --stdin --ext .commit.md --label "$(msg @commit_prose_label)" >&2; then
    echo "" >&2
    die @commit_prose_hint
  fi
fi

counted="$(harness_python "$root/.harness/lib/commit_text.py" "$msg_file" ${2:+"$2"} 2>/dev/null | tr -d '\r')"
case "$counted" in
  [0-9]*" "[0-9]*) ;;
  *) die @commit_body_uncounted "$counted" ;;
esac
body_words="${counted%% *}"
body_cap="${counted##* }"
if [ "$body_words" -gt "$body_cap" ]; then
  die @commit_body_long "$body_words" "$body_cap"
fi

exit 0
