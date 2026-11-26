#!/usr/bin/env python3

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
from tool_pins import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
