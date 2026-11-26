#!/usr/bin/env bash

_FORGE_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"

. "$_FORGE_LIB/manifest.sh"

_forge() { harness_python "$_FORGE_LIB/forge.py" "$@" | tr -d '\r'; }

forge_annotation() { _forge annotation "$@" 2>/dev/null || printf '%s\n' "$2"; }

forge_export_env() { harness_python "$_FORGE_LIB/forge.py" export-env "$@"; }
forge_add_path() { harness_python "$_FORGE_LIB/forge.py" add-path "$@"; }
