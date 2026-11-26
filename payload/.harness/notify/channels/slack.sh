#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=../../lib/python.sh
. "$(dirname "${BASH_SOURCE[0]}")/../../lib/python.sh"

harness_python --exec "$(dirname "${BASH_SOURCE[0]}")/slack.py"
