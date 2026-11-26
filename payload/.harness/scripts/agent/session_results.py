from __future__ import annotations

import json


def session_result(log_path: str) -> dict | None:
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        return parse_session_result(handle.read())


def parse_session_result(text: str) -> dict | None:
    for row in reversed(text.splitlines()):
        try:
            item = json.loads(row)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("type") == "result":
            return item
    return None


def native_result(items: list) -> dict:
    return next((item for item in reversed(items) if isinstance(item, dict) and item.get("type") == "result"
                 and "comments" not in item), {})
