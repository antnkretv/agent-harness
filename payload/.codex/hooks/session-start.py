#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
subprocess.call([sys.executable, str(root / ".harness" / "ah.py"), "install"], stdout=sys.stderr)
sys.path.insert(0, str(root / ".harness" / "lib"))
from platform_messages import msg
from executables import bash
environment = os.environ.copy()
environment["AGENT_PROJECT_DIR"] = str(root)
environment["AGENT_REMOTE"] = environment.get("CODEX_REMOTE", "false")
environment["AGENT_ENV_FILE"] = environment.get("CODEX_ENV_FILE", "")

shell = bash()
if not Path(shell).is_file():
    print(msg("session_no_bash"), file=sys.stderr)
    sys.exit(1)
sys.exit(subprocess.call(
    [shell, ".harness/hooks/session-start.sh"],
    cwd=root,
    env=environment,
))
