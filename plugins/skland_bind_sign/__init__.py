"""Binding rules and follow-ups on top of nonebot-plugin-skland 0.7.2's multi-account binding.

A member (one QQ) may bind several Skland accounts. Upstream asks for a 「确认」 after showing the
role list on every binding; this project keeps the flow its members know and adds one command:

  /skl绑定       first binding: scan -> 绑定成功, no confirmation.
                 Already bound and the login works: no QR code is handed out. If the Hypergryph
                 token behind the gacha queries has expired, a QR code is given and the member
                 re-scans with the same account, which renews it and keeps every record.
  /skl添加账号   another Skland account for a member who is already bound: scan -> role list
                 -> reply 「确认」 (upstream's confirmation). `skland qrcode --add` underneath.
                 A member with nothing bound yet is pointed to /skl绑定 and gets no QR code.

Rules kept from before, because the QR code is posted in the group and Skland cannot tell who
scanned it:
  - a Skland account belongs to one QQ only;
  - a re-scan through /skl绑定 must be one of the member's own accounts.

Follow-ups of a saved binding:
  - a first binding or an added account is signed in once in the background (the nightly jobs
    only cover accounts bound before midnight);
  - the reply says the gacha records are being fetched and asks the member to wait; until that
    sync has had its first attempt, /zmd抽卡记录 and /zmd抽卡记录更新 answer "please wait";
  - when skland_auto_gacha finishes the sync, the member is told the result where they bound.

And a QR code recall that fails in the protocol client no longer aborts the binding (upstream
recalls the picture between the scan and the save).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import time
from collections import Counter
from contextvars import ContextVar

from nonebot import get_bots, get_driver, logger, require
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
from nonebot_plugin_alconna import Option, Text, UniMessage, command_manager
from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_alconna.uniseg.message import current_send_wrapper
from nonebot_plugin_orm import get_scoped_session, get_session
from nonebot_plugin_skland.api import SklandLoginAPI
from nonebot_plugin_skland.commands import bind as bind_commands
from nonebot_plugin_skland.db_handler import get_account_characters, get_accounts
from nonebot_plugin_skland.exception import (
    AccountIdentityResolutionError,
    BindingStateChangedError,
    DuplicateAccountIdentityError,
    RequestException,
)
from nonebot_plugin_skland.matcher import skland_command
from nonebot_plugin_skland.model import SkUser
from nonebot_plugin_skland.services import binding as binding_service
from nonebot_plugin_skland.services.sign import sign_character
from nonebot_plugin_skland.utils.message import send_reaction
from sqlalchemy import select

from plugins import skland_auto_gacha as auto_gacha
from plugins.skland_roles import owner_id_of

__plugin_meta__ = PluginMetadata(
    name="Skland binding rules",
    description="森空岛绑定规则与绑定后的自动签到、抽卡同步通知；/skl添加账号 绑定更多森空岛账号。",
    usage="/skl绑定　/skl添加账号",
    type="application",
)

BOUND = "绑定成功"
NOTICE = "\n正在为你签到明日方舟和终末地（仅首次绑定，之后每天自动签到）"
ADDED = "\n已添加为你的另一个森空岛账号，正在为新账号签到。发 /skl角色 查看全部角色和序号，发 /skl切换终末地角色 序号 更换默认角色"
RENEWED = "\n登录凭证已更新，之前保存的抽卡记录都还在"
SYNC_NOTICE = "\n正在自动拉取你的终末地抽卡记录，完成后会在这里通知你；收到通知前请先不要发 /zmd抽卡记录 和 /zmd抽卡记录更新"
SYNC_BUSY = "正在自动拉取你的抽卡记录，完成后机器人会在这里通知你，请收到通知后再查看"
HEYBOX_HINT = "\n官方接口只提供近期记录，如果你在小黑盒有往期抽卡记录，可发送 /zmd导入小黑盒 小黑盒ID 同步"
PENDING_FILE = store.get_plugin_data_file("pending.json")
PENDING_TTL = 3 * 3600  # the queue gives up after ~35 min of retries; anything older is stale
_tasks: set[asyncio.Task] = set()
_flow: ContextVar[str | None] = ContextVar("skland_bind_flow", default=None)  # "first" | "renew" | "add" while a binding is saved


# ── reply line ──────────────────────────────────────────────────────────


def _install_notice(notice: str) -> None:
    """One-shot, current matcher context only; runs after the wrappers installed before it."""
    previous = current_send_wrapper.get(None)
    consumed = False

    async def with_notice(bot, event, message):
        nonlocal consumed
        first_reply = not consumed and isinstance(message, UniMessage) and message.extract_plain_text() in (BOUND, "账号更新成功")
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


# ── sign-in after a binding ─────────────────────────────────────────────


async def sign_once(owner_id: int) -> dict[str, int]:
    """Sign every Arknights / Endfield role of the member; returns counters for the log.

    Roles that were already signed today answer 重复签到, which is not a failure.
    """
    stats = Counter()
    session = get_scoped_session()
    try:
        for account in await get_accounts(owner_id, session):
            for character in await get_account_characters(account.id, session):
                if character.app_code not in ("arknights", "endfield") or (character.app_code == "endfield" and not character.role_id):
                    continue
                result = (await sign_character(account, character, character.app_code))["result"]
                failed = isinstance(result, str) and "重复签到" not in result
                stats[f"{'ark' if character.app_code == 'arknights' else 'endfield'}_{'failed' if failed else 'ok'}"] += 1
        await session.commit()  # refreshed tokens
    finally:
        await session.close()
    return dict(stats)


async def _sign_in_background(owner_id: int) -> None:
    try:
        logger.info(f"Skland sign-in after binding done: {await sign_once(owner_id)}")
    except Exception as e:
        logger.warning(f"Skland sign-in after binding failed: {type(e).__name__}")


# ── gacha sync notice ───────────────────────────────────────────────────


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


def _remember_chat(owner_id: int, kind: str = "first") -> None:
    """Note where the binding happened so the sync result can be reported there."""
    event = current_event.get(None)
    if event is None or not hasattr(event, "get_user_id"):
        return
    pending = _load_pending()
    pending[str(owner_id)] = {
        "qq": event.get_user_id(),
        "group_id": event.group_id if isinstance(event, GroupMessageEvent) else None,
        "ts": time.time(),
        "kind": kind,
    }
    _save_pending(pending)


def _forget_chat(owner_id: int) -> None:
    pending = _load_pending()
    if pending.pop(str(owner_id), None) is not None:
        _save_pending(pending)


def _rank_opted_out(qq: str) -> bool:
    try:
        from plugins import skland_gacha_rank

        return qq in skland_gacha_rank._load_optout()
    except Exception:
        return False


async def _on_gacha_synced(owner_id: int, status: str, records: int, final: bool) -> None:
    pending = _load_pending()
    entry = pending.get(str(owner_id))
    if entry is None or not final:  # a failed attempt that will be retried: keep waiting
        return
    pending.pop(str(owner_id))
    _save_pending(pending)
    done = status in ("success", "partial")
    partial = "（有角色拉取失败，可稍后发 /zmd抽卡记录更新 重试）" if status == "partial" else ""
    kind = entry.get("kind") or ("first" if entry.get("first", True) else "renew")  # "first" key: entries written before 0.7.2
    if done and kind == "add":
        text = f"新账号的抽卡记录已同步，新增 {records} 条{partial}。/zmd抽卡记录 看的是默认角色，看其他角色请加 -r 序号（序号见 /skl角色）"
    elif done and kind == "renew":
        text = f"抽卡记录已重新同步，新增 {records} 条{partial}，现在可以发 /zmd抽卡记录 查看"
    elif done and records > 0:
        text = f"拉取抽卡数据成功，获得 {records} 条数据{partial}"
        text += "" if _rank_opted_out(entry["qq"]) else "，现已加入欧非榜统计名单"
        text += HEYBOX_HINT
    elif done:
        text = "拉取抽卡数据完成，暂时没有查到终末地抽卡记录" + partial + HEYBOX_HINT
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

ALREADY_BOUND = "你已经绑定过森空岛了，不用再扫码。角色有变化发 /skl角色更新；想再绑定一个森空岛账号发 /skl添加账号；想解绑发 /skl解绑"
BIND_FIRST = "你还没有绑定过森空岛账号，请先发 /skl绑定 绑定第一个账号；/skl添加账号 是已经绑定之后再加一个账号用的"
BOUND_ELSEWHERE = "这个森空岛账号已经绑定在另一个 QQ 上，没有为你绑定。二维码只能由发指令的本人用自己的森空岛扫；如果那是你的另一个 QQ，请先在那个 QQ 上发 /skl解绑"
OTHER_ACCOUNT = "扫码的森空岛账号和你已绑定的不是同一个，绑定没有改动。续期请用原来的账号扫码；想再绑定一个账号请发 /skl添加账号"
NO_ROLES = "这个森空岛账号下没有可绑定的明日方舟或终末地角色，没有保存"
CHANGED = "绑定数据在这期间发生了变化，没有保存，请重新发送指令"
IDENTITY_FAILED = "现有账号的身份校验失败，请先发送 /skl角色更新，或用 /skl解绑 移除异常的账号"


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


def _arp(state: T_State):
    return getattr(state.get(ALCONNA_RESULT), "result", None)


def _add_requested() -> bool:
    matcher = current_matcher.get(None)
    result = _arp(matcher.state) if matcher else None
    return bool(result and result.find("qrcode.add"))


def _qrcode_scan() -> bool:
    matcher = current_matcher.get(None)
    result = _arp(matcher.state) if matcher else None
    return bool(result and result.find("qrcode"))


@run_preprocessor
async def _no_qrcode_when_bound(matcher: Matcher, bot: Bot, event: MessageEvent, state: T_State) -> None:
    arp = _arp(state)
    if matcher.plugin_name != "nonebot_plugin_skland" or arp is None or not arp.find("qrcode"):
        return
    adding = bool(arp.find("qrcode.add"))
    async with get_session() as session:
        owner_id = await owner_id_of(session, event.get_user_id())
        accounts = list(await session.scalars(select(SkUser).where(SkUser.owner_id == owner_id))) if owner_id is not None else []
        if adding:
            if accounts:
                return  # gets a QR code: the role list has to be confirmed before anything is saved
            # Nothing bound yet: one command for the first account, another for more, so members do not mix them up.
            await bot.send(event, MessageSegment.at(event.get_user_id()) + " " + BIND_FIRST if isinstance(event, GroupMessageEvent) else BIND_FIRST)
            raise IgnoredException("no Skland account bound yet")
        if not accounts:
            return
        renewal = False
        for account in accounts:
            if not await _login_alive(account) or await _gacha_login_expired(account):
                renewal = True
        await session.commit()  # a renewed token
    if renewal:
        logger.info("Skland bind guard: QR code allowed, a login needs renewing")
        return
    await bot.send(event, MessageSegment.at(event.get_user_id()) + " " + ALREADY_BOUND if isinstance(event, GroupMessageEvent) else ALREADY_BOUND)
    raise IgnoredException("already bound to Skland")


# ── saving a binding ────────────────────────────────────────────────────

_upstream_confirm = bind_commands._confirm_account_binding
if getattr(_upstream_confirm, "_bind_sign", False):
    raise RuntimeError("Skland binding rules already installed")
_previous_commit = binding_service.commit_account_binding
if getattr(_previous_commit, "_bind_sign", False):
    raise RuntimeError("Skland binding follow-ups already installed")


@functools.wraps(_previous_commit)
async def _commit_with_followups(prepared, session):
    """On top of skland_auto_gacha's hook: arm the sync notice, extend the reply, sign in."""
    kind, owner_id = _flow.get(None), prepared.owner_id
    if kind:
        try:
            _remember_chat(owner_id, kind)  # before the binding queues the sync, so the result cannot be missed
        except Exception as e:
            logger.warning(f"Skland binding: sync notice not armed: {type(e).__name__}")
    try:
        await _previous_commit(prepared, session)  # commits the binding (and queues the gacha sync)
    except BaseException:
        if kind:
            with contextlib.suppress(Exception):
                _forget_chat(owner_id)  # no sync was queued, so nothing will be reported
        raise
    if not kind:
        return
    syncing = SYNC_NOTICE if auto_gacha.bind_sync_pending(owner_id) else ""
    _install_notice({"first": NOTICE, "renew": RENEWED, "add": ADDED}[kind] + syncing)
    if kind != "renew":
        task = asyncio.get_running_loop().create_task(_sign_in_background(owner_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


@functools.wraps(_upstream_confirm)
async def _confirm(*, owner_id, pending, snapshot, mode, user_session, session) -> None:
    """Decide what a scanned / entered credential may do, then save it with or without upstream's prompt."""
    own_accounts = {account.skland_user_id for account in await get_accounts(owner_id, session)}
    taken = None
    if pending.skland_user_id not in own_accounts:
        taken = await session.scalar(select(SkUser.id).where(SkUser.skland_user_id == pending.skland_user_id, SkUser.owner_id != owner_id).limit(1))
    await session.rollback()
    if taken is not None:
        logger.info("Skland bind guard: refused, the account belongs to another QQ")
        await UniMessage(BOUND_ELSEWHERE).send(at_sender=True)
        return
    own = pending.skland_user_id in own_accounts
    if own_accounts and not own:
        if _qrcode_scan() and not _add_requested():  # a QR code handed out for a renewal, scanned with a different account
            logger.info("Skland bind guard: refused, re-scan with another account")
            await UniMessage(OTHER_ACCOUNT).send(at_sender=True)
            return
        token = _flow.set("add")
        try:
            await _upstream_confirm(owner_id=owner_id, pending=pending, snapshot=snapshot, mode=mode, user_session=user_session, session=session)
        finally:
            _flow.reset(token)
        return
    # First binding, or one of the member's own accounts again: saved without a confirmation.
    try:
        prepared = await binding_service.prepare_account_binding(owner_id, pending, snapshot, session, mode="upsert")
    except AccountIdentityResolutionError:
        await UniMessage(IDENTITY_FAILED).send(at_sender=True)
        return
    except (DuplicateAccountIdentityError, ValueError) as e:
        logger.warning(f"Skland binding not prepared: {type(e).__name__}")
        await UniMessage(IDENTITY_FAILED).send(at_sender=True)
        return
    if prepared.target_account_id is None and not any(role.is_available for role in snapshot.roles):
        await UniMessage(NO_ROLES).send(at_sender=True)
        return
    token = _flow.set("renew" if own else "first")
    try:
        await binding_service.commit_account_binding(prepared, session)
    except BindingStateChangedError:
        await UniMessage(CHANGED).send(at_sender=True)
        return
    else:
        send_reaction(user_session, "done")
        await UniMessage(BOUND).send(at_sender=True)  # the wrappers installed at commit add the follow-up lines
    finally:
        _flow.reset(token)


_commit_with_followups._bind_sign = True
binding_service.commit_account_binding = _commit_with_followups
_confirm._bind_sign = True
bind_commands._confirm_account_binding = _confirm

_qrcode = next(option for option in skland_command.options if getattr(option, "dest", "") == "qrcode")
if not any(getattr(option, "dest", "") == "add" for option in _qrcode.options):
    with command_manager.update(skland_command):
        _qrcode.options.append(Option("--add|add", help_text="为已绑定的成员再绑定一个森空岛账号"))


# ── no gacha commands while the binding's sync runs ─────────────────────


@run_preprocessor
async def _wait_for_bind_sync(matcher: Matcher, bot: Bot, event: MessageEvent, state: T_State) -> None:
    arp = _arp(state)
    if matcher.plugin_name != "nonebot_plugin_skland" or arp is None or not arp.find("efgacha"):
        return
    async with get_session() as session:
        owner_id = await owner_id_of(session, event.get_user_id())
    # Only a sync the member was told to wait for (and will be told the result of).
    if not owner_id or str(owner_id) not in _load_pending() or not auto_gacha.bind_sync_pending(owner_id):
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


@get_driver().on_startup
async def _verify_binding_rules() -> None:
    if bind_commands._confirm_account_binding is not _confirm or binding_service.commit_account_binding is not _commit_with_followups:
        raise RuntimeError("Skland binding rules were replaced")
    prefix = next(iter(skland_command.prefixes), "")
    if not skland_command.parse(f"{prefix}skland qrcode --add").find("qrcode.add"):
        raise RuntimeError("Skland qrcode --add option is not active")
