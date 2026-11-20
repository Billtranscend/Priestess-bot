"""合并转发回复留在发起请求的私聊里。

在群临时会话里发的指令是私聊消息，但仍带着群号。nonebot-plugin-alconna 优先按 `group_id`
选发送目标，于是由转发节点组成的回复（例如多页的 /zmd抽卡记录）会走 send_group_forward_msg：
请求者的记录被发到了群里，而群里根本看不到那条指令。普通回复不受影响（它们走 bot.send，
会遵守 message_type）。

这个钩子在处理私聊消息期间拦下这次调用，改为把每个节点的内容逐条发给请求者。
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

NODE_GAP = 1.0  # 每页之间间隔的秒数


def _content(node: Any) -> Message | None:
    """取出一个转发节点携带的消息，不管调用方用哪种形式构造的。"""
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
