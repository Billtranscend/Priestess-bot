"""Arknights gacha history like the Endfield one: saved records, a manual update and a daily sync.

Upstream's `skland gacha` always asks the game first and has no sync. Here, as for Endfield:

    /mrfz抽卡记录        the saved records, at once (nothing is asked of the game's record API)
    /mrfz抽卡记录更新    upstream's command: asks the game, saves what is new, shows the page

and every Arknights role of every account a member has bound is synced once a day, in the same 01:00
slot as Endfield (its own queue and worker, the same Queue class). The official API keeps pulls for a
limited time, so what is saved here stays when the game no longer returns it. A member with an Arknights
role and no sync so far is queued within SEEN_SECONDS: that covers new bindings, and every existing
member once after this plugin was installed.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from apscheduler.triggers.cron import CronTrigger
from nonebot import get_driver, logger, on_command, require
from nonebot.adapters import Bot, Message
from nonebot.exception import MatcherException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert

require("nonebot_plugin_skland")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")

from nonebot_plugin_alconna import At, CustomNode, Match, UniMessage
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import async_scoped_session, get_session
from nonebot_plugin_skland import render
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.commands import gacha as gacha_command
from nonebot_plugin_skland.commands.selection import check_user_character
from nonebot_plugin_skland.config import config
from nonebot_plugin_skland.db_handler import get_character_gacha_records
from nonebot_plugin_skland.exception import SklandException
from nonebot_plugin_skland.model import Character, GachaRecord, SkUser
from nonebot_plugin_skland.player_data import get_ark_card
from nonebot_plugin_skland.services.gacha import get_all_gacha_records, group_gacha_records
from nonebot_plugin_skland.utils.message import send_request_error
from nonebot_plugin_user import UserSession, get_user

from plugins.skland_auto_gacha.core import Queue, TZ
from plugins.strict_command import strict

__plugin_meta__ = PluginMetadata(
    name="Skland Arknights gacha",
    description="明日方舟抽卡记录：查看已保存的记录、手动更新，以及每天 01:00 自动同步。",
    usage="/mrfz抽卡记录　/mrfz抽卡记录更新",
    type="application",
)

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data/skland_auto_gacha/ark_queue.sqlite3"
TRIGGER = CronTrigger(hour=1, minute=0, second=0, timezone=TZ)
DAILY_ID = "skland_auto_arkgacha_daily"
WORKER_ID = "skland_auto_arkgacha_worker"
SEEN_ID = "skland_auto_arkgacha_seen"
SEEN_SECONDS = 60
NO_RECORDS = "还没有保存的抽卡记录，请发送 /mrfz抽卡记录更新 拉取数据"
SYNC_FAILED = "抽卡记录拉取失败，请稍后再试。如果一直失败，可能是登录凭证过期了，重新发送 /skl绑定 扫码即可"
USAGE = "用法：/mrfz抽卡记录更新，查看另一个角色时加 -r 序号"
SEVERAL_PICTURES = "抽卡记录过多，将以多张图片形式发送"

_queue: Queue | None = None
_busy = asyncio.Lock()
_active: asyncio.Task | None = None
_stopping = False


# ── the commands ───────────────────────────────────────────────────────

_fetch_and_show = gacha_command.gacha_handler


async def show_saved(
    user_session: UserSession,
    session: async_scoped_session,
    begin: Match[int],
    limit: Match[int],
    target: Match[At | int],
    bot: Bot,
    *,
    role_index: int | None = None,
) -> None:
    """Upstream's page from the saved records alone; the profile on it (avatar, level) still comes from the card."""
    if target.available:
        target_platform_id = target.result.target if isinstance(target.result, At) else target.result
        target_id = (await get_user(user_session.platform, str(target_platform_id))).id
    else:
        target_id = user_session.user_id
    selected = await check_user_character(target_id, user_session, session, app_code="arknights", role_index=role_index)
    if selected is None:  # the reason has been sent
        return
    user, character = selected
    gacha_command.send_reaction(user_session, "processing")
    records = await get_character_gacha_records(character.id, session)
    if not records:
        await session.rollback()
        await UniMessage.text(NO_RECORDS).send(reply_to=True)
        return
    grouped = group_gacha_records(records)
    nickname, channel = character.nickname, character.channel_master_id
    try:
        card = await get_ark_card(user, character)
    except SklandException as error:
        await session.commit()
        await send_request_error(error)
        return
    await session.commit()  # refreshed tokens
    if not card:
        return
    first = begin.result if begin.available else None
    last = limit.result if limit.available else None
    shown = len(grouped.pools[first:last])
    if shown <= config.gacha_render_max:
        page = await render.render_gacha_history(grouped, card.status, nickname=nickname, channel_master_id=channel, begin=first, limit=last)
        await UniMessage.image(raw=page).send()
    else:
        await UniMessage.text(SEVERAL_PICTURES).send(reply_to=True)
        gate = asyncio.Semaphore(4)
        start, step = first or 0, config.gacha_render_max

        async def draw(index: int) -> bytes:
            async with gate:
                return await render.render_gacha_history(grouped, card.status, nickname=nickname, channel_master_id=channel, begin=index, limit=index + step)

        starts = range(start, start + shown, step)
        pages = await asyncio.gather(*(draw(index) for index in starts))
        await UniMessage.reference(
            *(CustomNode(bot.self_id, f"{nickname} | {index}-{min(index + step, start + shown)}", UniMessage.image(raw=page)) for index, page in zip(starts, pages))
        ).send()
    gacha_command.send_reaction(user_session, "done")


gacha_command.gacha_handler = show_saved  # upstream's `skland gacha` imports it when it runs

update_command = on_command("mrfz抽卡记录更新", aliases={"方舟抽卡记录更新", "明日方舟抽卡记录更新"}, rule=strict, priority=5, block=True)


@update_command.handle()
async def _(user_session: UserSession, session: async_scoped_session, bot: Bot, arg: Message = CommandArg()) -> None:
    role = re.fullmatch(r"(?:(?:-r|--role)\s*(\d+))?", arg.extract_plain_text().strip())
    if role is None:
        await update_command.finish(USAGE)
    nothing = Match(None, False)
    try:
        await _fetch_and_show(user_session, session, nothing, nothing, nothing, bot, role_index=int(role[1]) if role[1] else None)
    except MatcherException:
        raise
    except (SklandException, httpx.HTTPError) as e:
        await session.rollback()
        logger.warning(f"Arknights gacha update failed: {type(e).__name__}")
        gacha_command.send_reaction(user_session, "fail")
        await UniMessage.text(SYNC_FAILED).send(at_sender=True)


# ── the sync ───────────────────────────────────────────────────────────


async def sync_user(owner_id: int) -> tuple[str, int]:
    """Sync every Arknights role of the member: ("success" | "partial" | reason, new rows); all roles failing raises, so the queue retries."""
    async with get_session() as session:  # no transaction is held during the requests
        rows = (await session.execute(
            select(Character.id, Character.account_id, Character.uid, SkUser.access_token)
            .join(Character.account).where(SkUser.owner_id == owner_id, Character.app_code == "arknights")
            .order_by(Character.account_id, Character.id))).all()
    if not rows:
        return "no_arknights_character", 0
    roles = [role for role in rows if role.access_token]
    if not roles:
        return "no_access_token", 0
    total, synced, errors, grants = 0, 0, [], {}
    for role in roles:
        try:
            if role.account_id not in grants:
                grants[role.account_id] = await SklandLoginAPI.get_grant_code(role.access_token, 1)
            role_token = await SklandLoginAPI.get_role_token_by_uid(role.uid, grants[role.account_id])
            cookie = await SklandLoginAPI.get_ak_cookie(role_token)
            records = {}
            for category in await SklandAPI.get_gacha_categories(role.uid, role_token, role.access_token, cookie):
                async for record in get_all_gacha_records(role, category, role.access_token, role_token, cookie):
                    records[(record.gacha_ts_sec, record.pos)] = dict(
                        character_id=role.id, pool_id=record.poolId, pool_name=record.poolName, char_id=record.charId,
                        char_name=record.charName, rarity=record.rarity, is_new=record.isNew, gacha_ts=record.gacha_ts_sec, pos=record.pos,
                    )
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(exc)
            logger.warning("auto-arkgacha role_failed error_type={}", type(exc).__name__)
            continue
        async with get_session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))  # against /mrfz抽卡记录更新, unbinding and rebinding
            current = (await session.execute(
                select(SkUser.owner_id, SkUser.access_token, Character.app_code).join(Character.account).where(Character.id == role.id))).first()
            if current is None:
                return "unbound", total
            if tuple(current) != (owner_id, role.access_token, "arknights"):
                return "binding_changed", total
            for values in records.values():
                result = await session.execute(insert(GachaRecord).values(**values).on_conflict_do_nothing(index_elements=["character_id", "gacha_ts", "pos"]))
                total += max(result.rowcount, 0)
            await session.commit()
        synced += 1
    if not synced:
        raise errors[0]
    return ("partial" if errors else "success"), total


async def _owners() -> list[int]:
    async with get_session() as session:
        return list(await session.scalars(select(SkUser.owner_id).join(Character, Character.account_id == SkUser.id).where(Character.app_code == "arknights").distinct()))


async def queue_daily() -> None:
    if _queue is None or _stopping:
        return
    now = datetime.now(timezone.utc)
    if _queue.day_due(now):
        logger.info("auto-arkgacha daily_queued users={}", _queue.enqueue_day(now, await _owners()))


async def queue_unseen() -> None:
    """Members with an Arknights role that were never synced: new bindings, and everyone once at the start."""
    if _queue is None or _stopping:
        return
    with _queue.connection() as db:
        known = {row[0] for row in db.execute("SELECT user_id FROM results UNION SELECT user_id FROM pending")}
    for owner in set(await _owners()) - known:
        _queue.enqueue(owner)


async def poll_queue() -> None:
    global _active
    if not _stopping and (_active is None or _active.done()):
        _active = asyncio.create_task(drain_queue(), name="skland-auto-arkgacha")


async def drain_queue() -> None:
    global _active
    if _queue is None or _busy.locked() or _stopping:
        return
    async with _busy:
        _active = asyncio.current_task()
        try:
            await queue_daily()  # also catches up a slot missed while the bot was down
            while not _stopping and (item := _queue.next()) is not None:
                try:
                    async with asyncio.timeout(600):
                        status, records = await sync_user(item[0])
                except asyncio.CancelledError:
                    raise  # the item stays queued for the next start
                except Exception as exc:
                    _queue.finish(item, "failed", retry=True)
                    logger.warning("auto-arkgacha update_failed reason={} attempt={} error_type={}", item[2], item[3] + 1, type(exc).__name__)
                else:
                    _queue.finish(item, status, records)
                    logger.info("auto-arkgacha update_done reason={} status={} new_records={}", item[2], status, records)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("auto-arkgacha worker_failed error_type={}", type(exc).__name__)
        finally:
            _active = None


async def _daily_tick() -> None:
    try:
        await queue_daily()
    except Exception as exc:
        logger.error("auto-arkgacha daily_queue_failed error_type={}", type(exc).__name__)


async def _unseen_tick() -> None:
    try:
        await queue_unseen()
    except Exception as exc:
        logger.error("auto-arkgacha unseen_queue_failed error_type={}", type(exc).__name__)


@get_driver().on_startup
async def _start() -> None:
    global _queue, _stopping
    _queue = Queue(STATE)
    _queue.initialize_day(datetime.now(timezone.utc))
    _stopping = False
    scheduler.add_job(_daily_tick, TRIGGER, id=DAILY_ID, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=86400)
    scheduler.add_job(poll_queue, "interval", seconds=10, id=WORKER_ID, replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(_unseen_tick, "interval", seconds=SEEN_SECONDS, id=SEEN_ID, replace_existing=True, max_instances=1, coalesce=True)


@get_driver().on_shutdown
async def _stop() -> None:
    global _stopping
    _stopping = True
    if _active is not None and _active is not asyncio.current_task():
        _active.cancel()
        await asyncio.gather(_active, return_exceptions=True)
