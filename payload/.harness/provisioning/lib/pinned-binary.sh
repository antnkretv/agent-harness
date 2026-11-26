#!/usr/bin/env bash

# shellcheck disable=SC2034
pinned_binary_args() {
  FORCE=0
  CI_OUT=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --version) VERSION="${2:?}"; shift ;;
      --force)   FORCE=1 ;;
      --ci)      CI_OUT=1 ;;
      *) reject_arg "$1" ;;
    esac
    shift
  done
}

installed_version() {
  [ -x "$1" ] || return 1
  "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1
}

pinned_binary_platform() {
  case "$(uname -s)" in
    Linux)  OS=$1 ;;
    Darwin) OS=$2 ;;
    MINGW*|MSYS*|CYGWIN*) OS=${5:?} ;;
    *) die @tool_unknown_os "$(uname -s)" ;;
  esac
  local arch
  arch=$(host_arch)
  ARCH=$3
  [ "$arch" = amd64 ] || ARCH=$4
}

pinned_binary_ci_path() { [ "$CI_OUT" -eq 0 ] || ci_path "$1"; }

pinned_binary_present() {
  local candidate
  for candidate in "$3" "$(command -v "$1" 2>/dev/null || true)"; do
    [ -n "$candidate" ] || continue
    if [ "$(installed_version "$candidate" || true)" = "$VERSION" ]; then
      say @tool_present "$2" "$candidate" "$VERSION"
      pinned_binary_ci_path "$(dirname "$candidate")"
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

pinned_binary_fetch() {
  local asset=$1 url=$2 sum=$3 archive got
  PINNED_BINARY_TMP="$(mktemp -d)"
  trap 'rm -rf "$PINNED_BINARY_TMP"' EXIT
  archive="$PINNED_BINARY_TMP/$asset"

  say @tool_downloading "$asset"
  fetch_file "$url" "$archive" || die @tool_download_failed "$url"

  if [ -n "$sum" ]; then
    got="$( { sha256sum "$archive" 2>/dev/null || shasum -a 256 "$archive"; } | awk '{print $1}' )"
    [ "$got" = "$sum" ] || die @tool_checksum_mismatch "$asset" "$got" "$sum"
  else
    say @tool_no_checksum "$asset"
  fi
}

pinned_binary_unpack() {
  case "$1" in
    *.zip) unzip -oq "$PINNED_BINARY_TMP/$1" -d "$2" ;;
    *) tar -xf "$PINNED_BINARY_TMP/$1" -C "$2" ;;
  esac || die @tool_unpack_failed "$1"
}

pinned_binary_verify() {
  local installed
  installed="$(installed_version "$1" || true)"
  [ "$installed" = "$VERSION" ] || die @tool_version_mismatch "$installed" "$VERSION"
  say @tool_installed "$1" "$installed"
  pinned_binary_ci_path "$(dirname "$1")"
  printf '%s\n' "$1"
}

install_pinned_binary() {
  local name=$1 bin=$2 asset=$3 member=$6
  pinned_binary_fetch "$asset" "$4" "$5"
  pinned_binary_unpack "$asset" "$PINNED_BINARY_TMP"
  [ -x "$PINNED_BINARY_TMP/$member" ] || die @tool_not_in_archive "$name"
  mkdir -p "$(dirname "$bin")"
  install -m 0755 "$PINNED_BINARY_TMP/$member" "$bin"
  pinned_binary_verify "$bin"
}

install_pinned_tree() {
  local root="$PINNED_BINARY_TMP/tree"
  local tree="$root${2:+/$2}"
  mkdir -p "$root"
  pinned_binary_unpack "$1" "$root"
  [ -f "$tree/$4" ] || die @tool_not_in_archive "$(basename "$4")"
  rm -rf "$3"
  mkdir -p "$(dirname "$3")"
  mv "$tree" "$3"
  chmod 0755 "$3/$4"
}
