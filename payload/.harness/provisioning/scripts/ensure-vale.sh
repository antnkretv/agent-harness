#!/usr/bin/env bash
# Закрепленная версия нужна для одинакового вердикта локально и в CI.
#   ensure-vale.sh [--version 3.13.0] [--force] [--ci]
#
# --ci отдает каталог в PATH следующих шагов CI, --force переставляет
# уже установленную версию.

set -euo pipefail

HARNESS_SAY_PREFIX="vale: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

# shellcheck source=../lib/pinned-binary.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/pinned-binary.sh"

VERSION="${VALE_VERSION:-3.13.0}"
case "${1:-}" in -h|--help) print_header_help "$0"; exit 0 ;; esac
pinned_binary_args "$@"

BIN="${VALE_BIN_DIR:-$HOME/.local/bin}/vale"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) BIN="$BIN.exe" ;; esac

if [ "$FORCE" -eq 0 ] && pinned_binary_present vale Vale "$BIN"; then
  exit 0
fi

pinned_binary_platform Linux macOS 64-bit arm64 Windows

ASSET="vale_${VERSION}_${OS}_${ARCH}.tar.gz"
MEMBER=vale
if [ "$OS" = Windows ]; then
  ASSET="vale_${VERSION}_${OS}_${ARCH}.zip"
  MEMBER=vale.exe
fi

case "$ASSET" in
  vale_3.13.0_Linux_64-bit.tar.gz)  SUM=4378ee4bc7c2493760826270e55d5569cda35d7c89582e9fdc3070e2a1089193 ;;
  vale_3.13.0_Linux_arm64.tar.gz)   SUM=2134f23e7afbdf70b44272e6d3b5f26e85972340faa1e2a2b194358cf2892d84 ;;
  vale_3.13.0_macOS_64-bit.tar.gz)  SUM=9f2991092579e85dd5be082c691b7b14ddbcd7c65477a6ff44b5f5e8dc3a9079 ;;
  vale_3.13.0_macOS_arm64.tar.gz)   SUM=2e89bd82cadfffa6abebda80a141529db2799df5d4197e6aa0489a4d711d8a3b ;;
  vale_3.13.0_Windows_64-bit.zip)   SUM=fb1141183d783ef1b9278ea5b1cc04e85801256b1539fc47147b90f6bf082341 ;;
  *) SUM="" ;;
esac

install_pinned_binary vale "$BIN" "$ASSET" \
  "https://github.com/errata-ai/vale/releases/download/v${VERSION}/${ASSET}" "$SUM" "$MEMBER"
