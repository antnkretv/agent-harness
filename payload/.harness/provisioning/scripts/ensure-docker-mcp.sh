#!/usr/bin/env bash
# Плагин docker mcp для сервера MCP mcp/docker: закрепленная версия в каталоге плагинов Docker CLI.
#   ensure-docker-mcp.sh [--version 0.42.2] [--force] [--wait <секунды>]
#
# Плагин, который уже отвечает, не переставляется: Docker Desktop несет свой. --force ставит
# закрепленную версию поверх. --wait ждет команду docker и ответ демона: шлюз без демона
# не стартует, а удаленная сессия ставит и поднимает Docker позже, чем клиент MCP запускает серверы.
# При отвечающем демоне и без единого каталога серверов загружается каталог Docker: загрузка дольше
# DOCKER_MCP_CATALOG_WAIT секунд прерывается, и шлюз стартует без каталога, а не снимается клиентом.
set -euo pipefail

HARNESS_SAY_PREFIX="docker-mcp: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

# shellcheck source=../lib/pinned-binary.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/pinned-binary.sh"

VERSION="${DOCKER_MCP_VERSION:-0.42.2}"
CATALOG="mcp/docker-mcp-catalog:latest"
CATALOG_WAIT="${DOCKER_MCP_CATALOG_WAIT:-45}"
WAIT=0
rest=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) print_header_help "$0"; exit 0 ;;
    --wait) WAIT="${2:?}"; shift ;;
    *) rest+=("$1") ;;
  esac
  shift
done
pinned_binary_args ${rest[@]+"${rest[@]}"}

BIN="${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins/docker-mcp"

deadline=$(( $(date +%s) + WAIT ))
await() { wait_until "$(( deadline - $(date +%s) ))" "$@"; }
finished() { ! kill -0 "$1" 2>/dev/null; }

await have docker || require_command docker

found=""
[ "$FORCE" -eq 1 ] || found="$(docker mcp version 2>/dev/null | head -n1 || true)"
if [ -n "$found" ]; then
  say @docker_mcp_present "$found"
else
  pinned_binary_platform linux darwin amd64 arm64
  ASSET="docker-mcp-${OS}-${ARCH}.tar.gz"
  case "$VERSION:$ASSET" in
    0.42.2:docker-mcp-linux-amd64.tar.gz)  SUM=c646ef93e7edcbe02844c0b2318c279757a79296ff72fc2c2e98ea1fa74ba81b ;;
    0.42.2:docker-mcp-linux-arm64.tar.gz)  SUM=373bb081460e63f389e3aa03bf1c63ad14a96d647b5ca7bf0bb29d3e942d6db5 ;;
    0.42.2:docker-mcp-darwin-amd64.tar.gz) SUM=3e1bcf2a8fa834634e60111792f4a8e87a3762ca637e0e24089eb3a8aeded606 ;;
    0.42.2:docker-mcp-darwin-arm64.tar.gz) SUM=06c3328d7247dec33166364cea8ce157a720f7be91599244da25c1692ea8bc39 ;;
    *) SUM="" ;;
  esac
  install_pinned_binary docker-mcp "$BIN" "$ASSET" \
    "https://github.com/docker/mcp-gateway/releases/download/v${VERSION}/${ASSET}" "$SUM" \
    docker-mcp
fi

if ! await docker info; then
  [ "$WAIT" -gt 0 ] || exit 0
  die @docker_mcp_no_daemon "$WAIT"
fi

if [ "$(docker mcp catalog ls --format json 2>/dev/null | tr -d '[:space:]')" = "[]" ]; then
  say @docker_mcp_catalog "$CATALOG"
  set -m
  docker mcp catalog pull "$CATALOG" >/dev/null 2>&1 &
  pull=$!
  set +m
  if ! wait_every "$CATALOG_WAIT" 1 finished "$pull"; then
    kill -- "-$pull" 2>/dev/null || true
  fi
  wait "$pull" 2>/dev/null || warn @docker_mcp_no_catalog "$CATALOG"
fi
