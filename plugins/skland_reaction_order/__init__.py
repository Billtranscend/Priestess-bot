"""Send nonebot-plugin-skland's "done" reaction after the reply has been delivered.

Upstream reacts to the member's command with 喝彩 as soon as the picture is rendered, and only
then uploads it. From this host an upload can take a minute, so members saw "done" long before
anything arrived. Here the reaction is held back until the command's next message (picture, text
or forward card) has been sent. A command that sends nothing after it gets the reaction when it
ends, and when that send fails no "done" is shown at all.

Every module that imported `send_reaction` holds its own reference, so the replacement is put
into each of them once all plugins are loaded; site-packages stays untouched.
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

__plugin_meta__ = PluginMetadata(
    name="Skland reaction order",
    description="森空岛指令的「完成」表情改为在回复发出之后再发送。",
    usage="无指令，自动生效。",
    type="application",
)

SEND_APIS = frozenset({"send_msg", "send_group_msg", "send_private_msg", "send_forward_msg", "send_group_forward_msg", "send_private_forward_msg"})
DONE_FACE = "144"  # 喝彩, the face upstream uses for "done" on QQ
DONE_EMOJI = "\U0001f389"  # what upstream uses on other platforms
_original = skland_message.send_reaction
if getattr(_original, "_after_delivery", False):
    raise RuntimeError("Skland reaction order already installed")
_held: dict[int, object] = {}  # id(running matcher) -> the user session whose "done" is waiting


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
        _original(user_session, "done")  # still inside the command: upstream's own way of sending it


@run_postprocessor
async def _react_when_nothing_was_sent(matcher: Matcher, bot: BaseBot, event: Event, exception: Exception | None) -> None:
    user_session = _held.pop(id(matcher), None)
    if user_session is None or exception is not None:
        return
    try:  # the command's context is gone here, so the event and bot are named
        await message_reaction(DONE_FACE if getattr(user_session, "platform", "") == "QQClient" else DONE_EMOJI, event=event, bot=bot)
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
