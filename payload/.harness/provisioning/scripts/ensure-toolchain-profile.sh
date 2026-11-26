#!/usr/bin/env bash
# Сохраняет подключение окружения сессии session-env.sh в файлах старта Bash пользователя.
#   ensure-toolchain-profile.sh
set -euo pipefail

HARNESS_SAY_PREFIX="toolchain-profile: "
# shellcheck source=../lib/shell-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/shell-common.sh"

case "${1:-}" in
  "") ;;
  -h|--help) print_header_help "$0"; exit 0 ;;
  --env-script) usage_die @toolchain_profile_env_script ;;
  *) reject_arg "$1" ;;
esac

harness_python - "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/session-env.sh" <<'PYTHON'
import re
import shlex
import sys
from pathlib import Path

home = Path.home()
login = next((home / name for name in (".bash_profile", ".bash_login", ".profile")
              if (home / name).exists()), home / ".profile")
script = shlex.quote(str(Path(sys.argv[1]).resolve()))
block = ("# harness:begin toolchain-env\n"
         f"if [ -r {script} ]; then\n"
         f'  eval "$(bash {script} --export)"\n'
         "fi\n"
         "# harness:end toolchain-env\n")
for path in (home / ".bashrc", login):
    content = path.read_text() if path.exists() else ""
    content = re.sub(r"^# harness:begin toolchain-env-extension\n.*?^# harness:end toolchain-env-extension\n?\n?", "",
                     content, flags=re.M | re.S)
    pattern = re.compile(r"^# harness:begin toolchain-env\n.*?^# harness:end toolchain-env\n?", re.M | re.S)
    if pattern.search(content):
        content = pattern.sub(lambda match: block, content)
    else:
        if "# harness:begin toolchain-env\n" in content:
            raise ValueError(str(path))
        content = content.rstrip("\n") + ("\n\n" if content else "") + block
    path.write_text(content)
PYTHON
say @toolchain_profile_written
