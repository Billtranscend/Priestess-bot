"""Say whose record a merged-forward reply is, and mention that member.

Commands with many pages (/zmd抽卡记录, /抽卡记录, the operator box) answer in a group with a
聊天记录 card. Upstream names its pages after the in-game nickname and announces the card
long before it arrives, so in a busy group nobody can tell whose card it is.

QQ cannot put an @ inside such a card or in the same message, so this plugin
- drops the early notice ("…过多，将以多张图片形式发送"; the reaction already shows progress),
- titles the card and its pages with the member's name in this group, and
- sends one line mentioning the member right after the card has gone out, so the two sit together.

Private chats and temporary sessions are left alone (see temp_session_forward).
"""

from __future__ import annotations

import re
import time
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.exception import MockApiException
from nonebot.matcher import current_event
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Forward reply owner",
    description="多页结果以聊天记录卡片发送时，卡片标注群昵称，并在卡片后 @ 查询人。",
    usage="无指令，自动生效。",
    type="application",
)

NOTICE = re.compile(r"^(?P<what>.+?)过多，将以多张图片形式发送$")
LABELS = {"干员数量": "干员列表"}
NAME_LIMIT = 20
PREVIEW_LINES = 4  # QQ shows at most four lines on a card
STALE = 900  # seconds; a command that never produced its card

_announced: dict[tuple[int, int], tuple[str, float]] = {}  # (group, command message) -> what was announced
_sent: dict[tuple[int, int], tuple[str, int]] = {}  # (group, command message) -> (label, pages) of the card on its way


def _key(event: GroupMessageEvent) -> tuple[int, int]:
    return event.group_id, event.message_id


def _plain(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    try:
        return Message(message).extract_plain_text().strip()
    except Exception:
        return ""


def _name(event: GroupMessageEvent) -> str:
    name = (event.sender.card or event.sender.nickname or str(event.user_id)).strip()
    return name if len(name) <= NAME_LIMIT else name[: NAME_LIMIT - 1] + "…"


@Bot.on_calling_api
async def _label_forward(bot: Bot, api: str, data: dict[str, Any]) -> None:
    event = current_event.get(None)
    if not isinstance(event, GroupMessageEvent):
        return
    if api in ("send_msg", "send_group_msg"):
        if match := NOTICE.match(_plain(data.get("message"))):
            now = time.time()
            for key in [k for k, (_, at) in _announced.items() if now - at > STALE]:
                del _announced[key]
            _announced[_key(event)] = (match["what"], now)
            raise MockApiException({"message_id": 0})
        return
    if api != "send_group_forward_msg" or _key(event) not in _announced:
        return
    what, _ = _announced.pop(_key(event))
    nodes = [seg for seg in data.get("messages") or [] if getattr(seg, "type", "") == "node" and "nickname" in seg.data]
    if not nodes:
        return
    name = _name(event)
    pages = []
    for node in nodes:
        page = str(node.data["nickname"]).rpartition(" | ")[2]  # upstream: "<in-game name> | 卡池 1-5"
        pages.append(page)
        node.data["nickname"] = f"{name} | {page}"
    if what == "抽卡记录":
        label = "终末地抽卡记录" if any("卡池" in page for page in pages) else "明日方舟抽卡记录"
    else:
        label = LABELS.get(what, what)
    # NapCat extras for the card: title, preview lines, bottom line, chat-list text.
    data["source"] = f"{name} 的{label}"
    data["news"] = [{"text": f"{name}: {page}"} for page in pages[:PREVIEW_LINES]]
    data["summary"] = f"共 {len(nodes)} 张图"
    data["prompt"] = f"[{name} 的{label}]"
    _sent[_key(event)] = (label, len(nodes))


@Bot.on_called_api
async def _mention_owner(bot: Bot, exception: Exception | None, api: str, data: dict[str, Any], result: Any) -> None:
    if api != "send_group_forward_msg":
        return
    event = current_event.get(None)
    if not isinstance(event, GroupMessageEvent) or _key(event) not in _sent:
        return
    label, count = _sent.pop(_key(event))
    if exception is not None:
        return
    try:
        await bot.send_group_msg(
            group_id=event.group_id,
            message=MessageSegment.at(event.user_id) + MessageSegment.text(f" 上面的聊天记录是你的{label}，共 {count} 张图"),
        )
    except Exception as e:
        logger.warning(f"Forward owner mention failed: {type(e).__name__}")
