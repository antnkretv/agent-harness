#!/usr/bin/env python3
"""PR обновления комплекта: ah upgrade зовет его в рабочей копии ветки после apply новой версии."""

from __future__ import annotations

import argparse
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
import text_templates  # noqa: E402
from ci_common import forge, opened_from  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("--head", "--base", "--title", "--previous", "--version", "--sha", "--files"):
        parser.add_argument(option, required=True)
    args = parser.parse_args()
    opened = opened_from(args.head)
    if opened is not None:
        print(opened.url)
        return 0
    body = text_templates.render("change/kit-upgrade", {"PreviousVersion": args.previous, "KitVersion": args.version,
                                                         "ArchiveSha": args.sha, "FileCount": args.files})
    print(forge.open_change(args.head, args.base, args.title, body, True).url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
