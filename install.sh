#!/usr/bin/env bash
# Установка agent-harness в репозиторий проекта: скачивает архив выпуска, сверяет sha256 и запускает ah init.
# Выпуск публикует этот файл с именем, версией и адресом комплекта вместо __NAME__, __VERSION__ и __REPOSITORY__:
#   curl -fsSL https://github.com/antnkretv/agent-harness/releases/latest/download/install.sh | bash
set -euo pipefail

name="${AGENT_HARNESS_NAME:-__NAME__}"
version="${AGENT_HARNESS_VERSION:-__VERSION__}"
repository="${AGENT_HARNESS_REPOSITORY:-__REPOSITORY__}"
url="$repository/releases/download/v$version/$name-$version.tar.gz"

fail() {
  printf 'agent-harness: %s\n' "$1" >&2
  exit 2
}

case "${OS:-}:${OSTYPE:-}" in
  Windows_NT:*|*:msys*|*:cygwin*)
    fail "на Windows вопросы установки идут в PowerShell: irm $repository/releases/latest/download/install.ps1 | iex" ;;
esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "запустите команду в рабочей копии git-репозитория проекта"

fetch() {
  curl -fsSL --connect-timeout 30 --retry 3 --speed-limit 1024 --speed-time 60 -o "$2" "$1" || fail "не удалось скачать $1"
}

digest() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d ' ' -f 1
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
archive="$work/$name-$version.tar.gz"
fetch "$url" "$archive"
fetch "$url.sha256" "$archive.sha256"
expected="$(cut -d ' ' -f 1 <"$archive.sha256")"
actual="$(digest "$archive")"
[ "$expected" = "$actual" ] || fail "sha256 архива $actual не совпал с опубликованным $expected"
mkdir "$work/kit"
tar -xzf "$archive" -C "$work/kit"
kit="$(find "$work/kit" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -f "$kit/ah.py" ] || fail "в архиве нет каталога комплекта с ah.py"

python=(bash "$kit/payload/.harness/hooks/python.cmd")
"${python[@]}" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || fail "нужен Python 3.11 или новее: интерпретатор, который выбирает .harness/hooks/python.cmd, не найден или старше"

init=("${python[@]}" -B "$kit/ah.py" init --target "$root" --archive "$archive")
if [ -t 1 ] && { : </dev/tty; } 2>/dev/null; then
  "${init[@]}" </dev/tty
else
  "${init[@]}"
fi
