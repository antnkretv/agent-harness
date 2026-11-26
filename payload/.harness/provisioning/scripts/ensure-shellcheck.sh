#!/usr/bin/env bash
# Закрепленная версия нужна для одинакового вердикта локально и в CI.
#   ensure-shellcheck.sh [--version 0.9.0] [--force] [--ci]
#
# --ci отдает каталог в PATH следующих шагов CI, --force переставляет уже стоящую.
set -euo pipefail

HARNESS_SAY_PREFIX="shellcheck: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

# shellcheck source=../lib/pinned-binary.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/pinned-binary.sh"

VERSION="${SHELLCHECK_VERSION:-0.9.0}"
case "${1:-}" in -h|--help) print_header_help "$0"; exit 0 ;; esac
pinned_binary_args "$@"

BIN="${SHELLCHECK_BIN_DIR:-$HOME/.local/bin}/shellcheck"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) BIN="$BIN.exe" ;; esac

if [ "$FORCE" -eq 0 ] && pinned_binary_present shellcheck shellcheck "$BIN"; then
  exit 0
fi

pinned_binary_platform linux darwin x86_64 aarch64 windows

ASSET="shellcheck-v${VERSION}.${OS}.${ARCH}.tar.xz"
MEMBER="shellcheck-v${VERSION}/shellcheck"
if [ "$OS" = windows ]; then
  ASSET="shellcheck-v${VERSION}.zip"
  MEMBER=shellcheck.exe
fi

case "$ASSET" in
  shellcheck-v0.9.0.linux.x86_64.tar.xz)  SUM=700324c6dd0ebea0117591c6cc9d7350d9c7c5c287acbad7630fa17b1d4d9e2f ;;
  shellcheck-v0.9.0.linux.aarch64.tar.xz) SUM=179c579ef3481317d130adebede74a34dbbc2df961a70916dd4039ebf0735fae ;;
  shellcheck-v0.9.0.darwin.x86_64.tar.xz) SUM=7d3730694707605d6e60cec4efcb79a0632d61babc035aa16cda1b897536acf5 ;;
  shellcheck-v0.9.0.zip) SUM=ae58191b1ea4ffd9e5b15da9134146e636440302ce3e2f46863e8d71c8be1bbb ;;
  *) SUM="" ;;
esac

install_pinned_binary shellcheck "$BIN" "$ASSET" \
  "https://github.com/koalaman/shellcheck/releases/download/v${VERSION}/${ASSET}" "$SUM" \
  "$MEMBER"
