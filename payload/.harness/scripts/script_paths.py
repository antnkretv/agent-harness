from __future__ import annotations

import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
FOLDERS = [*sorted(entry.path for entry in os.scandir(SCRIPTS) if entry.is_dir() and entry.name[0] not in "._"),
           SCRIPTS, os.path.normpath(os.path.join(SCRIPTS, "..", "lib"))]
sys.path[:] = [*FOLDERS, *(path for path in sys.path if path not in FOLDERS)]
