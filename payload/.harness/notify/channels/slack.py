#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))

from manifest import setting  # noqa: E402
from platform_messages import msg  # noqa: E402

CHANNEL = "slack"
ICONS = {"release": ":white_check_mark:", "release-failure": ":x:", "rollback": ":leftwards_arrow_with_hook:",
         "rollback-failure": ":x:"}
LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
EXTENSION = f"notifications/{CHANNEL}"
API = "https://slack.com/api/"
HISTORY_SCOPES = "channels:read, channels:history"
TIMEOUT = 30
SKIPPED = 3


class Refused(RuntimeError):
    pass


def call(token: str, method: str, message: dict | None = None, **query: object) -> dict:
    url = API + method + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"Authorization": "Bearer " + token}
    data = None
    if message is not None:
        data = json.dumps(message).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=TIMEOUT) as response:
            answer = json.load(response)
    except (OSError, ValueError) as error:
        raise Refused(f"{method}: {error}") from None
    if not answer.get("ok"):
        raise Refused(f"{method}: {answer.get('error')}")
    return answer


def thread(token: str, channel: str) -> str:
    try:
        pattern = re.compile(os.environ["NOTIFY_SLACK_THREAD_MATCH"])
    except re.error as error:
        raise Refused(f"NOTIFY_SLACK_THREAD_MATCH: {error}") from None
    name = channel.lstrip("#")
    listed = call(token, "conversations.list", types="public_channel", exclude_archived="true", limit=1000)
    found = [item["id"] for item in listed.get("channels", []) if item.get("name") == name]
    if not found:
        raise Refused(f"conversations.list: {name}")
    history = call(token, "conversations.history", channel=found[0], limit=200,
                   oldest=os.environ.get("NOTIFY_SLACK_THREAD_SINCE") or "0")
    for message in history.get("messages", []):
        texts = [message.get("text", "")] + [
            attachment.get(field, "") for attachment in message.get("attachments", []) for field in ("title", "fallback")
        ]
        if any(pattern.search(text or "") for text in texts):
            return message["ts"]
    return ""


def mrkdwn(text: str) -> str:
    return BOLD.sub(r"*\1*", LINK.sub(r"<\2|\1>", text))


def heading(title: str) -> str:
    icon = ICONS.get(os.environ.get("NOTIFY_STATUS", ""), "")
    return f"*{icon} {title}*" if icon else f"*{title}*"


def main() -> int:
    token = setting(EXTENSION, "token", "")
    if not token:
        return SKIPPED
    channel = os.environ.get("NOTIFY_SLACK_CHANNEL") or setting(EXTENSION, "channel", "")
    message = {"channel": channel, "text": f"{heading(os.environ['NOTIFY_TITLE'])}\n\n{mrkdwn(os.environ['NOTIFY_BODY'])}"}
    if os.environ.get("NOTIFY_SLACK_THREAD_MATCH"):
        try:
            parent = thread(token, channel)
        except Refused as error:
            print(error, file=sys.stderr)
            parent = ""
        if parent:
            message["thread_ts"] = parent
        else:
            print(msg("notify_no_thread", channel, HISTORY_SCOPES), file=sys.stderr)
    try:
        call(token, "chat.postMessage", message)
    except Refused as error:
        print(msg("notify_rejected", CHANNEL, error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
