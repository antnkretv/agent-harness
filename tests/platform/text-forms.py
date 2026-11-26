#!/usr/bin/env python3
"""Заготовка и сверка текста агента командами text_templates.py form и render.

Отказ здесь останавливает шаг конвейера, который собирает текст по инструкции:
таблица держит заготовку каждого текста агента и правило неотмеченного пункта
чеклиста PR, у которого есть или нет причины.

    python3 .harness/tests/text-forms.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / ".harness").is_dir():
    ROOT = ROOT.parent
runpy.run_path(str(ROOT / ".harness" / "scripts" / "script_paths.py"))
import text_forms as forms  # noqa: E402
import text_templates  # noqa: E402

TYPE = "docs"


def form(name: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        text_templates.command_form(argparse.Namespace(name=name, out=None))
    return out.getvalue()


def check_agent_forms() -> None:
    """Заготовку пишет каждая переменная текста агента, а не только переменная с формой площадки."""
    failed = []
    for name, entry in text_templates.variables().items():
        if entry.get("prompt"):
            try:
                form(name)
            except ValueError as error:
                failed.append(f"{name}: {error}")
    if failed:
        raise AssertionError("form без заготовки: " + "; ".join(failed))


def body(skeleton: str, reason: bool) -> str:
    """Отмечен один тип; пункты чеклиста вне его набора остаются неотмеченными, с причиной или без."""
    lines, section = [], ""
    for line in skeleton.replace(forms.FILL, "текст").replace(forms.BOX + TYPE, forms.CHECKED + TYPE).split("\n"):
        if line.startswith("## "):
            section = line
        if section == "## Чеклист" and line.startswith(forms.BOX):
            outside = not forms.applies(line, {TYPE})
            line = forms.WHEN.sub("", line) + (f"{forms.UNMET} не относится к PR" if reason or not outside else "")
        lines.append(line)
    return "\n".join(lines)


def check_unmet_reason() -> None:
    """Неотмеченный пункт с причиной принимается и вне набора типа; без причины - отказ."""
    skeleton = form("ChangeBody")
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "body.md"
        for reason in (True, False):
            path.write_text(body(skeleton, reason), encoding="utf-8")
            result = subprocess.run([sys.executable, os.path.join(".harness", "scripts", "text", "text_templates.py"),
                                     "render", "change/description", "--set-file", f"ChangeBody={path}"],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False)
            if (result.returncode == 0) != reason:
                raise AssertionError(f"render с причиной={reason}: код {result.returncode}, {result.stderr.strip()}")


def main() -> int:
    try:
        check_agent_forms()
        check_unmet_reason()
    except (AssertionError, OSError, ValueError) as error:
        print(f"text-forms: FAIL: {error}", file=sys.stderr)
        return 1
    print("text-forms: заготовки текстов агента и пункты чеклиста с причиной прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
