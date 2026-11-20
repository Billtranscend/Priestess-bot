"""日志里不记聊天内容。

NoneBot 会为收到的每个事件记一行日志，消息事件的这一行末尾是完整的消息文字
（"[message.group.normal]: Message 12 from 34@[群:56] '...'"）。journal 会保留好几周，
日志里就会存着每个群的聊天副本。一个 loguru patcher 在任何 handler 看到这行之前把文字换成它的长度；
消息、发送者和群的编号保留，方便排查。适配器没有对应类的消息事件会把全部字段原样记下来，
里面同样有文字：这种行只保留事件名。

最先加载，保证第一个事件到达前 patcher 已经就位。
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
