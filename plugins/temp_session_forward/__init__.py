"""Keep merged-forward replies inside the private chat they were asked for in.

A command sent in a group's temporary session (群临时会话) is a private message that still carries
the group's id. nonebot-plugin-alconna picks its send target from `group_id` first, so a reply
made of forward nodes (e.g. a /zmd抽卡记录 with several pages) went out with
send_group_forward_msg: the requester's record was posted in the group, with no visible command.
Plain replies were not affected (they go through bot.send, which honours message_type).

This hook catches that call while a private message is being handled and sends each node's
content to the requester instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment, PrivateMessageEvent
from nonebot.exception import MockApiException
from nonebot.matcher import current_event
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Private forward replies",
    description="临时会话 / 私聊里触发的合并转发回复只发给本人，不会发到群里。",
    usage="无指令，自动生效。",
    type="application",
)

NODE_GAP = 1.0  # seconds between the pages


def _content(node: Any) -> Message | None:
    """The message carried by one forward node, whatever form the caller built it in."""
    data = node.get("data") if isinstance(node, dict) else getattr(node, "data", None)
    content = (data or {}).get("content")
    if not content:
        return None
    if isinstance(content, (str, MessageSegment)):
        return Message(content)
    if isinstance(content, Message):
        return content
    return Message([MessageSegment(type=seg["type"], data=seg.get("data") or {}) if isinstance(seg, dict) else seg for seg in content])


@Bot.on_calling_api
async def _keep_forward_private(bot: Bot, api: str, data: dict[str, Any]) -> None:
    if api != "send_group_forward_msg":
        return
    event = current_event.get(None)
    if not isinstance(event, PrivateMessageEvent):
        return
    pages = [content for node in data.get("messages") or [] if (content := _content(node))]
    logger.info(f"Forward reply to a private session kept private: {len(pages)} pages instead of a group forward")
    for index, page in enumerate(pages):
        if index:
            await asyncio.sleep(NODE_GAP)
        await bot.send(event, page)
    raise MockApiException({"message_id": 0})
