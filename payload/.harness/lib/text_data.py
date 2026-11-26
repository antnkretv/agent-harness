from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

MARK = re.compile(r"<!-- harness-text: (\{.*?\}) -->")


def mark(template: str, values: Mapping[str, object]) -> str:
    data = json.dumps({"template": template, **{name: str(values[name]) for name in sorted(values)}},
                      ensure_ascii=False)
    if "-->" in data:
        raise ValueError(template)
    return f"<!-- harness-text: {data} -->"


def opening(template: str) -> str:
    return mark(template, {})[:-len("} -->")]


def marks(text: str):
    for match in MARK.finditer(text):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict):
            yield match, data


def marked(text: str, template: str) -> dict:
    found = [data for _, data in marks(text) if data.get("template") == template]
    return found[0] if len(found) == 1 else {}


def unmarked(text: str) -> str:
    return re.sub(r"\n*" + MARK.pattern + r"\n?", "\n", text).strip() + "\n"


def inert(text: str) -> str:
    return text.replace("<!--", "&lt;!--")


def said(bodies: list[str], template: str, values: Mapping[str, object], anchor: str) -> bool:
    wanted = marked(mark(template, values), template)
    return any(found == wanted if (found := marked(body, template)) else anchor in body for body in bodies)


def digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def rendered(text: str) -> bool:
    return any("Digest" in data for _match, data in marks(text))


def altered(text: str) -> str:
    for match, data in marks(text):
        if "Digest" in data and data["Digest"] != digest(text[:match.start()]):
            return str(data.get("template", ""))
    return ""
