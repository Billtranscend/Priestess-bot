"""Accept a command typed after a plain-text "@<bot name>".

People copy each other's messages or type the mention by hand, which sends "@Priestess /cmd ..."
as ordinary text instead of a real @ segment. The message then starts with "@", not the command
prefix, and nothing answers. Before commands are matched, a leading text mention of this bot
(its QQ nickname or its card in that group) is removed when a command follows it.
"""

from __future__ import annotations

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
