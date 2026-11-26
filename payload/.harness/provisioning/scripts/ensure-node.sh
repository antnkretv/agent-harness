#!/usr/bin/env bash
# Ставит Node.js из .node-version, без него - agents.cli.nodeVersion. Версию конвейер берет через
# actions/setup-node. Под Node образа агента ensure-npm.sh не ставит npm из devEngines.
#
#   ensure-node.sh
#
# Где Node образа уже ведет NVM (есть алиас default, так в Codex), версия ставится им и
# становится default: оболочки агента берут Node из алиаса. Свой NVM не вводим:
# иначе - архив nodejs.org в ~/.local/node. На PATH нужный Node кладет toolchain-env.sh.
set -euo pipefail

HARNESS_SAY_PREFIX="node: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"
# shellcheck source=../lib/pinned-binary.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/pinned-binary.sh"

case "${1:-}" in -h|--help) print_header_help "$0"; exit 0 ;; esac
[ $# -eq 0 ] || reject_arg "$1"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [ -f "$ROOT/.node-version" ]; then
  VERSION="$(tr -d '[:space:]' < "$ROOT/.node-version")"
else
  # shellcheck source=../../lib/manifest.sh
  . "$ROOT/.harness/lib/manifest.sh"
  VERSION="$(manifest_get '.agents.cli.nodeVersion' '')"
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die @node_bad_version "$VERSION"

NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ] && [ -e "$NVM_DIR/alias/default" ]; then
  # shellcheck disable=SC1091
  . "$NVM_DIR/nvm.sh" --no-use
  [ -x "$NVM_DIR/versions/node/v$VERSION/bin/node" ] || nvm install "$VERSION"
  nvm alias default "$VERSION"
  exit 0
fi

DIR="$HOME/.local/node"
pinned_binary_platform linux darwin x64 arm64 win
MEMBER=bin/node
[ "$OS" != win ] || MEMBER=node.exe
CI_OUT=0
pinned_binary_present node Node "$DIR/$MEMBER" && exit 0

NAME="node-v$VERSION-$OS-$ARCH"
BASE="https://nodejs.org/dist/v$VERSION"
ARCHIVE="$NAME.tar.xz"
if [ "$OS" = win ]; then
  ARCHIVE="$NAME.zip"
fi

SUM="$(fetch_file "$BASE/SHASUMS256.txt" - | awk -v f="$ARCHIVE" '$2 == f { print $1 }')" || true
[ -n "$SUM" ] || die @tool_download_failed "$BASE/SHASUMS256.txt"
pinned_binary_fetch "$ARCHIVE" "$BASE/$ARCHIVE" "$SUM"

install_pinned_tree "$ARCHIVE" "$NAME" "$DIR" "$MEMBER"
pinned_binary_verify "$DIR/$MEMBER"
