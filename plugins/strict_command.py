"""Shared rule: a command must be followed by the end, whitespace, or a non-text segment.

Stops accidental triggers like "/欧非榜真好玩" while still accepting "/账号详情@某人",
where QQ often puts the @ segment directly after the command text.
"""

from nonebot.adapters import Event
from nonebot.consts import PREFIX_KEY, RAW_CMD_KEY
from nonebot.rule import Rule
from nonebot.typing import T_State


async def _command_ends_cleanly(event: Event, state: T_State) -> bool:
    message = event.get_message()
    if not message or message[0].type != "text":
        return True
    raw = state.get(PREFIX_KEY, {}).get(RAW_CMD_KEY) or ""
    text = str(message[0]).lstrip()
    if not raw or not text.startswith(raw):
        return True
    rest = text[len(raw) :]
    return not rest or rest[0].isspace()


strict = Rule(_command_ends_cleanly)
