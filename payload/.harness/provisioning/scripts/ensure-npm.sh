#!/usr/bin/env bash
# Выравнивает npm с тем, что требует devEngines.packageManager корневого package.json.
#
#   ensure-npm.sh [--check]
#
# --check: ничего не ставит и молчит, отдает 0, когда npm в окружении подходит.
# Так ensure-repo-deps.sh узнает, можно ли ему install, и требование не заводит
# второй копии.
#
# Зачем шаг вообще нужен. Соседние major npm по-разному раскладывают в локе
# необязательные peer-зависимости, и чей major стоит в окружении, тот лок и
# переписывает при install. Поэтому major один на все среды, а под чужим npm сам
# npm отказывается работать в репозитории (devEngines, onFail: error). Setup-node
# приносит npm, вшитый в Node, - этот шаг меняет его на требуемый.
#
# Код возврата: 0 - npm подходит; 1 - не подошел и поставить не удалось.
set -uo pipefail

HARNESS_SAY_PREFIX="npm: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check)   CHECK=1 ;;
    -h|--help) print_header_help "$0"; exit 0 ;;
    *) reject_arg "$1" ;;
  esac
  shift
done

major_of() {
  case "${1:-}" in
    [0-9]*) printf '%s\n' "${1%%.*}" ;;
    *)      printf '0\n' ;;
  esac
}

# shellcheck source=./toolchain-env.sh
. "$(dirname "${BASH_SOURCE[0]}")/toolchain-env.sh"

installed=""
have npm && installed="$(npm --version 2>/dev/null)"
if [ -z "$installed" ]; then
  [ "$CHECK" = "1" ] && exit 1
  die @npm_no_node
fi

RANGE="$(node -p 'const p = require(process.argv[1]); (p.devEngines && p.devEngines.packageManager && p.devEngines.packageManager.version) || ""' \
  "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/package.json")"
if [ -z "$RANGE" ]; then
  [ "$CHECK" = "1" ] || say @npm_no_engines "$installed"
  exit 0
fi
MAJOR="$(major_of "${RANGE#^}")"

if [ "$(major_of "$installed")" = "$MAJOR" ]; then
  [ "$CHECK" = "1" ] || say @npm_ok "$installed"
  exit 0
fi

if [ "$CHECK" = "1" ]; then
  exit 1
fi

say @npm_wrong_major "$installed" "$RANGE"

prefix="$(cd "$(dirname "$(command -v npm)")/.." && pwd)"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) prefix="$(dirname "$(command -v npm)")" ;; esac
if ! npm install -g --no-audit --no-fund --prefix "$prefix" "npm@$RANGE"; then
  fallback="$(harness_npm_fallback_prefix)"
  warn @npm_prefix_denied "$prefix" "$fallback"
  prefix="$fallback"
  npm install -g --no-audit --no-fund --prefix "$prefix" "npm@$RANGE" \
    || die @npm_install_failed "$RANGE"
fi

_toolchain_prepend "$(harness_npm_bin "$prefix")"
export PATH
hash -r 2>/dev/null || true
installed="$(npm --version 2>/dev/null || true)"
[ "$(major_of "$installed")" = "$MAJOR" ] \
  || die @npm_path_mismatch "${installed:-$(msg none_value)}" "$RANGE"
say @tool_installed "$(command -v npm)" "$installed"
