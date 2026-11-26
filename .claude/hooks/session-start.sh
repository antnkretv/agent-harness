#!/usr/bin/env bash
# Удаленная сессия Claude Code в репозитории комплекта получает инструменты smoke так же, как шаг CI.
# Локальная сессия машину сопровождающего не трогает: там их ставит команда из MAINTAINING.md.
set -uo pipefail

[ "${CLAUDE_CODE_REMOTE:-}" = true ] || exit 0
kit="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if env="$(bash "$kit/tools/smoke-tools.sh")"; then
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    printf '%s\n' "$env" >> "$CLAUDE_ENV_FILE"
  fi
else
  echo "smoke-tools: инструменты smoke не поставлены, kit.py check --adapters их не найдет; журнал выше" >&2
fi
exit 0
