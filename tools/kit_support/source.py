from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

from installer.catalog import owner
from installer.resources import CATALOG

from .catalog_checks import catalog_paths
from .context import KIT, REFERENCE, TREES, catalog, msg


def git(*args: str, data: bytes | None = None, accept: tuple[int, ...] = (0,)) -> bytes:
    """Git от каталога комплекта: пути состава не зависят от того, где лежит checkout."""
    done = subprocess.run(["git", *args], cwd=KIT, input=data, capture_output=True)
    if done.returncode not in accept:
        raise SystemExit(msg("kit_git_failed", " ".join(args), done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout


@functools.cache
def tree_files() -> tuple[str, ...]:
    """Файлы деревьев комплекта по правилам .gitignore: кеш интерпретатора и выход сборки в состав не входят."""
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *TREES).decode("utf-8").split("\0")
    return tuple(sorted(path for path in listed if path and (KIT / path).is_file()))


def leak_rules(kit: dict, installed: bool) -> list[tuple[re.Pattern[str], tuple[str, ...]]]:
    """В установленном дереве пути исключений читаются от корня проекта, а шаблоны со scope kit (пути
    приложений, номера задач) законны и не применяются. Путь каталога комплекта на машине сборки - след везде.
    Имя технологии законно в каталоге и в файлах расширения, которое ее несет (modules правила)."""
    rules = [(re.compile(re.escape(str(KIT))), ())]
    for rule in kit["leaks"]:
        if installed and rule.get("scope") == "kit":
            continue
        allow = [p.removeprefix("payload/") if installed else p for p in rule.get("allow", [])]
        if rule.get("modules"):
            # Справочник параметров - человеческая копия каталога: имена расширений законны и в нем.
            prefix = "" if installed else "payload/"
            allow += [(".harness/state/" if installed else "") + CATALOG, prefix + REFERENCE]
            allow += [prefix + path for path, meta in kit["files"].items() if owner(meta) in rule["modules"]]
        rules.append((re.compile(rule["pattern"]), tuple(allow)))
    return rules


def module_mask(modules: dict) -> re.Pattern[str]:
    """Пути модулей каталога: путь называет модуль, а не технологию или каталог проекта, и правилу следа не виден."""
    paths = sorted((path for path in catalog_paths(modules) if "/" in path), key=len, reverse=True)
    return re.compile(r"(?<![\w/-])(?:" + "|".join(map(re.escape, paths)) + r")(?![\w-])")


def leaks(rules: list[tuple[re.Pattern[str], tuple[str, ...]]], tree: Path, paths: list[str]) -> list[str]:
    mask = module_mask(catalog())
    problems = []
    for relative in paths:
        try:
            text = mask.sub("", (tree / relative).read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, allow in rules:
            if relative.startswith(allow) or not pattern.search(text):
                continue
            problems += [f"{relative}:{number}: {pattern.pattern}: {line.strip()[:100]}"
                         for number, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
    return problems
