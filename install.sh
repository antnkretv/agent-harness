#!/usr/bin/env bash
# Установка agent-harness в репозиторий проекта: скачивает архив выпуска, сверяет sha256 и запускает ah init.
# Выпуск публикует этот файл с версией вместо __VERSION__:
#   curl -fsSL https://github.com/antnkretv/agent-harness/releases/latest/download/install.sh | bash
set -euo pipefail

version="${AGENT_HARNESS_VERSION:-__VERSION__}"
releases="${AGENT_HARNESS_RELEASES:-https://github.com/antnkretv/agent-harness/releases/download}"
name="agent-harness-$version"

fail() {
  printf 'agent-harness: %s\n' "$1" >&2
  exit 2
}

case "${OS:-}:${OSTYPE:-}" in
  Windows_NT:*|*:msys*|*:cygwin*)
    fail "на Windows вопросы установки идут в PowerShell: irm https://github.com/antnkretv/agent-harness/releases/latest/download/install.ps1 | iex" ;;
esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "запустите команду в рабочей копии git-репозитория проекта"

python=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    python="$candidate"
    break
  fi
done
[ -n "$python" ] || fail "нужен Python 3.11 или новее: python3 не найден или старше"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
archive="$work/$name.tar.gz"
curl -fsSL -o "$archive" "$releases/v$version/$name.tar.gz" || fail "не удалось скачать $releases/v$version/$name.tar.gz"
curl -fsSL -o "$archive.sha256" "$releases/v$version/$name.tar.gz.sha256" || fail "не удалось скачать sha256 архива"
expected="$(cut -d ' ' -f 1 <"$archive.sha256")"
actual="$("$python" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$archive")"
[ "$expected" = "$actual" ] || fail "sha256 архива $actual не совпал с опубликованным $expected"
tar -xzf "$archive" -C "$work"

init=("$python" -B "$work/$name/ah.py" init --target "$root" --archive "$archive")
if [ -t 1 ] && { : </dev/tty; } 2>/dev/null; then
  "${init[@]}" </dev/tty
else
  "${init[@]}"
fi
