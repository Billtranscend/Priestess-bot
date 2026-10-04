"""Sign in once right after a first-time Skland binding.

The nightly jobs (00:15 / 00:20) only cover accounts that were already bound, so someone who
binds during the day would miss that day's sign-in. This wraps the binding step (on top of
skland_auto_gacha's hook): when the account did not exist before, both games are signed in the
background and one short line is appended to the "绑定成功" reply. Re-binding, token updates and
/skl角色更新 are not first-time bindings and are left alone.
"""

from __future__ import annotations

import asyncio
import functools
from collections import Counter

from nonebot import logger, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_orm")
require("nonebot_plugin_skland")
require("plugins.skland_auto_gacha")

from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_alconna.uniseg.message import current_send_wrapper
from nonebot_plugin_orm import get_scoped_session
from nonebot_plugin_skland import tasks
from nonebot_plugin_skland.commands import bind as binding
from nonebot_plugin_skland.db_handler import get_arknights_characters, get_endfield_characters
from nonebot_plugin_skland.model import SkUser
from sqlalchemy import inspect

__plugin_meta__ = PluginMetadata(
    name="Skland first-bind sign-in",
    description="首次绑定森空岛后自动签到一次明日方舟和终末地。",
    usage="无指令，首次绑定成功后自动执行。",
    type="application",
)

NOTICE = "\n正在为你签到明日方舟和终末地（仅首次绑定，之后每天自动签到）"
_tasks: set[asyncio.Task] = set()


def _install_notice() -> None:
    """One-shot, current matcher context only; runs after the wrappers installed before it."""
    previous = current_send_wrapper.get(None)
    consumed = False

    async def with_notice(bot, event, message):
        nonlocal consumed
        first_reply = not consumed and isinstance(message, UniMessage) and message.extract_plain_text() == "绑定成功"
        if previous is not None:
            message = await previous(bot, event, message)
        if first_reply:
            consumed = True
            current_send_wrapper.set(previous)
            message = message.copy()
            message += NOTICE
        return message

    current_send_wrapper.set(with_notice)


async def sign_once(user_id: int) -> dict[str, int]:
    """Sign every Arknights / Endfield role of one account; returns counters for the log."""
    stats = Counter()
    session = get_scoped_session()
    try:
        user = await session.get(SkUser, user_id)
        if user is None:
            return dict(stats)
        for character in await get_arknights_characters(user, session):
            result = await tasks._ark_sign_in(user, str(character.uid), character.channel_master_id)
            stats["ark_failed" if isinstance(result, str) and "重复签到" not in result else "ark_ok"] += 1
        for character in await get_endfield_characters(user, session):
            if not character.role_id:
                continue
            result = await tasks._endfield_sign_in(user, character.role_id, character.channel_master_id)
            stats["endfield_failed" if isinstance(result, str) and "重复签到" not in result else "endfield_ok"] += 1
        await session.commit()  # refreshed tokens
    finally:
        await session.close()
    return dict(stats)


async def _sign_in_background(user_id: int) -> None:
    try:
        logger.info(f"Skland first-bind sign-in done: {await sign_once(user_id)}")
    except Exception as e:
        logger.warning(f"Skland first-bind sign-in failed: {type(e).__name__}")


_previous = binding.get_characters_and_bind
if getattr(_previous, "_bind_sign", False):
    raise RuntimeError("Skland first-bind sign-in hook already installed")


@functools.wraps(_previous)
async def _bind_then_sign(user, session):
    state = inspect(user)
    first_time = state.pending or state.transient  # a brand-new row, not a re-bind or token update
    user_id = user.id
    await _previous(user, session)  # commits the binding (and queues the gacha sync)
    if first_time:
        _install_notice()
        task = asyncio.get_running_loop().create_task(_sign_in_background(user_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


_bind_then_sign._bind_sign = True
binding.get_characters_and_bind = _bind_then_sign
