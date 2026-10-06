"""Sanity (理智) status and full-sanity group reminders for Arknights and Endfield.

/理智              show current sanity for both games
/理智提醒 开|关     subscribe in this group; the bot @s you here when sanity is full

A background job runs every 10 minutes but only calls Skland when a stored
full-time is due or the last check is older than REFRESH_SECONDS. A reminder is
sent only after a fresh check confirms sanity is full, once per full cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from nonebot import get_bots, logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_skland")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_user")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import message_reaction
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.exception import LoginException, RequestException, UnauthorizedException
from nonebot_plugin_skland.model import SkUser
from nonebot_plugin_skland.schemas import CRED

from plugins.skland_roles import default_role, owner_id_of
from sqlalchemy import select

__plugin_meta__ = PluginMetadata(
    name="Sanity reminder",
    description="Shows Arknights/Endfield sanity and @s subscribers in their group when it is full.",
    usage="/理智 | /理智提醒 开 | /理智提醒 关",
    type="application",
)

PLATFORM = "QQClient"
GAMES = {"arknights": "明日方舟", "endfield": "终末地"}
CHECK_MINUTES = 10
REFRESH_SECONDS = 3 * 3600
DUE_MARGIN = 120  # re-check this many seconds before the predicted full time
REQUEST_GAP = 2.0
SUBS_FILE = store.get_plugin_data_file("subscriptions.json")
REACTION_DONE = "144"

sanity = on_command("理智", rule=strict, priority=5, block=True)
reminder = on_command("理智提醒", rule=strict, priority=5, block=True)
_lock = asyncio.Lock()


@dataclass
class Sanity:
    current: int
    maximum: int
    full_ts: float

    @property
    def is_full(self) -> bool:
        return self.current >= self.maximum or time.time() >= self.full_ts


def _load() -> dict:
    if not SUBS_FILE.exists():
        return {}
    return json.loads(SUBS_FILE.read_text("utf-8"))


def _save(subs: dict) -> None:
    tmp = SUBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs, ensure_ascii=False), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(SUBS_FILE)


async def _call_with_refresh(user: SkUser, fetch):
    """Run a Skland request, refreshing tokens like Skland does but never sending chat messages."""
    try:
        return await fetch(CRED(cred=user.cred, token=user.cred_token))
    except UnauthorizedException:
        user.cred_token = await SklandLoginAPI.refresh_token(user.cred)
    except LoginException:
        if not user.access_token:
            raise
        grant_code = await SklandLoginAPI.get_grant_code(user.access_token, 0)
        new_cred = await SklandLoginAPI.get_cred(grant_code)
        user.cred, user.cred_token = new_cred.cred, new_cred.token
    return await fetch(CRED(cred=user.cred, token=user.cred_token))


async def _fetch(session, owner_id: int, game: str) -> Sanity | None:
    """Sanity of the member's default role in one game (None: no such role)."""
    selected = await default_role(session, owner_id, game)
    if selected is None:
        return None
    user, char = selected
    if game == "arknights":
        card = await _call_with_refresh(user, lambda cred: SklandAPI.ark_card(cred, str(char.uid)))
        ap = card.status.ap
        return Sanity(ap.ap_now if ap.current < ap.max else ap.current, ap.max, float(ap.completeRecoveryTime))
    card = await _call_with_refresh(
        user, lambda cred: SklandAPI.endfield_card(cred, user_id=user.skland_user_id, role_id=char.role_id, server_id=char.channel_master_id)
    )
    dungeon = card.dungeon
    cur, mx = int(dungeon.curStamina or 0), int(dungeon.maxStamina or 0)
    return Sanity(cur, mx, float(dungeon.maxTs) if dungeon.maxTs else time.time())


async def _member_for_qq(session, qq: str) -> int | None:
    """The member id of a QQ that has at least one Skland account bound."""
    owner_id = await owner_id_of(session, qq)
    if owner_id is None or (await session.scalars(select(SkUser.id).where(SkUser.owner_id == owner_id).limit(1))).first() is None:
        return None
    return owner_id


def _remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}小时{seconds % 3600 // 60}分"


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


@sanity.handle()
async def _(event: MessageEvent) -> None:
    lines = []
    async with get_session() as session:
        user = await _member_for_qq(session, str(event.user_id))
        if user is None:
            await sanity.finish("未绑定森空岛账号，请先发送 /skl绑定", at_sender=True)
        for game, label in GAMES.items():
            try:
                state = await _fetch(session, user, game)
            except (RequestException, LoginException, UnauthorizedException) as e:
                lines.append(f"{label}：查询失败（{e.args[0] if e.args else type(e).__name__}）")
                continue
            if state is None:
                continue
            note = "已回满" if state.is_full else f"约 {_remaining(state.full_ts - time.time())}后回满"
            lines.append(f"{label}：理智 {min(state.current, state.maximum)}/{state.maximum}，{note}")
        await session.commit()
    await sanity.finish("\n".join(lines) or "没有找到已绑定的明日方舟或终末地角色", at_sender=True)


@reminder.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    qq = str(event.user_id)
    if action not in ("开", "关"):
        await reminder.finish("用法：/理智提醒 开  或  /理智提醒 关")

    async with _lock:
        subs = _load()
        if action == "关":
            removed = subs.pop(qq, None)
            _save(subs)
            await _react(REACTION_DONE)
            await reminder.finish("已关闭理智回满提醒" if removed else "你还没有开启理智提醒", at_sender=True)

        if not isinstance(event, GroupMessageEvent):
            await reminder.finish("请在群里发送 /理智提醒 开，回满时会在该群 @你")
        async with get_session() as session:
            user = await _member_for_qq(session, qq)
            if user is None:
                await reminder.finish("未绑定森空岛账号，请先发送 /skl绑定", at_sender=True)
            games = [g for g in GAMES if await default_role(session, user, g) is not None]
        if not games:
            await reminder.finish("没有找到已绑定的明日方舟或终末地角色", at_sender=True)
        subs[qq] = {"group": event.group_id, "games": {g: {"full_ts": None, "checked": 0, "notified": False} for g in games}}
        _save(subs)
    await _react(REACTION_DONE)
    names = "、".join(GAMES[g] for g in games)
    await reminder.finish(f"已开启理智回满提醒（{names}），回满时会在本群 @你。关闭请发 /理智提醒 关", at_sender=True)


def _bot() -> Bot | None:
    return next((b for b in get_bots().values() if isinstance(b, Bot)), None)


@scheduler.scheduled_job("interval", minutes=CHECK_MINUTES, id="sanity_reminder_check")
async def _check() -> None:
    if _lock.locked():
        return
    async with _lock:
        subs = _load()
        if not subs:
            return
        changed = False
        async with get_session() as session:
            for qq, sub in subs.items():
                for game, st in sub["games"].items():
                    now = time.time()
                    due = st["full_ts"] is not None and now >= st["full_ts"] - DUE_MARGIN
                    if not (due or now - st["checked"] >= REFRESH_SECONDS):
                        continue
                    try:
                        user = await _member_for_qq(session, qq)
                        state = await _fetch(session, user, game) if user is not None else None
                    except Exception as e:
                        logger.warning(f"Sanity check failed for one {game} subscriber: {type(e).__name__}")
                        st["checked"] = now
                        changed = True
                        continue
                    finally:
                        await asyncio.sleep(REQUEST_GAP)
                    st["checked"], changed = now, True
                    if state is None:
                        continue
                    if not state.is_full:
                        st["full_ts"], st["notified"] = state.full_ts, False
                        continue
                    st["full_ts"] = None
                    if st["notified"]:
                        continue
                    bot = _bot()
                    if bot is None:
                        continue
                    try:
                        await bot.send_group_msg(
                            group_id=sub["group"],
                            message=MessageSegment.at(int(qq))
                            + f" 你的{GAMES[game]}理智已回满（{state.maximum}/{state.maximum}），记得上线使用～",
                        )
                        st["notified"] = True
                    except Exception as e:
                        logger.warning(f"Sanity reminder send failed: {type(e).__name__}")
            await session.commit()
        if changed:
            _save(subs)
