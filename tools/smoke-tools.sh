#!/usr/bin/env bash
# Инструменты smoke комплекта, одни для шага CI и для сессии агента: без них kit.py check --adapters
# падает на шагах prose, shellcheck, secret-scan и сборки фикстуры .NET.
#   tools/smoke-tools.sh            # строки export для файла окружения сессии - в stdout
#   tools/smoke-tools.sh --github   # каталоги - в GITHUB_PATH и GITHUB_ENV следующих шагов CI
# Каталог каждого инструмента берется из вывода его скрипта установки: путь к файлу или DOTNET_ROOT=.
# gitleaks ставится, только когда Docker недоступен: с Docker скан идет через образ, как у проекта GitHub.
set -euo pipefail

kit="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scripts="$kit/payload/.harness/provisioning/scripts"
dirs=()
add() {
  case " ${dirs[*]} " in *" $1 "*) ;; *) dirs+=("$1") ;; esac
}
binary() {
  local found
  found="$(bash "$scripts/ensure-$1.sh" | tail -n 1)"
  add "$(dirname "$found")"
}

binary vale
binary shellcheck
if ! docker info >/dev/null 2>&1; then
  binary gitleaks
fi
dotnet="$(bash "$scripts/ensure-dotnet-sdk.sh" --repo "$kit/tools/fixtures/dotnet/src" --no-runtimes | tail -n 1)"
dotnet="${dotnet#DOTNET_ROOT=}"
add "$dotnet"

if [ "${1:-}" = --github ]; then
  printf '%s\n' "${dirs[@]}" >> "$GITHUB_PATH"
  echo "DOTNET_ROOT=$dotnet" >> "$GITHUB_ENV"
else
  path="$(IFS=:; echo "${dirs[*]}")"
  printf "export PATH=\"%s:\$PATH\"\nexport DOTNET_ROOT=\"%s\"\n" "$path" "$dotnet"
fi
