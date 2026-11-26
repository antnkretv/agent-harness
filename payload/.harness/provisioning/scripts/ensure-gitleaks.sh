#!/usr/bin/env bash
# Скан секретов без Docker: gitleaks закрепленной версии в ~/.local/bin, где его берет check-all.py.
#   ensure-gitleaks.sh [--version 8.30.1] [--force] [--ci]
#
# --ci отдает каталог в PATH следующих шагов CI, --force переставляет уже стоящую.
set -euo pipefail

HARNESS_SAY_PREFIX="gitleaks: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

# shellcheck source=../lib/pinned-binary.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/pinned-binary.sh"

VERSION="${GITLEAKS_VERSION:-8.30.1}"
case "${1:-}" in -h|--help) print_header_help "$0"; exit 0 ;; esac
pinned_binary_args "$@"

BIN="${GITLEAKS_BIN_DIR:-$HOME/.local/bin}/gitleaks"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) BIN="$BIN.exe" ;; esac

if [ "$FORCE" -eq 0 ] && pinned_binary_present gitleaks gitleaks "$BIN"; then
  exit 0
fi

pinned_binary_platform linux darwin x64 arm64 windows

ASSET="gitleaks_${VERSION}_${OS}_${ARCH}.tar.gz"
MEMBER=gitleaks
if [ "$OS" = windows ]; then
  ASSET="gitleaks_${VERSION}_${OS}_${ARCH}.zip"
  MEMBER=gitleaks.exe
fi

case "$ASSET" in
  gitleaks_8.30.1_linux_x64.tar.gz)   SUM=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
  gitleaks_8.30.1_linux_arm64.tar.gz) SUM=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
  gitleaks_8.30.1_darwin_x64.tar.gz)  SUM=dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 ;;
  gitleaks_8.30.1_darwin_arm64.tar.gz) SUM=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
  gitleaks_8.30.1_windows_x64.zip)    SUM=d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e ;;
  gitleaks_8.30.1_windows_arm64.zip)  SUM=b95f5e4f5c425cedca7ee203d9afd29597e692c4924a12ed42f970537c72cc0f ;;
  *) SUM="" ;;
esac

install_pinned_binary gitleaks "$BIN" "$ASSET" \
  "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${ASSET}" "$SUM" \
  "$MEMBER"
