from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .configure import read_answers, settle
from .lock_install import loader
from .questions import KEEP, Question, value_text
from .resources import ANSWERS, HERE, Refusal, msg, say, sha256

REQUIREMENTS = HERE / "installer" / "requirements.txt"
OTHER = object()


def terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def git_bash() -> bool:
    return os.name == "nt" and bool(os.environ.get("MSYSTEM"))


def questionary():
    folder = loader().CACHE / "deps" / sha256(REQUIREMENTS.read_bytes())[:16]
    if not folder.is_dir():
        fresh = folder.with_name(f"{folder.name}.{os.getpid()}.tmp")
        shutil.rmtree(fresh, ignore_errors=True)
        say("prompt_installing", folder)
        done = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                               "--no-deps", "--only-binary", ":all:", "--require-hashes", "--target", str(fresh),
                               "-r", str(REQUIREMENTS)], capture_output=True, text=True, encoding="utf-8")
        if done.returncode:
            shutil.rmtree(fresh, ignore_errors=True)
            raise Refusal(msg("prompt_pip", REQUIREMENTS, (done.stderr or done.stdout).strip()[-2000:]))
        try:
            fresh.rename(folder)
        except OSError:
            shutil.rmtree(fresh, ignore_errors=True)
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    return importlib.import_module("questionary")


def titled(label: str, note: str) -> list[tuple[str, str]]:
    return [("", label), *([("class:instruction", f"  {note}")] if note else [])]


def validator(question: Question):
    def check(value) -> bool | str:
        try:
            question.parse(value)
        except ValueError as error:
            return str(error)
        return True
    return check if question.parse else None


def asked(library, question: Question):
    if question.why:
        print(msg("prompt_why", question.why))
    if question.rule:
        print(msg("prompt_rule", question.rule))
    if question.items is not None:
        choices = [library.Choice(titled(f"{item['id']} = {value_text(item['value'])}", item["why"]), value=item["id"],
                                  checked=True) for item in question.items]
        kept = library.checkbox(question.text, choices=choices, instruction=msg("prompt_summary_hint")).unsafe_ask()
        dropped = [item["id"] for item in question.items if item["id"] not in kept]
        return ",".join(dropped) if dropped else KEEP
    if question.kind == "single":
        values = [entry["value"] for entry in question.options]
        choices = [library.Choice(titled(entry["label"], entry["description"]), value=entry["value"]) for entry in question.options]
        if not question.decision:
            choices.append(library.Choice(msg("prompt_other"), value=OTHER))
        default = question.default if question.default in values else None
        picked = library.select(question.text, choices=choices, default=default).unsafe_ask()
        if picked is not OTHER:
            return picked
        return library.text(question.text, validate=validator(question) or (lambda _: True)).unsafe_ask()
    if question.kind == "multi":
        chosen = set(question.default) if isinstance(question.default, list) else {question.default}
        choices = [library.Choice(titled(entry["label"], entry["description"]), value=entry["value"],
                                  checked=entry["value"] in chosen) for entry in question.options]
        return library.checkbox(question.text, choices=choices, validate=validator(question) or (lambda _: True)).unsafe_ask()
    return library.text(question.text, default=value_text(question.default),
                        validate=validator(question) or (lambda _: True)).unsafe_ask()


def unattended(root: Path) -> list[str]:
    if git_bash():
        print(msg("prompt_git_bash"), file=sys.stderr)
    pending, problems, found = settle(root, strict=True, defaults=True)
    for item in found:
        print(msg("prompt_found", item["id"], value_text(item["value"]), item.get("why") or "-"))
    if pending is not None:
        raise Refusal(msg("prompt_unanswered", pending.id, pending.text, pending.rule or pending.why or "-", ANSWERS))
    return problems


def answered(root: Path, change: bool = False) -> list[str]:
    if not terminal():
        return unattended(root)
    library = questionary()
    given, first = {}, not read_answers(root).get("reopened")
    while True:
        try:
            pending, problems, found = settle(root, given, change and first)
        except Refusal as error:
            if not given:
                raise
            print(error, file=sys.stderr)
            given = {}
            continue
        first = False
        for item in found:
            print(msg("prompt_found", item["id"], value_text(item["value"]), item.get("why") or "-"))
        if pending is None:
            return problems
        try:
            given = {pending.id: asked(library, pending)}
        except KeyboardInterrupt:
            raise Refusal(msg("prompt_aborted", ANSWERS)) from None
        except Exception as error:
            if type(error).__name__ == "NoConsoleScreenBufferError":
                raise Refusal(msg("prompt_no_console")) from None
            raise
