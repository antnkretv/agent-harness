#!/usr/bin/env python3
"""Каналы уведомлений по манифесту: куда идет вид уведомления и что делать с отказом доставки.

Канал - расширение модуля notifications в .harness/project/manifest.json, его адаптер - channels/<имя>.sh рядом.
Вид уведомления (release и виды проекта) идет в каналы, которые ему называет настройка модуля events, вид
без записи - во все включенные каналы. Отказ канала с onFailure=fatal роняет рассылку, с warn - остается
предупреждением. Рассылает notify.sh.

Контракт адаптера. Уведомление приходит переменными NOTIFY_KIND (вид), NOTIFY_TITLE (заголовок одной
строкой), NOTIFY_BODY (тело в markdown) и необязательной NOTIFY_STATUS (исход: release, release-failure,
rollback, rollback-failure - по нему адаптер ставит значок), адресация - переменными канала. Код 0 - доставлено, 3 - канал
здесь не адресован и пропускается: у выката нет своего запроса на слияние, у канала может не быть токена.
Иной код - отказ доставки. Свои настройки адаптер читает через manifest.setting: ссылку на секрет
раскрывает переменная окружения с именем секрета, и значение секрета не печатается.

    ./.harness/hooks/python.cmd .harness/notify/notify.py channels release   # план рассылки: <канал> <политика> на строку
    ./.harness/hooks/python.cmd .harness/notify/notify.py secrets            # секреты включенных каналов для CI: secret-<n>=<имя>
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from manifest import extensions, secrets, setting  # noqa: E402
from platform_messages import msg  # noqa: E402

MODULE = "notifications"
POLICIES = ("fatal", "warn")


class NotifyError(RuntimeError):
    pass


def channels(kind: str) -> list[tuple[str, str]]:
    enabled = extensions(MODULE)
    routes = setting(MODULE, "events", {})
    named = routes.get(kind) if isinstance(routes, dict) else None
    if named is None:
        named = list(enabled)
    elif isinstance(named, str):
        named = [named]
    elif not isinstance(named, list):
        raise NotifyError(msg("notify_route_shape", kind, named))
    plan = []
    for name in named:
        if name not in enabled:
            raise NotifyError(msg("notify_route_off", kind, name))
        policy = setting(f"{MODULE}/{name}", "onFailure", "fatal")
        if policy not in POLICIES:
            raise NotifyError(msg("notify_unknown_policy", name, policy))
        plan.append((name, policy))
    return plan


def secret_names() -> list[str]:
    found: list[str] = []
    for name in extensions(MODULE):
        found += [secret for secret in secrets(f"{MODULE}/{name}") if secret not in found]
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    planned = commands.add_parser("channels", help="каналы вида уведомления и политика отказа")
    planned.add_argument("kind")
    commands.add_parser("secrets", help="имена секретов включенных каналов, secret-<n>=<имя>")
    args = parser.parse_args()
    if args.command == "secrets":
        for number, name in enumerate(secret_names(), 1):
            print(f"secret-{number}={name}")
        return 0
    try:
        plan = channels(args.kind)
    except NotifyError as error:
        print(error, file=sys.stderr)
        return 1
    for name, policy in plan:
        print(name, policy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
