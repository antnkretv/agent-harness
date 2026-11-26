from __future__ import annotations

import os
import re

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "messages.env")
PROJECT_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "project", "messages.env")
LINE = re.compile(r"^MSG_([a-z][a-z0-9_]*)='([^']*)'", re.MULTILINE)


def load(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return dict(LINE.findall(handle.read()))
    except OSError:
        return {}


PROJECT_TEMPLATES = load(PROJECT_REGISTRY)
TEMPLATES = {**load(REGISTRY), **PROJECT_TEMPLATES}


def format_message(templates, key: str, *args: object) -> str:
    template = templates.get(key)
    if template is None:
        return " ".join(["@" + key, *map(str, args)])
    return template % args


def msg(key: str, *args: object) -> str:
    return format_message(TEMPLATES, key, *args)
