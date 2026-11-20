"""指令进来之前统一写法，让各个指令不用各自处理。

- 写在纯文字 "@<机器人名>" 后面的指令：群友互相复制消息或手打 @ 时，"@Priestess /cmd ..." 会作为
  普通文字发出，而不是真正的 @ 消息段，没有任何指令会响应。如果开头是对本机器人的文字 @（它的 QQ
  昵称或在该群的群名片），且后面跟着指令，就把这段文字 @ 去掉。
- 选角色的中文写法：指令后面的 "角色2" / "角色 2"（紧挨着指令也行）换成各指令认的 " -r 2"（/skl角色 卡片上的序号）。
  森空岛账号管理的指令（/skl角色、/skl切换终末地角色 2 ……）本身带“角色”二字，不换。
- 去掉最后一个词后面的空白：按整句匹配的快捷指令（"/zmd抽卡记录 限定池"、"/mrfz抽卡记录 第2页"、"-r 2"）
  带了空格就认不出；结尾的 @机器人 被适配器删掉后，它前面的空格还留着。规则同桥接的 without_trailing_space
  （结尾连着几段文字时逐段去；官方机器人上桥接已去过一次，再去一次没有影响）；只改 event.get_message()，原文留给按钮比对用。
"""

from __future__ import annotations

import re
import time

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.message import event_preprocessor
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Command tolerance",
    description="识别开头的文字版「@机器人」，让其后的指令照常生效。",
    usage="无指令。",
    type="application",
)

CARD_TTL = 3600
ROLE_WORD = re.compile(r"\s*角色\s*(\d+)(?=\s|$)")
ACCOUNT_COMMANDS = re.compile(r"^\s*/\s*(?:森空岛|skl|skd|sk|skland)", re.I)
_nicknames: dict[str, str] = {}
_cards: dict[tuple[str, int], tuple[float, str]] = {}


async def _names(bot: Bot, event: MessageEvent) -> list[str]:
    if bot.self_id not in _nicknames:
        _nicknames[bot.self_id] = str((await bot.get_login_info()).get("nickname") or "")
    names = {_nicknames[bot.self_id], *get_driver().config.nickname}
    if isinstance(event, GroupMessageEvent):
        key = (bot.self_id, event.group_id)
        cached = _cards.get(key)
        if cached is None or time.time() - cached[0] > CARD_TTL:
            info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(bot.self_id))
            cached = _cards[key] = (time.time(), str(info.get("card") or ""))
        names.add(cached[1])
    return sorted((n for n in names if n), key=len, reverse=True)


@event_preprocessor
async def _tolerate(bot: Bot, event: MessageEvent) -> None:
    """一个预处理器、固定顺序：NoneBot 会同时运行各个预处理器，分开写的话“角色2”的改写可能抢在
    去掉打字的 @机器人名 之前，那时文字还不是以 / 开头，就不改写了。"""
    await _strip_text_mention(bot, event)
    _role_in_words(event)
    _without_trailing_space(event)


async def _strip_text_mention(bot: Bot, event: MessageEvent) -> None:
    message = event.get_message()
    if not message or message[0].type != "text":
        return
    text = str(message[0].data.get("text", "")).lstrip()
    if not text.startswith("@"):
        return
    starts = tuple(s for s in get_driver().config.command_start if s)
    try:
        names = await _names(bot, event)
    except Exception as e:
        logger.warning(f"Command tolerance: bot names unavailable ({type(e).__name__})")
        return
    for name in names:
        rest = text[1 + len(name) :]
        if text[1:].startswith(name) and starts and rest.lstrip().startswith(starts):
            message[0].data["text"] = rest.lstrip()
            event.to_me = True
            return


def _role_in_words(event: MessageEvent) -> None:
    """"/zmd开盒 角色2" -> "/zmd开盒 -r 2"。"""
    message = event.get_message()
    if not message or message[0].type != "text":
        return
    first = str(message[0].data.get("text", ""))
    if not first.lstrip().startswith("/") or ACCOUNT_COMMANDS.match(first):
        return
    for segment in message:
        if segment.type == "text":
            segment.data["text"] = ROLE_WORD.sub(r" -r \1", str(segment.data.get("text", "")))


def _without_trailing_space(event: MessageEvent) -> None:
    """"/zmd抽卡记录 限定池 " -> "/zmd抽卡记录 限定池"：最后一段是文字就去掉末尾空白，去空了且还有别的段就删掉这段，
    新的最后一段是文字的话接着去。"""
    message = event.get_message()
    while message and message[-1].type == "text":
        text = str(message[-1].data.get("text", "")).rstrip()
        if text or len(message) == 1:
            message[-1].data["text"] = text
            return
        message.pop()
