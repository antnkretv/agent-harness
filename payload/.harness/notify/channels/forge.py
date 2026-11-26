#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))

import forge  # noqa: E402
from platform_messages import msg  # noqa: E402

SKIPPED = 3
PLACEMENTS = ("comment", "body")


def replace_block(current: str, marker: str, text: str) -> str:
    closing = marker.replace("<!-- ", "<!-- /", 1)
    block = f"{marker}\n{text.rstrip()}\n{closing}"
    body = re.sub(re.escape(marker) + r".*?" + re.escape(closing), "", current, flags=re.DOTALL)
    body = re.sub(r"\n{3,}", "\n\n", body).rstrip()
    return f"{body}\n\n{block}\n" if body else f"{block}\n"


def main() -> int:
    change = os.environ["NOTIFY_FORGE_CHANGE"]
    marker = os.environ.get("NOTIFY_FORGE_MARKER", "")
    placement = os.environ.get("NOTIFY_FORGE_PLACEMENT", "comment")
    if not change.isdigit():
        print(msg("notify_forge_change", change), file=sys.stderr)
        return 1
    if placement not in PLACEMENTS or placement == "body" and not marker:
        print(msg("notify_forge_placement", placement), file=sys.stderr)
        return 1
    if forge.missing():
        print(forge.missing(), file=sys.stderr)
        return SKIPPED
    number = int(change)
    text = f"### {os.environ['NOTIFY_TITLE']}\n\n{os.environ['NOTIFY_BODY']}\n"
    try:
        if placement == "body":
            forge.edit_change(number, replace_block(forge.change_body(number), marker, text))
        else:
            forge.comment(number, f"{marker}\n{text}" if marker else text, marker)
    except forge.ForgeUnavailable as error:
        print(error, file=sys.stderr)
        return SKIPPED
    except forge.ForgeError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
