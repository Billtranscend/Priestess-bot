"""公用规则：指令后面必须是消息结尾、空白或非文字消息段。

防止 "/欧非榜真好玩" 这类误触发，同时仍接受 "/zmd开盒@某人"——QQ 经常把 @ 消息段直接接在指令文字后面。
个别指令允许选项紧贴指令名（"/卫3"、"/zmd开盒-r 2"），用 command_ends_cleanly 的参数说明哪些开头算选项。
"""

from nonebot.adapters import Event
from nonebot.consts import PREFIX_KEY, RAW_CMD_KEY
from nonebot.rule import Rule
from nonebot.typing import T_State


def command_ends_cleanly(event: Event, state: T_State, glued: tuple[str, ...] = (), digits: bool = False) -> bool:
    """指令文字后面没有多余的字。glued：可以紧贴指令名的选项开头（小写，比较时不分大小写）；
    digits：紧贴的数字也算（"/卫3"、"/zmd导入小黑盒12345678"）。"""
    message = event.get_message()
    if not message or message[0].type != "text":
        return True
    raw = state.get(PREFIX_KEY, {}).get(RAW_CMD_KEY) or ""
    text = str(message[0]).lstrip()
    if not raw or not text.startswith(raw):
        return True
    rest = text[len(raw) :]
    return not rest or rest[0].isspace() or (digits and rest[0].isdigit()) or rest.lower().startswith(glued)


async def _command_ends_cleanly(event: Event, state: T_State) -> bool:
    return command_ends_cleanly(event, state)


strict = Rule(_command_ends_cleanly)
