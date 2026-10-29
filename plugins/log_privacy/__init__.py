"""Keep chat text out of the log.

NoneBot logs every event it receives, and for a message that line ends with the whole message
text ("[message.group.normal]: Message 12 from 34@[群:56] '...'"). The journal keeps it for weeks,
so the log would hold a copy of every group's chat. A loguru patcher replaces the text with its
length before any handler sees the line; the message, sender and group ids stay for debugging.
A message event the adapter has no class for is logged as its whole field dump, which carries
the text too: that line keeps its event name only.

Loaded first, so the patcher is in place before the first event arrives.
"""

from __future__ import annotations

import re

from nonebot import logger
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Log privacy",
    description="日志里不记录聊天内容，只保留消息编号、发送者与群号。",
    usage="无指令，自动生效。",
    type="application",
)

EVENT = re.compile(r"^(?P<head>.*? \| \[message(?:_sent)?\.[\w.]*\]: )(?P<rest>.*)$", re.S)
DESCRIBED = re.compile(r"^(?P<who>Message -?\d+ from \S+) (?P<text>.*)$", re.S)


def _hide_text(record) -> None:
    if (event := EVENT.match(record["message"])) is None:
        return
    if described := DESCRIBED.match(event["rest"]):
        record["message"] = f"{event['head']}{described['who']} <text, {len(described['text'])} chars>"
    else:
        record["message"] = f"{event['head']}<fields omitted>"


logger.configure(patcher=_hide_text)
