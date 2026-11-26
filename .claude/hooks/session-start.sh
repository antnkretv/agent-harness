#!/usr/bin/env bash
# Сессия Claude Code в репозитории комплекта получает инструменты smoke так же, как шаг CI.
set -uo pipefail

kit="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if env="$(bash "$kit/tools/smoke-tools.sh")"; then
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    printf '%s\n' "$env" >> "$CLAUDE_ENV_FILE"
  fi
else
  echo "smoke-tools: инструменты smoke не поставлены, kit.py check --adapters их не найдет; журнал выше" >&2
fi
exit 0
