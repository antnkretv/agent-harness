#!/usr/bin/env python3
"""Заготовки форм и их сверка командой render в установленном дереве.

Отказ здесь останавливает агента на шаге, который требует заготовку формы, или
отклоняет описание PR, собранное по правилу чеклиста. Таблица держит выдачу
заготовки каждой переменной с полем form и сборку заполненной заготовки ее
шаблоном. Заготовка описания PR оставляет пункты чеклиста вне набора типов
изменения неотмеченными с причиной и проходит; без причины ее отклоняет
отдельный сценарий.

    python3 .harness/tests/text-forms.py
"""

from __future__ import annotations

import json
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
SCRIPTS = ROOT / ".harness" / "scripts"
TEXT = SCRIPTS / "text" / "text_templates.py"
TEMPLATES = ROOT / ".harness" / "templates"
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS / "text")]
import script_paths  # noqa: E402,F401
from text_forms import FILL, UNMET, WHEN  # noqa: E402

REASON = UNMET + " к изменению не относится"
REPORTS = ("AuditReport", "ReviewReport", "SimplifyReport", "DelegatedReport")
failed = 0


def report(name: str, passed: bool, detail: str = "") -> None:
    global failed
    print(("  ok    " if passed else "  FAIL  ") + name)
    if not passed:
        failed += 1
        print("        " + detail.strip(), file=sys.stderr)


def text(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TEXT), *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8")


def sections(form: str) -> list[list[str]]:
    found: list[list[str]] = [[]]
    for line in form.split("\n"):
        if line.startswith("## "):
            found.append([])
        found[-1].append(line)
    return found


def filled(form: str, kind: str = "docs", reason: bool = True) -> str:
    """Заполняет заготовку, как ее заполняет автор: необязательные разделы сняты, отмечен тип kind,
    пункт чеклиста из набора отмечен, вне набора оставлен неотмеченным с причиной или без нее."""
    lines: list[str] = []
    for section in sections(form):
        if "<!-- optional -->" in section:
            continue
        for number, line in enumerate(section):
            if line == FILL and number and section[number - 1].startswith("| ---"):
                line = "|" + " текст |" * section[number - 1].count("---")
            elif line.startswith("- [ ] ") and "<!-- pick: some -->" in section:
                line = line.replace("- [ ] ", "- [x] ", 1) if line[6:].split()[0] == kind else ""
            elif line.startswith("- [ ] ") and "<!-- pick: all -->" in section:
                keys = WHEN.search(line)
                if not keys or kind in keys.group(1).split():
                    line = line.replace("- [ ] ", "- [x] ", 1)
                elif reason:
                    line = WHEN.sub("", line) + REASON + keys.group(0)
            lines.append(line.replace(FILL, "текст"))
    return "\n".join(lines)


def forms() -> dict[str, str]:
    registry = json.loads((TEMPLATES / "variables.json").read_text(encoding="utf-8"))["variables"]
    return {name: entry.get("form", "") for name, entry in registry.items() if entry.get("form") or name in REPORTS}


def owner(name: str) -> str:
    for path in sorted(TEMPLATES.rglob("*.md")):
        if "${" + name + "}" in path.read_text(encoding="utf-8"):
            return path.relative_to(TEMPLATES).with_suffix("").as_posix()
    return ""


def skeleton(name: str) -> tuple[str, str]:
    done = text("form", name)
    return (done.stdout, "") if done.returncode == 0 else ("", done.stderr)


def form_cases(work: Path) -> None:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    for name, spec in forms().items():
        blank, refusal = skeleton(name)
        source = work / f"{name}.md"
        source.write_text(filled(blank), encoding="utf-8")
        done = text("render", owner(name), "--set-file", f"{name}={source}", "--commit", head)
        report(f"заготовка {name} собирается шаблоном {owner(name)}", bool(blank) and done.returncode == 0,
               refusal or done.stderr)


def checklist_cases(work: Path) -> None:
    blank = skeleton("ChangeBody")[0]
    source = work / "body.md"
    source.write_text(filled(blank, reason=False), encoding="utf-8")
    done = text("render", "change/description", "--set-file", f"ChangeBody={source}")
    report("пункт чеклиста вне набора без причины отклонен", done.returncode == 1 and "Невыполнимо" in done.stderr,
           done.stderr)


def main() -> int:
    with tempfile.TemporaryDirectory() as folder:
        form_cases(Path(folder))
        checklist_cases(Path(folder))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
