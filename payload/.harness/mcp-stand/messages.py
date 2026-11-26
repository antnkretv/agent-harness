from __future__ import annotations

import os
import re

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.env")
LINE = re.compile(r"^MSG_([a-z][a-z0-9_]*)='([^']*)'", re.MULTILINE)

try:
    with open(REGISTRY, encoding="utf-8") as _handle:
        TEMPLATES = dict(LINE.findall(_handle.read()))
except OSError:
    TEMPLATES = {}


def msg(key: str, *args: object) -> str:
    template = TEMPLATES.get(key)
    if template is None:
        return " ".join(["@" + key, *map(str, args)])
    return template % args
