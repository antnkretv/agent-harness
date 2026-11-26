#!/usr/bin/env bash
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/../../.." && pwd)"
# shellcheck source=../ci-common.sh
. "$SELF_DIR/../ci-common.sh"

[ $# -gt 0 ] || { say @shell_check_nothing; exit 0; }

say @shell_check_count "$#"

status=0
for script in "$@"; do
  "$BASH" -n "$script" || { say @gate_fail "$script"; status=1; }
done
[ "$status" -eq 0 ] && say @shell_check_syntax_ok

if ! sc="$("$REPO_ROOT/.harness/provisioning/scripts/ensure-shellcheck.sh" | tail -1)"; then
  annotate warning @shell_check_no_shellcheck
  exit "$status"
fi

printf 'shellcheck %s\n' "$("$sc" --version | sed -n 's/^version: //p')"

if ! LC_ALL=C.UTF-8 "$sc" -x -P SCRIPTDIR -S info --include=SC2251 -f gcc "$@"; then
  say @shell_check_lint_failed
  status=1
fi

if LC_ALL=C.UTF-8 "$sc" -x -P SCRIPTDIR -S warning -e SC1090,SC2153 -f gcc "$@"; then
  [ "$status" -eq 0 ] && say @shell_check_lint_ok
else
  say @shell_check_lint_failed
  status=1
fi

exit "$status"
