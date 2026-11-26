#!/usr/bin/env bash
# Ставит зависимости репозитория: restore решения .NET, npm по корневому package.json, путь git-хуков
# и с --steps шаги проекта.
# Единственное место этой логики для всех потребителей: SessionStart-хук, setup
# облачных агентов, локальный запуск. Состав берется из манифеста .harness, а не из
# аргументов - свой список у каждого потребителя разъезжался бы.
#
#   ensure-repo-deps.sh [--repo <корень>] [--clean] [--steps | --only-steps]
#
# --steps: затем шаги проекта - команды списка setup.session манифеста по порядку; отказ шага код возврата не меняет.
# --only-steps: только шаги проекта, без установок; отказ шага дает код 1. Так шаги зовет хук
# старта сессии, чтобы показать отказ шага в сессии.
# --clean: npm ci по lock-файлу вместо npm install. Для разовой сборки снапшота
# ci воспроизводимее; в per-session хуке install быстрее и не стирает node_modules.
# Под npm, который не требует devEngines (ensure-npm.sh --check), npm в репозитории
# не работает вовсе, поэтому шаг npm пропускается с отказом.
#
# Код возврата: 0 - зависимости и git-хуки поставлены; 1 - хотя бы одна установка упала.
set -uo pipefail

HARNESS_SAY_PREFIX="repo-deps: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

REPO=""
CLEAN=0
STEPS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)    REPO="${2:?}"; shift ;;
    --clean)   CLEAN=1 ;;
    --steps)   STEPS=1 ;;
    --only-steps) STEPS=only ;;
    -h|--help) print_header_help "$0"; exit 0 ;;
    *) reject_arg "$1" ;;
  esac
  shift
done

[ -n "$REPO" ] || REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO" || die @no_dir "$REPO"

solution=""
apps=""
hooks=""
steps=""
lib="$REPO/.harness/lib/manifest.sh"
if [ -r "$lib" ]; then
  # shellcheck disable=SC1090
  . "$lib"
  solution="$(manifest_get '.modules.backend.extensions.dotnet.solution' '')"
  apps="$(manifest_list '.apps[].path' '')"
  if [ -d .githooks ] && manifest_installed githooks; then hooks="on"; else hooks="off"; fi
  [ "$STEPS" = 0 ] || steps="$(manifest_list '.setup.session' '')"
fi

# shellcheck source=./toolchain-env.sh
. "$REPO/.harness/provisioning/scripts/toolchain-env.sh"

run_steps() {
  local failed=0 step
  while IFS= read -r step; do
    [ -n "$step" ] || continue
    say @repo_deps_step "$step"
    bash -c "$step" </dev/null || { warn @repo_deps_step_failed "$step"; failed=1; }
  done <<<"$steps"
  return "$failed"
}

if [ "$STEPS" = only ]; then
  run_steps
  exit
fi

status=0

if [ "$hooks" = on ]; then
  say @repo_deps_hooks_set
  git config core.hooksPath .githooks || { say_err @check_failed "$(msg repo_deps_hooks_set)"; status=1; }
elif [ "$hooks" = off ] && [ "$(git config --get core.hooksPath)" = .githooks ]; then
  say @repo_deps_hooks_unset
  git config --unset core.hooksPath || { say_err @check_failed "$(msg repo_deps_hooks_unset)"; status=1; }
fi

if [ -n "$solution" ]; then
  say @repo_deps_root_command "dotnet restore $solution"
  dotnet restore "$solution" || { say_err @check_failed "dotnet restore"; status=1; }
else
  say @repo_deps_no_solution
fi

# shellcheck disable=SC2086
for app in $apps; do
  [ -d "$app" ] || { say_err @check_failed "$(msg repo_deps_no_app_dir "$app")"; status=1; }
done

npm_gate="$REPO/.harness/provisioning/scripts/ensure-npm.sh"
if [ ! -f package.json ]; then
  say @repo_deps_no_npm
elif [ -f "$npm_gate" ] && ! bash "$npm_gate" --check; then
  say_err @repo_deps_npm_mismatch
  status=1
elif [ "$CLEAN" = "1" ] && [ -f "package-lock.json" ]; then
  say @repo_deps_root_command "npm ci"
  npm ci --no-audit --no-fund || { say_err @check_failed "npm ci"; status=1; }
else
  say @repo_deps_root_command "npm install"
  npm install --no-audit --no-fund || { say_err @check_failed "npm install"; status=1; }
fi

run_steps || true

exit "$status"
