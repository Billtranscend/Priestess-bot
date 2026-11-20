"""标明合并转发回复是谁的记录，并 @ 这位群友。

多页结果的指令（/zmd抽卡记录、/抽卡记录、干员列表）在群里以聊天记录卡片回复。上游用游戏内昵称
给每页命名，而且在卡片到达之前很久就先发预告，群里一热闹就分不清是谁的卡片。

QQ 不能在这种卡片里或同一条消息里放 @，所以本插件：
- 去掉提前的预告（"…过多，将以多张图片形式发送"、"…将按卡池类型分成多张图片发送"；表情回应已经显示了进度），
- 用该群友在本群的名字给预告过的卡片和每一页加标题（一条指令发几张卡片时每张都加），
- 卡片发出后紧接着发一行 @ 该群友，让两条消息挨在一起；没有预告的卡片（例如 /签到详情）也一样，
  只是不知道是什么记录，统称查询结果。一条指令只 @ 一次，在它的第一张卡片后面。

官方机器人没有聊天记录卡片（桥接把各页逐条发出），那里只吞掉预告，不发“上面的聊天记录”这一行。
私聊和临时会话不处理（见 temp_session_forward）。
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

from plugins.quick_buttons import is_official_bot

__plugin_meta__ = PluginMetadata(
    name="Forward reply owner",
    description="多页结果以聊天记录卡片发送时，卡片标注群昵称，并在卡片后 @ 查询人。",
    usage="无指令，自动生效。",
    type="application",
)

NOTICE = re.compile(r"^(?P<what>.+?)(?:过多，将以多张图片形式发送|将按卡池类型分成多张图片发送)$")
LABELS = {"干员数量": "干员列表"}
UNANNOUNCED = "查询结果"  # 没有预告的卡片：不知道是什么记录
NAME_LIMIT = 20
PREVIEW_LINES = 4  # QQ 卡片上最多显示四行
STALE = 900  # 秒；一条指令的预告、是否已 @ 过查询人，记这么久

_announced: dict[tuple[int, int], tuple[str, float]] = {}  # (群, 指令消息) -> (预告的内容, 时间)
_mentioned: dict[tuple[int, int], float] = {}  # (群, 指令消息) -> 卡片后 @ 过查询人的时间
_sent: dict[tuple[int, int], str] = {}  # (群, 指令消息) -> 卡片发出后 @ 后面的那句话


def _key(event: GroupMessageEvent) -> tuple[int, int]:
    return event.group_id, event.message_id


def _plain(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    try:
        return Message(message).extract_plain_text().strip()
    except Exception:
        return ""


def _forget(now: float) -> None:
    for key in [k for k, (_, at) in _announced.items() if now - at > STALE]:
        del _announced[key]
    for key in [k for k, at in _mentioned.items() if now - at > STALE]:
        del _mentioned[key]


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
            _forget(now)
            _announced[_key(event)] = (match["what"], now)
            raise MockApiException({"message_id": 0})
        return
    if api != "send_group_forward_msg":
        return
    _forget(time.time())
    announced = _announced.get(_key(event))
    nodes = [seg for seg in data.get("messages") or [] if getattr(seg, "type", "") == "node" and "nickname" in seg.data]
    if not nodes:
        return
    label = _title(event, announced[0], nodes, data) if announced else UNANNOUNCED
    if not is_official_bot(bot) and _key(event) not in _mentioned:
        _sent[_key(event)] = f"上面的聊天记录是你的{label}" + (f"，共 {len(nodes)} 张图" if all(map(_picture, nodes)) else "")


def _title(event: GroupMessageEvent, what: str, nodes: list[MessageSegment], data: dict[str, Any]) -> str:
    """用群友在本群的名字给预告过的卡片和每一页加标题，返回卡片的标签（例如“明日方舟抽卡记录”）。"""
    name = _name(event)
    pages = []
    for node in nodes:
        page = str(node.data["nickname"]).rpartition(" | ")[2]  # "<游戏内昵称> | 限定池"，上游写法："<游戏内昵称> | 第 1 页"
        pages.append(page)
        node.data["nickname"] = f"{name} | {page}"
    if what == "抽卡记录":
        label = "终末地抽卡记录" if any("池" in page for page in pages) else "明日方舟抽卡记录"  # 限定池 / 武器池 / 其他卡池
    else:
        label = LABELS.get(what, what)
    # NapCat 为卡片提供的额外字段：标题、预览行、底部一行、聊天列表里显示的文字。
    data["source"] = f"{name} 的{label}"
    data["news"] = [{"text": f"{name}: {page}"} for page in pages[:PREVIEW_LINES]]
    data["summary"] = f"共 {len(nodes)} 张图"
    data["prompt"] = f"[{name} 的{label}]"
    return label


def _picture(node: MessageSegment) -> bool:
    """这一页是图片（不是文字）。"""
    content = node.data.get("content")
    segments = [content] if isinstance(content, (str, MessageSegment)) else content or []
    return any((seg.get("type") if isinstance(seg, dict) else getattr(seg, "type", "")) == "image" for seg in segments)


@Bot.on_called_api
async def _mention_owner(bot: Bot, exception: Exception | None, api: str, data: dict[str, Any], result: Any) -> None:
    if api != "send_group_forward_msg":
        return
    event = current_event.get(None)
    if not isinstance(event, GroupMessageEvent) or _key(event) not in _sent:
        return
    said = _sent.pop(_key(event))
    if exception is not None:
        return
    _mentioned[_key(event)] = time.time()
    try:
        await bot.send_group_msg(group_id=event.group_id, message=MessageSegment.at(event.user_id) + MessageSegment.text(f" {said}"))
    except Exception as e:
        logger.warning(f"Forward owner mention failed: {type(e).__name__}")
