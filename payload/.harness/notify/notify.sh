#!/usr/bin/env bash
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/messages.sh
. "$SELF/../lib/messages.sh"
# shellcheck source=../lib/forge.sh
. "$SELF/../lib/forge.sh"

for name in NOTIFY_KIND NOTIFY_TITLE NOTIFY_BODY; do
  [ -n "${!name:-}" ] || platform_die @notify_need_var "$name"
done
export NOTIFY_KIND NOTIFY_TITLE NOTIFY_BODY

plan=$(harness_python "$SELF/notify.py" channels "$NOTIFY_KIND")
[ -n "$plan" ] || platform_say @notify_no_channels "$NOTIFY_KIND"

failed=0

while read -r channel policy; do
  [ -n "$channel" ] || continue

  adapter="$SELF/channels/${channel}.sh"
  [ -x "$adapter" ] || platform_die @notify_no_adapter "$channel" "$adapter"

  status=0
  "$adapter" </dev/null || status=$?
  case "$status" in
    0) ;;
    3) platform_say @notify_skipped "$NOTIFY_KIND" "$channel" ;;
    *) if [ "$policy" = fatal ]; then
         forge_annotation error "$(platform_msg @notify_failed "$NOTIFY_KIND" "$channel")" >&2
         failed=1
       else
         forge_annotation warning "$(platform_msg @notify_failed "$NOTIFY_KIND" "$channel")" >&2
       fi ;;
  esac
done <<< "$plan"

exit "$failed"
