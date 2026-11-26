#!/bin/sh
set -eu
[ "$#" = 0 ] && [ "${SSH_ORIGINAL_COMMAND:-}" = diagnostics ] || exit 126
exec /usr/bin/sudo -n "${0%/*}/dispatcher.py"
