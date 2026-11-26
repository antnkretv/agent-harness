#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from file_lock import Busy, locked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default="")
    parser.add_argument("lock", type=Path)
    parser.add_argument("executable")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    announced = False
    while True:
        try:
            with locked(args.lock):
                return subprocess.call([args.executable, *args.command], env={**os.environ, "HARNESS_FILE_LOCK": str(args.lock.resolve())})
        except Busy:
            if args.message and not announced:
                print(args.message, file=sys.stderr, flush=True)
                announced = True
            time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
