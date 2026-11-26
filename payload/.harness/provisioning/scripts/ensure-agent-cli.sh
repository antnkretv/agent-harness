#!/usr/bin/env bash
# CLI агента CI закрепленной версии из .harness/project/manifest.json (agents.cli): ставится, когда его нет или
# стоит другая версия.
#   ensure-agent-cli.sh claude-code|codex|cursor [--version V] [--force] [--ci]
#
# claude-code и codex ставит npm в префикс npm окружения, cursor - архив выпуска со сверкой sha256
# в ${XDG_DATA_HOME:-$HOME/.local/share}/cursor-agent/versions/<версия> со ссылкой в ~/.local/bin.
# --ci отдает каталог CLI в PATH следующих шагов CI, --force переставляет уже стоящий.
set -euo pipefail

HARNESS_SAY_PREFIX="agent-cli: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"
# shellcheck source=../lib/pinned-binary.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/pinned-binary.sh"
# shellcheck source=./toolchain-env.sh
. "$(dirname "${BASH_SOURCE[0]}")/toolchain-env.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

manifest_value() {
  harness_python "$ROOT/.harness/lib/manifest.py" project/manifest.json ".agents.cli.$1" | tr -d '\r'
}

case "${1:-}" in
  -h|--help) print_header_help "$0"; exit 0 ;;
  claude-code) COMMAND=claude; TITLE="Claude Code"; KEY=claudeVersion; PACKAGE=@anthropic-ai/claude-code ;;
  codex)       COMMAND=codex; TITLE=Codex; KEY=codexVersion; PACKAGE=@openai/codex ;;
  cursor)      COMMAND=cursor-agent; TITLE="Cursor Agent"; KEY=cursorVersion; PACKAGE="" ;;
  "")          usage_die @agent_cli_usage ;;
  *)           reject_arg "$1" ;;
esac
PROVIDER=$1
shift

VERSION="$(manifest_value "$KEY")" || die @agent_cli_no_version "$KEY"
pinned_binary_args "$@"
[ -n "$VERSION" ] || die @agent_cli_no_version "$KEY"
say @version_from "project/manifest.json agents.cli.$KEY" "$VERSION"

ensure_node() {
  have npm && return 0
  bash "$(dirname "${BASH_SOURCE[0]}")/ensure-node.sh"
  # shellcheck source=./toolchain-env.sh
  . "$(dirname "${BASH_SOURCE[0]}")/toolchain-env.sh"
  hash -r 2>/dev/null || true
  require_command npm @agent_cli_need_node
}

if [ -n "$PACKAGE" ]; then
  if [ "$FORCE" -eq 0 ] && pinned_binary_present "$COMMAND" "$TITLE" ""; then
    exit 0
  fi
  ensure_node
  npm_global() { npm install -g --no-fund --no-audit --allow-scripts="$PACKAGE" --prefix "$1" "$PACKAGE@$VERSION"; }
  prefix="$(npm prefix -g | tr -d '\r')"
  if ! npm_global "$prefix"; then
    fallback="$(harness_npm_fallback_prefix)"
    warn @npm_prefix_denied "$prefix" "$fallback"
    prefix="$fallback"
    npm_global "$prefix" || die @agent_cli_install_failed "$PACKAGE@$VERSION"
  fi
  hash -r 2>/dev/null || true
  pinned_binary_verify "$(harness_npm_bin "$prefix")/$COMMAND"
  exit 0
fi

installed_version() {
  [ -x "$1" ] || return 1
  "$1" --version 2>/dev/null | grep -oE '[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}-[0-9a-f]+' | head -n1
}

BIN="${CURSOR_AGENT_BIN_DIR:-$HOME/.local/bin}/cursor-agent"
if [ "$FORCE" -eq 0 ] && pinned_binary_present "$COMMAND" "$TITLE" "$BIN"; then
  exit 0
fi

pinned_binary_platform linux darwin x64 arm64 win32

case "$VERSION/$OS/$ARCH" in
  2026.10.22-27dcf5f/linux/x64)    SUM=85296944e4600a83c4b764f578d1502bf9c416db200d33a593e8f3ecabfdc294 ;;
  2026.10.22-27dcf5f/linux/arm64)  SUM=22a36df9720830668ccd4d707e5190204ab9c8a0efb2f068cc914895f804d3ec ;;
  2026.10.22-27dcf5f/darwin/x64)   SUM=7a3cfbf0fcfb979b6213feb5f9fd10fbc7cd4e51ab28c3b572bb935e0b114961 ;;
  2026.10.22-27dcf5f/darwin/arm64) SUM=2f5b0a3dd5fb951cecffac813867d8a08d810e59837d59fd5f12d69b922398d7 ;;
  *) die @agent_cli_no_checksum "$PROVIDER" "$VERSION" "$OS/$ARCH" ;;
esac

ASSET="agent-cli-package.tar.gz"
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/cursor-agent/versions/$VERSION"
pinned_binary_fetch "$ASSET" "https://downloads.cursor.com/lab/$VERSION/$OS/$ARCH/$ASSET" "$SUM"
install_pinned_tree "$ASSET" dist-package "$DIR" cursor-agent
mkdir -p "$(dirname "$BIN")"
ln -sfn "$DIR/cursor-agent" "$BIN"
pinned_binary_verify "$BIN"
