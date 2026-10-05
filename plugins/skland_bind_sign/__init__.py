"""Follow-ups for a first-time Skland binding: sign in once, and report the first gacha sync.

The nightly jobs (00:15 / 00:20) only cover accounts that were already bound, so someone who
binds during the day would miss that day's sign-in. This wraps the binding step (on top of
skland_auto_gacha's hook): when the account did not exist before,
  - both games are signed in the background and one short line is appended to the "绑定成功" reply;
  - the chat it happened in is remembered, and when skland_auto_gacha finishes that account's
    first gacha sync the user is told there (with an @ in groups) how many records were fetched.
Re-binding, token updates and /skl角色更新 are not first-time bindings and are left alone, except
that a re-scan is told about its gacha sync as well.

While that sync is still running, the member's /zmd抽卡记录 and /zmd抽卡记录更新 are answered with
a short "please wait" instead of being run: the reply to "绑定成功" asks them to hold on, and a
command sent anyway would only fetch the same records a second time.

It also keeps a binding from being taken over by someone else's scan (the QR code is posted in the
group, and Skland cannot tell who scanned it):
  - a member who is already bound and whose login still works gets no QR code at all, unless
    the Hypergryph token behind the gacha queries has expired (the Skland login outlives it; a
    re-scan with the same account is the only way to renew it, and it keeps every record);
  - a scan may not attach a Skland account that another QQ has already bound;
  - re-scanning after an expired login must be done with the account that was bound before.

And it keeps a scan from being wasted: upstream recalls the QR code between the scan and the
save, so a recall that fails in the protocol client used to abort the binding.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections import Counter

from nonebot import get_bots, logger, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.exception import IgnoredException, MockApiException
from nonebot.matcher import Matcher, current_event, current_matcher
from nonebot.message import run_preprocessor
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State

require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")
require("plugins.skland_auto_gacha")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import Text, UniMessage
from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_alconna.uniseg.message import current_send_wrapper
from nonebot_plugin_orm import get_scoped_session, get_session
from nonebot_plugin_skland import tasks
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.commands import bind as binding
from nonebot_plugin_skland.api import SklandLoginAPI
from nonebot_plugin_skland.db_handler import get_arknights_characters, get_endfield_characters
from nonebot_plugin_skland.exception import RequestException
from nonebot_plugin_skland.model import SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_user.models import Bind
from sqlalchemy import inspect, select

from plugins import skland_auto_gacha as auto_gacha

__plugin_meta__ = PluginMetadata(
    name="Skland first-bind follow-ups",
    description="首次绑定森空岛后自动签到一次，并在首次抽卡记录同步完成后通知本人。",
    usage="无指令，首次绑定成功后自动执行。",
    type="application",
)

NOTICE = "\n正在为你签到明日方舟和终末地（仅首次绑定，之后每天自动签到）"
SYNC_NOTICE = "\n正在自动拉取你的终末地抽卡记录，完成后会在这里通知你；收到通知前请先不要发 /zmd抽卡记录 和 /zmd抽卡记录更新"
RENEWED = "\n登录凭证已更新，之前保存的抽卡记录都还在"
SYNC_BUSY = "正在自动拉取你的抽卡记录，完成后机器人会在这里通知你，请收到通知后再查看"
HEYBOX_HINT = "\n官方接口只提供近期记录，如果你在小黑盒有往期抽卡记录，可发送 /zmd导入小黑盒 小黑盒ID 同步"
PENDING_FILE = store.get_plugin_data_file("pending.json")
PENDING_TTL = 3 * 3600  # the queue gives up after ~35 min of retries; anything older is stale
_tasks: set[asyncio.Task] = set()


# ── reply line ──────────────────────────────────────────────────────────


def _install_notice(notice: str) -> None:
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
            if SYNC_NOTICE in notice:  # says the same more precisely than the queue's own line
                message = UniMessage(Text(seg.text.replace(auto_gacha.BIND_NOTICE, "")) if isinstance(seg, Text) else seg for seg in message)
            message += notice
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


def _remember_chat(user_id: int, first: bool = True) -> None:
    """Note where the binding happened so the sync result can be reported there."""
    event = current_event.get(None)
    if event is None or not hasattr(event, "get_user_id"):
        return
    pending = _load_pending()
    pending[str(user_id)] = {
        "qq": event.get_user_id(),
        "group_id": event.group_id if isinstance(event, GroupMessageEvent) else None,
        "ts": time.time(),
        "first": first,
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
    if status == "success" and not entry.get("first", True):
        text = f"抽卡记录已重新同步，新增 {records} 条，现在可以发 /zmd抽卡记录 查看"
    elif status == "success" and records > 0:
        text = f"拉取抽卡数据成功，获得 {records} 条数据"
        text += "" if _rank_opted_out(entry["qq"]) else "，现已加入欧非榜统计名单"
        text += HEYBOX_HINT
    elif status == "success":
        text = "拉取抽卡数据完成，暂时没有查到终末地抽卡记录" + HEYBOX_HINT
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


# ── whose account is it ─────────────────────────────────────────────────

ALREADY_BOUND = "你已经绑定过森空岛了，不用再扫码。角色有变化发 /skl角色更新；想换绑别的森空岛账号请先发 /skl解绑（会清除已保存的抽卡记录）"
BOUND_ELSEWHERE = "这个森空岛账号已经绑定在另一个 QQ 上，没有为你绑定。二维码只能由发指令的本人用自己的森空岛扫；如果那是你的另一个 QQ，请先在那个 QQ 上发 /skl解绑"
OTHER_ACCOUNT = "扫码的森空岛账号和你原来绑定的不是同一个，绑定没有改动。请用原来的账号扫码；想换绑请先发 /skl解绑"
UNVERIFIED = "暂时无法确认扫码的森空岛账号，绑定没有改动，请稍后再试"


async def _gacha_login_expired(user: SkUser) -> bool:
    """The token the gacha queries need is dead. Anything inconclusive counts as not expired."""
    if not user.access_token:
        return False
    try:
        await SklandLoginAPI.get_grant_code(user.access_token, 1)
    except RequestException as e:
        return "过期" in str(e)
    except Exception as e:
        logger.info(f"Skland bind guard: gacha login check inconclusive ({type(e).__name__})")
    return False


async def _login_alive(user: SkUser) -> bool:
    """Whether the stored login still works (renewed if Skland asks). Unknown counts as alive: no QR code is handed out on a guess."""
    try:
        from plugins import skland_health  # loaded after this plugin

        return (await skland_health._login(user))[0] != "登录已过期"
    except Exception as e:
        logger.info(f"Skland bind guard: login check inconclusive ({type(e).__name__})")
        return True


@run_preprocessor
async def _no_qrcode_when_bound(matcher: Matcher, bot: Bot, event: MessageEvent, state: T_State) -> None:
    arp = getattr(state.get(ALCONNA_RESULT), "result", None)
    if matcher.plugin_name != "nonebot_plugin_skland" or arp is None or not arp.find("qrcode"):
        return
    async with get_session() as session:
        bind_id = (await session.scalars(select(Bind.bind_id).where(Bind.platform == "QQClient", Bind.platform_id == event.get_user_id()))).first()
        user = await session.get(SkUser, bind_id) if bind_id else None
        if user is None:
            return
        alive = await _login_alive(user)
        renewal = alive and await _gacha_login_expired(user)
        await session.commit()  # a renewed token
    if renewal:
        logger.info("Skland bind guard: QR code allowed, the gacha login has expired")
    elif alive:
        await bot.send(event, MessageSegment.at(event.get_user_id()) + " " + ALREADY_BOUND if isinstance(event, GroupMessageEvent) else ALREADY_BOUND)
        raise IgnoredException("already bound to Skland")


async def _account_problem(user: SkUser, session, first_time: bool) -> str:
    """Why this binding must not go through, or "". Called with the scanned / entered credentials already on `user`."""
    if first_time:
        if not user.user_id:
            return ""
        with session.no_autoflush:
            taken = (await session.scalars(select(SkUser.id).where(SkUser.user_id == user.user_id, SkUser.id != user.id))).first()
        return BOUND_ELSEWHERE if taken is not None else ""
    if not user.user_id or not inspect(user).attrs.cred.history.has_changes():
        return ""  # /skl角色更新 and renewed tokens keep the same credential
    try:
        scanned = await SklandAPI.get_user_ID(CRED(cred=user.cred, token=user.cred_token))
    except Exception as e:
        logger.warning(f"Skland bind guard: scanned account not verified ({type(e).__name__})")
        return UNVERIFIED
    return "" if str(scanned) == str(user.user_id) else OTHER_ACCOUNT


# ── no gacha commands while the binding's sync runs ─────────────────────


@run_preprocessor
async def _wait_for_bind_sync(matcher: Matcher, bot: Bot, event: MessageEvent, state: T_State) -> None:
    arp = getattr(state.get(ALCONNA_RESULT), "result", None)
    if matcher.plugin_name != "nonebot_plugin_skland" or arp is None or not arp.find("efgacha"):
        return
    async with get_session() as session:
        bind_id = (await session.scalars(select(Bind.bind_id).where(Bind.platform == "QQClient", Bind.platform_id == event.get_user_id()))).first()
    # Only a sync the member was told to wait for (and will be told the result of).
    if not bind_id or str(bind_id) not in _load_pending() or not auto_gacha.bind_sync_pending(bind_id):
        return
    await bot.send(event, MessageSegment.at(event.get_user_id()) + " " + SYNC_BUSY if isinstance(event, GroupMessageEvent) else SYNC_BUSY)
    raise IgnoredException("the binding's gacha sync is still running")


# ── QR code recall ──────────────────────────────────────────────────────


@Bot.on_called_api
async def _recall_may_fail(bot: Bot, exception: Exception | None, api: str, data: dict, result) -> None:
    """A failed recall inside a Skland command is logged and treated as done.

    NapCat sometimes times out on recallMsg. The code has been scanned or has expired by then,
    so leaving the picture in the chat costs nothing, while the exception cost the binding.
    """
    if api != "delete_msg" or exception is None:
        return
    matcher = current_matcher.get(None)
    if matcher is None or matcher.plugin_name != "nonebot_plugin_skland":
        return
    logger.warning(f"Skland QR code recall failed, the command continues: {type(exception).__name__}")
    raise MockApiException(None)


# ── binding hook ────────────────────────────────────────────────────────

_previous = binding.get_characters_and_bind
if getattr(_previous, "_bind_sign", False):
    raise RuntimeError("Skland first-bind sign-in hook already installed")


@functools.wraps(_previous)
async def _bind_then_sign(user, session):
    state = inspect(user)
    first_time = state.pending or state.transient  # a brand-new row, not a re-bind or token update
    rescan = not first_time and state.attrs.access_token.history.has_changes()  # /skl角色更新 changes no token
    user_id = user.id
    if problem := await _account_problem(user, session, first_time):
        await session.rollback()  # drops the new row / the credentials the scan put on the existing one
        logger.info(f"Skland bind guard: refused ({'first bind' if first_time else 're-bind'})")
        await UniMessage(problem).finish(at_sender=True)
    if first_time or rescan:
        try:
            _remember_chat(user_id, first_time)  # before the binding queues the sync, so the result cannot be missed
        except Exception as e:
            logger.warning(f"Skland first-bind notice not armed: {type(e).__name__}")
    await _previous(user, session)  # commits the binding (and queues the gacha sync)
    syncing = SYNC_NOTICE if (first_time or rescan) and auto_gacha.bind_sync_pending(user_id) else ""
    if rescan:
        _install_notice(RENEWED + syncing)
    if first_time:
        _install_notice(NOTICE + syncing)
        task = asyncio.get_running_loop().create_task(_sign_in_background(user_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


_bind_then_sign._bind_sign = True
binding.get_characters_and_bind = _bind_then_sign
