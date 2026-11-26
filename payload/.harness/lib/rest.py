from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone

TIMEOUT = 30
PAGE = 100
STAMP = "%Y-%m-%dT%H:%M:%SZ"
FORMS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")


def literal(settings: dict, key: str) -> str:
    value = settings.get(key)
    return value if isinstance(value, str) else ""


def detail(payload) -> str:
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)[:300]
    problems = [str(item) for item in payload.get("errorMessages") or []]
    errors = payload.get("errors")
    if isinstance(errors, dict):
        problems += [f"{name}: {why}" for name, why in errors.items()]
    return "; ".join(problems) or str(payload.get("message") or payload.get("error")
                                      or json.dumps(payload, ensure_ascii=False)[:300])


def utc(text: str) -> str:
    for form in FORMS:
        try:
            return datetime.strptime(text, form).astimezone(timezone.utc).strftime(STAMP)
        except ValueError:
            continue
    return text


def request(method: str, url: str, headers: Mapping[str, str], error: type[Exception], payload=None,
            ok: Collection[int] = (200,), raw: bool = False,
            private: Mapping[str, str] | None = None):
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    call = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json", **headers})
    for name, value in (private or {}).items():
        call.add_unredirected_header(name, value)
    try:
        with urllib.request.urlopen(call, timeout=TIMEOUT) as answer:
            body, status = answer.read().decode("utf-8", "replace"), answer.status
    except urllib.error.HTTPError as failure:
        body, status = failure.read().decode("utf-8", "replace"), failure.code
    except OSError as failure:
        raise error(str(getattr(failure, "reason", failure))) from None
    if raw and status in ok:
        return body
    try:
        parsed = json.loads(body) if body.strip() else {}
    except ValueError:
        parsed = {"message": body[:300]}
    if status not in ok:
        raise error(f"HTTP {status}: {detail(parsed)}", status)
    return parsed


def each_page(fetch: Callable[[str], object], path: str, error: type[Exception], limit: int | None = None):
    separator, page = "&" if "?" in path else "?", 1
    while limit is None or page <= limit:
        items = fetch(f"{path}{separator}per_page={PAGE}&page={page}")
        if not isinstance(items, list):
            raise error(detail(items))
        yield items
        if len(items) < PAGE:
            return
        page += 1


def pages(fetch: Callable[[str], object], path: str, error: type[Exception]) -> list:
    return [item for items in each_page(fetch, path, error) for item in items]


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
