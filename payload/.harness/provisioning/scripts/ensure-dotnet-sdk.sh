#!/usr/bin/env bash
# SDK должен удовлетворять `global.json`. Рантаймы устанавливаются отдельно для
# каждой мажорной версии TargetFramework: SDK новой версии их не включает.
#   ensure-dotnet-sdk.sh [--repo <каталог решения>] [--version 11.0.100-...]
#                        [--channel STS|LTS|preview] [--install-dir ~/.dotnet]
#                        [--runtimes "11.0"] [--no-runtimes]
#                        [--ci] [--force]
#
# Код возврата: 0 - пригодный SDK на месте (стоял или поставлен), 1 - нет.

set -euo pipefail

HARNESS_SAY_PREFIX="dotnet-sdk: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"
# shellcheck source=../lib/dotnet.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/dotnet.sh"

REPO="${HARNESS_REPO:-}"
VERSION=""
CHANNEL=""
INSTALL_DIR="${DOTNET_INSTALL_DIR:-$HOME/.dotnet}"
CI_OUT=0
FORCE=0
RUNTIMES=""
WITH_RUNTIMES=1
INSTALL_URL="${DOTNET_INSTALL_URL:-https://dot.net/v1/dotnet-install.sh}"
WINDOWS=0
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    WINDOWS=1
    INSTALL_URL="${DOTNET_INSTALL_URL:-https://dot.net/v1/dotnet-install.ps1}"
    ;;
esac
SIG_URL="${DOTNET_INSTALL_SIG_URL:-${INSTALL_URL%.sh}.sig}"
KEY_URL="${DOTNET_INSTALL_KEY_URL:-${INSTALL_URL%.sh}.asc}"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)         REPO="${2:?}"; shift ;;
    --version)      VERSION="${2:?}"; shift ;;
    --channel)      CHANNEL="${2:?}"; shift ;;
    --install-dir)  INSTALL_DIR="${2:?}"; shift ;;
    --runtimes)     RUNTIMES="${2:?}"; shift ;;
    --no-runtimes)  WITH_RUNTIMES=0 ;;
    --ci)           CI_OUT=1 ;;
    --force)        FORCE=1 ;;
    -h|--help)      print_header_help "$0"; exit 0 ;;
    *) reject_arg "$1" ;;
  esac
  shift
done


GLOBAL_JSON=""
if [ -n "$REPO" ] && [ -f "$REPO/global.json" ]; then
  GLOBAL_JSON="$REPO/global.json"
elif [ -f "global.json" ]; then
  GLOBAL_JSON="global.json"
fi

if [ -z "$VERSION" ] && [ -z "$CHANNEL" ]; then
  if [ -n "$GLOBAL_JSON" ]; then
    VERSION="$(dotnet_version_from_global_json "$GLOBAL_JSON")"
    [ -n "$VERSION" ] && say @version_from "$GLOBAL_JSON" "$VERSION"
  fi
  if [ -z "$VERSION" ]; then
    CHANNEL="preview"
    warn @dotnet_sdk_no_global_json
  fi
fi


sdk_is_suitable() {
  dotnet_sdk_suitable "$1" "$(dirname "${GLOBAL_JSON:-$PWD/global.json}")"
}

resolve_existing_root() {
  resolve_dotnet_sdk_root "$(dirname "${GLOBAL_JSON:-$PWD/global.json}")" "$INSTALL_DIR"
}

installer_dir="$(mktemp -d)"
script="$installer_dir/dotnet-install.sh"
[ "$WINDOWS" -eq 0 ] || script="$installer_dir/dotnet-install.ps1"
trap 'rm -rf "$installer_dir"' EXIT

DOTNET_INSTALL_KEY_FPR="${DOTNET_INSTALL_KEY_FPR:-2B930AB1228D11D5D7F6B6ACB9CF1A51FC7D3ACF}"

verify_installer() {
  local home ok=0 required=0 shell=pwsh
  if [ "$WINDOWS" -eq 1 ]; then
    have pwsh || shell=powershell
    HARNESS_DOTNET_INSTALLER="$(cygpath -w "$script")" "$shell" -NoProfile -Command '
      $signature = Get-AuthenticodeSignature -LiteralPath $env:HARNESS_DOTNET_INSTALLER
      if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.GetNameInfo("SimpleName", $false) -ne "Microsoft Corporation") { exit 1 }
    ' || die @dotnet_sdk_bad_signature "$INSTALL_URL"
    say @dotnet_sdk_signature_ok
    [ -n "${DOTNET_INSTALL_SIG_URL:-}${DOTNET_INSTALL_KEY_URL:-}" ] || return 0
    required=1
  fi
  have gpg || {
    [ "$required" -eq 0 ] || die @dotnet_sdk_bad_signature "$INSTALL_URL"
    warn @dotnet_sdk_no_gpg
    return 0
  }

  home="$(mktemp -d)"
  chmod 700 "$home"
  if ! fetch_file "$SIG_URL" "$home/installer.sig" || ! fetch_file "$KEY_URL" "$home/installer.asc"; then
    rm -rf "$home"
    [ "$required" -eq 0 ] || die @dotnet_sdk_bad_signature "$INSTALL_URL"
    warn @dotnet_sdk_no_signature "$SIG_URL"
    return 0
  fi

  gpg --homedir "$home" --batch --quiet --import "$home/installer.asc" 2>/dev/null \
    && gpg --homedir "$home" --batch --with-colons --fingerprint 2>/dev/null \
       | grep -q "^fpr:*${DOTNET_INSTALL_KEY_FPR}:" \
    || { rm -rf "$home"; die @dotnet_sdk_wrong_key "$DOTNET_INSTALL_KEY_FPR"; }

  gpg --homedir "$home" --batch --quiet --verify "$home/installer.sig" "$script" 2>/dev/null && ok=1
  rm -rf "$home"
  [ "$ok" = "1" ] || die @dotnet_sdk_bad_signature "$INSTALL_URL"
  say @dotnet_sdk_signature_ok
}

fetch_installer() {
  [ -s "$script" ] && return 0
  fetch_file "$INSTALL_URL" "$script" || die @dotnet_sdk_download_failed "$INSTALL_URL"
  verify_installer
  chmod +x "$script"
}

run_installer() {
  if [ "$WINDOWS" -eq 0 ]; then
    "$script" "$@"
    return
  fi
  local args=() shell=pwsh
  have pwsh || shell=powershell
  while [ $# -gt 0 ]; do
    case "$1" in
      --version) args+=(-Version "$2"); shift ;;
      --channel) args+=(-Channel "$2"); shift ;;
      --quality) args+=(-Quality "$2"); shift ;;
      --runtime) args+=(-Runtime "$2"); shift ;;
      --install-dir) args+=(-InstallDir "$(cygpath -w "$2")"); shift ;;
      --no-path) args+=(-NoPath) ;;
      *) reject_arg "$1" ;;
    esac
    shift
  done
  "$shell" -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$script")" "${args[@]}"
}

existing=""
if [ "$FORCE" = "0" ]; then
  existing="$(resolve_existing_root)"
fi

if [ -n "$existing" ]; then
  say @dotnet_sdk_present "$existing" "$( (cd "$(dirname "${GLOBAL_JSON:-.}")" && "$existing/dotnet" --version) 2>/dev/null )"
  DOTNET_ROOT_RESOLVED="$existing"
else
  fetch_installer

  if [ -n "$VERSION" ]; then
    say @dotnet_sdk_installing "$VERSION" "$INSTALL_DIR"
    run_installer --version "$VERSION" --install-dir "$INSTALL_DIR" --no-path
  else
    say @dotnet_sdk_installing_channel "${CHANNEL:-preview}" "$INSTALL_DIR"
    run_installer --channel "${CHANNEL:-preview}" --quality preview --install-dir "$INSTALL_DIR" --no-path
  fi

  sdk_is_suitable "$INSTALL_DIR" \
    || die @dotnet_sdk_still_bad "$INSTALL_DIR" "${GLOBAL_JSON:-global.json}"

  DOTNET_ROOT_RESOLVED="$INSTALL_DIR"
  say @dotnet_sdk_installed "$( (cd "$(dirname "${GLOBAL_JSON:-.}")" && "$INSTALL_DIR/dotnet" --version) 2>/dev/null )"
fi


runtime_channels_from_repo() {
  local repo="$1"
  [ -d "$repo" ] || return 0
  (grep -rhoE --include='*.csproj' --include='*.props' --include='*.targets' \
       '<TargetFrameworks?>[^<]+</TargetFrameworks?>' "$repo" 2>/dev/null || true) \
    | sed -E 's/<[^>]+>//g' | tr ';' '\n' \
    | sed -nE 's/^[[:space:]]*net([0-9]+)\.0[[:space:]]*$/\1.0/p' \
    | sort -u
}

runtime_present() {
  local major="${1%%.*}" versions
  versions="$("$DOTNET_ROOT_RESOLVED/dotnet" --list-runtimes 2>/dev/null \
    | sed -nE "s/^Microsoft\.AspNetCore\.App (${major}\.[^ ]+) .*/\1/p")"
  [ -n "$versions" ] || return 1
  printf '%s\n' "$versions" | grep -qv -- '-' && return 0
  [ "$major" = "$(sdk_major)" ]
}

sdk_major() {
  ( cd "$(dirname "${GLOBAL_JSON:-.}")" && "$DOTNET_ROOT_RESOLVED/dotnet" --version 2>/dev/null ) \
    | sed -E 's/^([0-9]+)\..*/\1/'
}

if [ "$WITH_RUNTIMES" = "1" ]; then
  if [ -z "$RUNTIMES" ] && [ -n "$REPO" ]; then
    RUNTIMES="$(runtime_channels_from_repo "$REPO" | tr '\n' ' ')"
  fi

  for channel in $RUNTIMES; do
    if runtime_present "$channel"; then
      say @dotnet_runtime_present "$channel"
      continue
    fi
    say @dotnet_runtime_installing "$channel"
    fetch_installer
    run_installer --channel "$channel" --runtime aspnetcore \
      --install-dir "$DOTNET_ROOT_RESOLVED" --no-path \
      || run_installer --channel "$channel" --runtime aspnetcore --quality preview \
           --install-dir "$DOTNET_ROOT_RESOLVED" --no-path
    runtime_present "$channel" || die @dotnet_runtime_failed "$channel" "${channel%%.*}"
  done
fi


if [ "$CI_OUT" = "1" ]; then
  ci_env "DOTNET_ROOT=$DOTNET_ROOT_RESOLVED"
  ci_path "$DOTNET_ROOT_RESOLVED"
else
  printf 'DOTNET_ROOT=%s\n' "$DOTNET_ROOT_RESOLVED"
fi
