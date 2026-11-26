#!/usr/bin/env python3
"""Валит дерево, в котором файл C# расходится с выводом CSharpier. Правило и его причина -
docs/platform/delivery/ci-cd.md, раздел "Форматирование C#".

    ./.harness/hooks/python.cmd .harness/scripts/check/check-dotnet-format.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from ci_common import ROOT, msg, run_strict  # noqa: E402


def main() -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    run_strict("dotnet", "tool", "restore", unread=msg("dotnet_format_restore"))
    if subprocess.run(["dotnet", "tool", "run", "csharpier", "check", "."], cwd=ROOT).returncode != 0:
        print(msg("dotnet_format_found"))
        return 1
    print(msg("dotnet_format_clean"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
