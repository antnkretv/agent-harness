"""Установка, обновление и проверка harness в репозитории проекта.

Установщик живет только в архиве комплекта и в проект не ставится. Порядок действий и критерии
готовности - AGENT.md комплекта, устройство установленного слоя - .harness/docs/harness.md.

    ./payload/.harness/hooks/python.cmd ah.py init --target <repo>      # подключение проекта: вопросы настройки, затем apply
    python3 .harness/ah.py change                                       # смена настроек: все вопросы с прежними ответами, затем apply
    ./payload/.harness/hooks/python.cmd ah.py pack --config <манифест или config.json> --out <каталог>  # архив под решения
    ./payload/.harness/hooks/python.cmd ah.py apply --target <repo> [--archive <архив этой сборки>]
    ./payload/.harness/hooks/python.cmd ah.py install --target <repo>   # файлы комплекта версии из lock.json; обычно через .harness/ah.py
    python3 .harness/ah.py upgrade [--version <версия>]                 # новая версия: ветка, apply, lock, PR
    ./payload/.harness/hooks/python.cmd ah.py verify --target <repo>    # приемка: параметры, реестр, гейты, пробы
    ./payload/.harness/hooks/python.cmd ah.py status --target <repo>    # файлы комплекта, которые apply перезапишет
    ./payload/.harness/hooks/python.cmd ah.py doctor --target <repo>    # зависимости комплекта против машины
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from .archive import pack
from .catalog import load_catalog
from .install import apply
from .lifecycle import change, init, upgrade
from .lock_install import install
from .resources import Refusal, kit_root
from .review import providers, review
from .verify import doctor, status, verify


def workspace(args: argparse.Namespace) -> int:
    script = kit_root() / "payload/.harness/scripts/pipeline/task-worktree.py"
    command = [sys.executable, str(script), "start", "--root", args.target, "--base", args.base, "--type", "chore", "--key", args.key]
    return subprocess.run(command, check=False).returncode


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    initialized = commands.add_parser("init", help="подключить проект: вопросы настройки, затем оболочка, манифест и lock")
    initialized.add_argument("--archive", help="архив этой версии; без него init скачивает архив выпуска")
    initialized.set_defaults(handler=init)
    changed = commands.add_parser("change", help="сменить настройки: все вопросы с прежними ответами, затем apply")
    changed.set_defaults(handler=change)
    upgraded = commands.add_parser("upgrade", help="новая версия комплекта: ветка задачи, apply новой версией, lock и PR")
    chosen = upgraded.add_mutually_exclusive_group()
    chosen.add_argument("--version", help="версия выпуска; без нее и без --commit - последний выпуск")
    chosen.add_argument("--commit", help="ветка или коммит репозитория комплекта: архив собирается из исходника этого коммита")
    chosen.add_argument("--new-archive", help="архив новой версии вместо скачивания выпуска; --archive забирает загрузчик")
    upgraded.add_argument("--base", help="база ветки и PR; без нее - git.baseBranch манифеста")
    upgraded.add_argument("--no-pr", action="store_true", help="обновить рабочую копию на месте, без ветки и PR")
    upgraded.set_defaults(handler=upgrade)
    packed = commands.add_parser("pack", help="архив комплекта под решения манифеста или config.json")
    packed.add_argument("--config", required=True, help="манифест проекта или config.json с решениями")
    packed.add_argument("--out", default=".", help="каталог архива")
    packed.set_defaults(handler=pack)
    allocated = commands.add_parser("workspace", help="allocate a worktree before installation or upgrade")
    allocated.add_argument("--base", required=True, help="origin/<branch>, or refs/heads/<branch> without a forge")
    allocated.add_argument("--key", default="harness-install", help="stable installation or upgrade task key")
    allocated.set_defaults(handler=workspace)
    reviewed = commands.add_parser("review", help="prepare or collect first-install review from a checksum-pinned external kit")
    reviewed.add_argument("--archive", required=True, help="trusted external .tar.gz; obtain its checksum outside the PR")
    reviewed.add_argument("--sha256", required=True, help="out-of-band SHA-256 of the entire trusted archive")
    reviewed.add_argument("--pr", required=True, type=int)
    reviewed.add_argument("--base", default="origin/develop")
    reviewed.add_argument("--work", required=True, help="private packet directory outside the consumer")
    reviewed.add_argument("--provider", choices=providers(load_catalog()), required=True)
    reviewed.add_argument("--reason", default="unavailable", help="ключ причины локального круга из LocalReason реестра шаблонов")
    reviewed.add_argument("--context", default="")
    reviewed.add_argument("--pass", dest="passes", action="append")
    reviewed.add_argument("--collect", action="store_true")
    reviewed.set_defaults(handler=review)
    applied = commands.add_parser("apply", help="установить или обновить harness")
    applied.add_argument("--dry-run", action="store_true", help="показать план без записи")
    applied.add_argument("--archive", help="архив этой сборки: lock.json закрепляет его tree, а сам он идет в кеш версий")
    applied.set_defaults(handler=apply)
    commands.add_parser("install", help="поставить файлы комплекта версии из lock.json в рабочую копию").set_defaults(handler=install)
    verified = commands.add_parser("verify", help="приемка установки")
    verified.add_argument("--no-gates", action="store_true", help="без гейтов дерева и проб: этапы 1 и 2 установки")
    verified.add_argument("--no-probes", action="store_true", help="без отрицательных проб гейтов")
    verified.set_defaults(handler=verify)
    commands.add_parser("status", help="файлы и блоки комплекта, разошедшиеся с версией комплекта: apply их перезапишет").set_defaults(handler=status)
    doctored = commands.add_parser("doctor", help="зависимости комплекта против машины")
    doctored.add_argument("--ci", action="store_true", help="зависимости CI обязательны")
    doctored.set_defaults(handler=doctor)
    for command in commands.choices.values():
        if command is not packed:
            command.add_argument("--target", default=".", help="корень репозитория проекта")
    return parser.parse_args()


def main() -> int:
    try:
        args = arguments()
        return args.handler(args)
    except Refusal as refusal:
        print(str(refusal), file=sys.stderr)
        return 2
