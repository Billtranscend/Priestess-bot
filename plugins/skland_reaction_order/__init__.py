"""等回复真正送达后，再发出 nonebot-plugin-skland 的“完成”表情回应。

上游一渲染完图片就给成员的指令回应“喝彩”，然后才开始上传。从本机上传可能要一分钟，
所以成员早早看到“完成”，东西却迟迟不到。这里把表情回应压住，直到指令的下一条消息（图片、
文字或转发卡片）发送出去。之后什么都不发的指令在结束时得到回应；发送失败的，就完全不显示“完成”。

每个导入了 `send_reaction` 的模块都持有自己的引用，所以等所有插件加载完后，再把替换版放进
每个模块里；site-packages 不动。
"""

from __future__ import annotations

import sys

from nonebot import get_driver, logger, require
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot
from nonebot.matcher import Matcher, current_matcher
from nonebot.message import run_postprocessor
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_alconna")
require("nonebot_plugin_skland")

from nonebot_plugin_alconna import message_reaction
from nonebot_plugin_skland.utils import message as skland_message
from plugins.shared import REACTION_DONE

__plugin_meta__ = PluginMetadata(
    name="Skland reaction order",
    description="森空岛指令的「完成」表情改为在回复发出之后再发送。",
    usage="无指令，自动生效。",
    type="application",
)

SEND_APIS = frozenset({"send_msg", "send_group_msg", "send_private_msg", "send_forward_msg", "send_group_forward_msg", "send_private_forward_msg"})
DONE_EMOJI = "\U0001f389"  # 上游在 QQ 以外的平台上表示“完成”用的
_original = skland_message.send_reaction
if getattr(_original, "_after_delivery", False):
    raise RuntimeError("Skland reaction order already installed")
_held: dict[int, object] = {}  # id(运行中的 matcher) -> 等着收“完成”的用户会话


def send_reaction(user_session, emoji) -> None:
    matcher = current_matcher.get(None)
    if emoji != "done" or matcher is None:
        _original(user_session, emoji)
        return
    _held[id(matcher)] = user_session


send_reaction._after_delivery = True


@Bot.on_called_api
async def _react_once_delivered(bot: Bot, exception: Exception | None, api: str, data: dict, result) -> None:
    if api not in SEND_APIS:
        return
    matcher = current_matcher.get(None)
    user_session = _held.pop(id(matcher), None) if matcher is not None else None
    if user_session is not None and exception is None:
        _original(user_session, "done")  # 还在指令里面：用上游自己的方式发送


@run_postprocessor
async def _react_when_nothing_was_sent(matcher: Matcher, bot: BaseBot, event: Event, exception: Exception | None) -> None:
    user_session = _held.pop(id(matcher), None)
    if user_session is None or exception is not None:
        return
    try:  # 这里已经没有指令的上下文了，所以要明确给出 event 和 bot
        await message_reaction(REACTION_DONE if getattr(user_session, "platform", "") == "QQClient" else DONE_EMOJI, event=event, bot=bot)
    except Exception as e:
        logger.debug(f"Skland reaction order: late reaction not sent ({type(e).__name__})")


@get_driver().on_startup
async def _install() -> None:
    replaced = []
    for name, module in list(sys.modules.items()):
        if (name == "nonebot_plugin_skland" or name.startswith(("nonebot_plugin_skland.", "plugins."))) and getattr(module, "send_reaction", None) is _original:
            module.send_reaction = send_reaction
            replaced.append(name)
    if "nonebot_plugin_skland.commands.card" not in replaced:
        raise RuntimeError("Skland reaction order could not be installed: upstream no longer imports send_reaction as expected")
    logger.info(f"Skland reaction order: 'done' is sent after delivery in {len(replaced)} modules")
