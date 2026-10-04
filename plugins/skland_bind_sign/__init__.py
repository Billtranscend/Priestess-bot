"""Follow-ups for a first-time Skland binding: sign in once, and report the first gacha sync.

The nightly jobs (00:15 / 00:20) only cover accounts that were already bound, so someone who
binds during the day would miss that day's sign-in. This wraps the binding step (on top of
skland_auto_gacha's hook): when the account did not exist before,
  - both games are signed in the background and one short line is appended to the "绑定成功" reply;
  - the chat it happened in is remembered, and when skland_auto_gacha finishes that account's
    first gacha sync the user is told there (with an @ in groups) how many records were fetched.
Re-binding, token updates and /skl角色更新 are not first-time bindings and are left alone.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections import Counter

from nonebot import get_bots, logger, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.matcher import current_event
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")
require("plugins.skland_auto_gacha")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_alconna.uniseg.message import current_send_wrapper
from nonebot_plugin_orm import get_scoped_session
from nonebot_plugin_skland import tasks
from nonebot_plugin_skland.commands import bind as binding
from nonebot_plugin_skland.db_handler import get_arknights_characters, get_endfield_characters
from nonebot_plugin_skland.model import SkUser
from sqlalchemy import inspect

from plugins import skland_auto_gacha as auto_gacha

__plugin_meta__ = PluginMetadata(
    name="Skland first-bind follow-ups",
    description="首次绑定森空岛后自动签到一次，并在首次抽卡记录同步完成后通知本人。",
    usage="无指令，首次绑定成功后自动执行。",
    type="application",
)

NOTICE = "\n正在为你签到明日方舟和终末地（仅首次绑定，之后每天自动签到）"
PENDING_FILE = store.get_plugin_data_file("pending.json")
PENDING_TTL = 3 * 3600  # the queue gives up after ~35 min of retries; anything older is stale
_tasks: set[asyncio.Task] = set()


# ── reply line ──────────────────────────────────────────────────────────


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


# ── first sign-in ───────────────────────────────────────────────────────


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


# ── first gacha sync notice ─────────────────────────────────────────────


def _load_pending() -> dict[str, dict]:
    try:
        data = json.loads(PENDING_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {uid: entry for uid, entry in data.items() if time.time() - entry.get("ts", 0) < PENDING_TTL}


def _save_pending(pending: dict[str, dict]) -> None:
    PENDING_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(pending), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(PENDING_FILE)


def _remember_chat(user_id: int) -> None:
    """Note where the binding happened so the sync result can be reported there."""
    event = current_event.get(None)
    if event is None or not hasattr(event, "get_user_id"):
        return
    pending = _load_pending()
    pending[str(user_id)] = {
        "qq": event.get_user_id(),
        "group_id": event.group_id if isinstance(event, GroupMessageEvent) else None,
        "ts": time.time(),
    }
    _save_pending(pending)


def _rank_opted_out(qq: str) -> bool:
    try:
        from plugins import skland_gacha_rank

        return qq in skland_gacha_rank._load_optout()
    except Exception:
        return False


async def _on_gacha_synced(user_id: int, status: str, records: int, final: bool) -> None:
    pending = _load_pending()
    entry = pending.get(str(user_id))
    if entry is None or not final:  # a failed attempt that will be retried: keep waiting
        return
    pending.pop(str(user_id))
    _save_pending(pending)
    if status == "success" and records > 0:
        text = f"拉取抽卡数据成功，获得 {records} 条数据"
        text += "" if _rank_opted_out(entry["qq"]) else "，现已加入欧非榜统计名单"
    elif status == "success":
        text = "拉取抽卡数据完成，暂时没有查到终末地抽卡记录"
    elif status == "failed":
        text = "抽卡数据拉取失败，可稍后发送 /zmd抽卡记录更新 重试"
    else:  # no Endfield role, no token, unbound or re-bound meanwhile: nothing worth saying
        return
    bot = next((b for b in get_bots().values() if isinstance(b, Bot)), None)
    if bot is None:
        return
    if entry.get("group_id"):
        await bot.send_group_msg(group_id=int(entry["group_id"]), message=MessageSegment.at(entry["qq"]) + " " + text)
    else:
        await bot.send_private_msg(user_id=int(entry["qq"]), message=text)


if _on_gacha_synced not in auto_gacha.sync_listeners:
    auto_gacha.sync_listeners.append(_on_gacha_synced)


# ── binding hook ────────────────────────────────────────────────────────

_previous = binding.get_characters_and_bind
if getattr(_previous, "_bind_sign", False):
    raise RuntimeError("Skland first-bind sign-in hook already installed")


@functools.wraps(_previous)
async def _bind_then_sign(user, session):
    state = inspect(user)
    first_time = state.pending or state.transient  # a brand-new row, not a re-bind or token update
    user_id = user.id
    if first_time:
        try:
            _remember_chat(user_id)  # before the binding queues the sync, so the result cannot be missed
        except Exception as e:
            logger.warning(f"Skland first-bind notice not armed: {type(e).__name__}")
    await _previous(user, session)  # commits the binding (and queues the gacha sync)
    if first_time:
        _install_notice()
        task = asyncio.get_running_loop().create_task(_sign_in_background(user_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


_bind_then_sign._bind_sign = True
binding.get_characters_and_bind = _bind_then_sign
