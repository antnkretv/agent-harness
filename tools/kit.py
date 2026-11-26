#!/usr/bin/env python3
"""Сборка и сверка комплекта harness в репозитории-источнике."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kit_support.cli import main

if __name__ == "__main__":
    sys.exit(main())
