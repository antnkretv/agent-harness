#!/usr/bin/env bash
# PowerShell 7 для хука code-cleanup.ps1 на Linux и macOS. Под Windows его ставят отдельно: в
# системе есть только Windows PowerShell 5.1.
#   ensure-pwsh.sh [--version 7.6.6] [--force] [--ci]
#
# Поле "shell": "powershell" хука в настройках агента называет оболочку этим именем, поэтому рядом
# с pwsh кладется ссылка powershell.
# --ci отдает каталог в PATH следующих шагов CI, --force переставляет уже стоящую версию.
set -euo pipefail

HARNESS_SAY_PREFIX="pwsh: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

# shellcheck source=../lib/pinned-binary.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/pinned-binary.sh"

VERSION="${PWSH_VERSION:-7.6.6}"
case "${1:-}" in -h|--help) print_header_help "$0"; exit 0 ;; esac
pinned_binary_args "$@"

BIN_DIR="${PWSH_BIN_DIR:-$HOME/.local/bin}"
BIN="$BIN_DIR/pwsh"

link_powershell() {
  have powershell || ln -sfn "$1" "$BIN_DIR/powershell"
}

if [ "$FORCE" -eq 0 ] && exe="$(HARNESS_SAY_FD=2 pinned_binary_present pwsh PowerShell "$BIN")"; then
  mkdir -p "$BIN_DIR"
  link_powershell "$exe"
  printf '%s\n' "$exe"
  exit 0
fi

pinned_binary_platform linux osx x64 arm64

ASSET="powershell-${VERSION}-${OS}-${ARCH}.tar.gz"

case "$ASSET" in
  powershell-7.6.6-linux-x64.tar.gz)   SUM=ddbc4a2d113bbd46d283cfedcbcd117a70caefd7673f41f2b4e0000badf103bc ;;
  powershell-7.6.6-linux-arm64.tar.gz) SUM=924829e54c983648f6f1419a2dc7f9433c861b2fb5bd57736ff096c24f133729 ;;
  powershell-7.6.6-osx-x64.tar.gz)     SUM=e325ed9f666894eb39a5ea52800b602da2fb4242bbe9747ceddb39cdc66de805 ;;
  powershell-7.6.6-osx-arm64.tar.gz)   SUM=6df833d094ebac1c1a74340d7b3437f4aaf5e03ce640484a1c4359f3ce8b3db1 ;;
  *) SUM="" ;;
esac

HOME_DIR="${PWSH_HOME:-$HOME/.local/share/powershell}/$VERSION"
pinned_binary_fetch "$ASSET" \
  "https://github.com/PowerShell/PowerShell/releases/download/v${VERSION}/${ASSET}" "$SUM"
install_pinned_tree "$ASSET" "" "$HOME_DIR" pwsh

mkdir -p "$BIN_DIR"
ln -sfn "$HOME_DIR/pwsh" "$BIN"
link_powershell "$BIN"
pinned_binary_verify "$BIN"
