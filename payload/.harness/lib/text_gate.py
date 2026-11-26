from __future__ import annotations

import functools
import importlib.util
import os

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "forge-text-lint.py")


@functools.lru_cache(maxsize=None)
def hook():
    spec = importlib.util.spec_from_file_location("forge_text_lint", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.evaluate


def refusal(body: str, document: bool = False, change: bool = False, title: str = "", capped: bool = True) -> str | None:
    import forge

    tools = forge.tools
    if tools is None:
        return None
    action = (tools.opens or ("open_change",))[0] if change else (tools.documents if document and tools.documents else tools.comments)[0]
    return hook()({"tool_name": tools.prefix + action, "tool_input": {"body": body, **({"title": title} if title else {})},
                   "capped": capped})
