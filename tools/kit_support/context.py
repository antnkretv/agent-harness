from __future__ import annotations

import json
from pathlib import Path

from installer.catalog import load_catalog
from installer.resources import KIT_DOCUMENTS, LINE, fill, runtime_files

KIT = Path(__file__).resolve().parents[2]
SHIPPED = (*KIT_DOCUMENTS, *runtime_files(KIT))
TREES = ("payload", "fragments", "skills")
TEMPLATES = dict(LINE.findall((KIT / "tools" / "messages.env").read_text(encoding="utf-8")))


def msg(key: str, *args: object) -> str:
    return fill(TEMPLATES, key, *args)


def manifest() -> dict:
    return json.loads((KIT / "kit.json").read_text(encoding="utf-8"))


catalog = load_catalog

REFERENCE = "docs/platform/agents/harness-parameters.md"


