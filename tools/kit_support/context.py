from __future__ import annotations

import json
import os
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
    """Шаблоны следов проекта, который шлет правки в комплект, лежат вне репозитория: их файл называет
    AGENT_HARNESS_LEAKS, и сверка добавляет их к шаблонам kit.json."""
    kit = json.loads((KIT / "kit.json").read_text(encoding="utf-8"))
    extra = os.environ.get("AGENT_HARNESS_LEAKS")
    if extra:
        kit["leaks"] = kit["leaks"] + json.loads(Path(extra).read_text(encoding="utf-8"))
    return kit


catalog = load_catalog

REFERENCE = "docs/platform/agents/harness-parameters.md"


