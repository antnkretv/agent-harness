from __future__ import annotations
import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from ci_common import forge, msg

MARKUP = re.compile(r"<!--.*?-->", re.S)


def fail(message: str, code: int = 1) -> None:
    print(f"pr-threads: {message}", file=sys.stderr)
    raise SystemExit(code)


def summary(thread, width: int = 140) -> str:
    text = " ".join(MARKUP.sub(" ", thread.body).split())
    return text[:width] + "..." if len(text) > width else text


def where(thread) -> str:
    place = thread.path or msg("threads_pr_discussion")
    return f"{place}:{thread.line}" if thread.line else place


def show(items: list) -> None:
    for thread in items:
        marks = [
            mark
            for mark, on in ((msg("threads_state_resolved"), thread.resolved), (msg("threads_state_outdated"), thread.outdated))
            if on
        ]
        print(f"{thread.id}  {thread.author or '?'}  {where(thread)}" + (f"  [{', '.join(marks)}]" if marks else ""))
        print(f"    {summary(thread)}")


def resolved_context(items: list, authors: list[str]) -> list[str]:
    return [f"> - {where(thread)}: {summary(thread, width=400)}"
            for thread in items if thread.resolved and thread.author in authors]


def main() -> int:
    parser = argparse.ArgumentParser(description="Треды ревью на запросе слияния: показать, ответить, закрыть.")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="треды запроса, по умолчанию только незакрытые")
    listing.add_argument("pr", type=int)
    listing.add_argument("--all", action="store_true", help="вместе с закрытыми")
    listing.add_argument("--json", action="store_true", help="вывод для разбора программой")
    listing.add_argument(
        "--resolved-context",
        action="store_true",
        help="закрытые находки одной строкой для контекста повторного ревью",
    )
    listing.add_argument(
        "--author",
        action="append",
        default=[],
        metavar="LOGIN",
        help="чьи находки идут в контекст; логин приложения с суффиксом [bot]; "
             "повторяемый, обязателен с --resolved-context",
    )

    answer = commands.add_parser("reply", help="ответ в тред; с --resolve сразу закрывает его")
    answer.add_argument("thread")
    text = answer.add_mutually_exclusive_group(required=True)
    text.add_argument("--body")
    text.add_argument("--body-file", dest="body", type=lambda path: Path(path).read_text(encoding="utf-8"),
                      help="файл с текстом ответа: текст не проходит через командную строку")
    answer.add_argument("--resolve", action="store_true")

    closing = commands.add_parser("resolve", help="закрыть тред")
    closing.add_argument("threads", nargs="+")

    args = parser.parse_args()
    if args.command == "list" and args.resolved_context and not args.author:
        fail(msg("threads_need_author"), 2)
    if forge.missing():
        fail(forge.missing(), 3)

    try:
        if args.command == "list":
            items = forge.review_threads(args.pr)
            if items is None:
                fail(msg("threads_pr_missing", forge.change_ref(args.pr), forge.repo()), 2)
            if not args.all and not args.resolved_context:
                items = [thread for thread in items if not thread.resolved]
            if args.resolved_context:
                for line in resolved_context(items, args.author):
                    print(line)
            elif args.json:
                print(json.dumps([dataclasses.asdict(thread) for thread in items], ensure_ascii=False, indent=2))
            elif items:
                show(items)
            else:
                print(msg("threads_none_open"))
            return 0

        if args.command == "reply":
            from authorship import sign
            from text_gate import refusal
            body = sign(args.body)
            if reason := refusal(body):
                fail(reason)
            print(forge.reply_thread(args.thread, body))
            if args.resolve:
                print(msg("threads_resolved", forge.resolve_thread(args.thread)))
            return 0

        for thread in args.threads:
            print(msg("threads_resolved", forge.resolve_thread(thread)))
        return 0
    except forge.ForgeUnavailable as error:
        fail(str(error), 3)
    except forge.ForgeError as error:
        fail(str(error))
