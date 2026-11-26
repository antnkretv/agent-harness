"""Сборка и сверка комплекта harness в репозитории-источнике.

    harness-kit/payload/.harness/hooks/python.cmd harness-kit/tools/kit.py build [--out <каталог>]   # каталог и архив полного комплекта
    harness-kit/payload/.harness/hooks/python.cmd harness-kit/tools/kit.py build --config <файл>     # архив под решения манифеста или config.json
    harness-kit/payload/.harness/hooks/python.cmd harness-kit/tools/kit.py check [--adapters | --no-smoke]  # состав, следы, комментарии, воспроизводимость, smoke

Порядок сопровождения - harness-kit/MAINTAINING.md, раздел "Сопровождение".
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from installer.archive import archive
from installer.resources import STACKS_WITH_ADAPTER, sha256

from .comment_checks import comment_problems
from .composition import composition
from .context import KIT, ROOT, manifest, msg
from installer.resources import unpack

from .packaging import build, stage
from .smoke import smoke
from .source import leak_rules, leaks


def kit_anchors() -> list[str]:
    """Якоря файлов комплекта сверяются здесь по установленной копии: гейт проекта их пропускает."""
    done = subprocess.run([sys.executable, ".harness/scripts/check/check-docs.py", "--anchors", "--kit-files"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    return [] if done.returncode == 0 else [msg("kit_anchors_broken", done.stdout.strip()[-2000:])]


def check(args: argparse.Namespace) -> int:
    kit = manifest()
    problems = composition(kit) + comment_problems(kit) + kit_anchors()
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        folder = stage(kit, Path(first))
        shipped = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
        problems += leaks(leak_rules(kit, installed=False), folder, [path for path in shipped if path not in kit["provenance"]])
        packed = [archive(folder), archive(stage(kit, Path(second)))]
        digests = {sha256(path.read_bytes()) for path in packed}
        if len(digests) != 1:
            problems.append(msg("kit_not_reproducible"))
        unpacked = Path(second) / "unpacked"
        unpack(packed[0], unpacked)
        extracted = unpacked / folder.name
        listed = {path.relative_to(extracted).as_posix() for path in extracted.rglob("*") if path.is_file()}
        problems += [msg("kit_archive_missing", path) for path in sorted(set(shipped) - listed)]
        if not problems and not args.no_smoke:
            problems += smoke(extracted, list(STACKS_WITH_ADAPTER) if args.adapters else [], kit)
    if problems:
        print("\n".join(problems))
        print(msg("kit_check_failed", len(problems)))
        return 1
    print(msg("kit_check_clean", kit["version"], digests.pop()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("build", help="собрать каталог и архив комплекта")
    built.add_argument("--out", default=str(KIT / "dist"), help="каталог результата")
    built.add_argument("--config", help="манифест проекта или config.json: архив под эти решения")
    built.set_defaults(handler=build)
    checked = commands.add_parser("check", help="сверка комплекта и smoke-установка")
    smoked = checked.add_mutually_exclusive_group()
    smoked.add_argument("--adapters", action="store_true", help="smoke с приложениями node и dotnet")
    smoked.add_argument("--no-smoke", action="store_true", help="без smoke: состав, следы, комментарии, воспроизводимость")
    checked.set_defaults(handler=check)
    args = parser.parse_args()
    return args.handler(args)


