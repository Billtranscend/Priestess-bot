"""Background Endfield history updates after binding and daily in Asia/Shanghai."""
from __future__ import annotations

import asyncio
import functools
import hashlib
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from nonebot import get_driver, logger, require
from nonebot.plugin import PluginMetadata
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

require("nonebot_plugin_skland")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_alconna.uniseg.message import current_send_wrapper
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.commands import bind as binding
from nonebot_plugin_skland.model import SkUser, Character, GachaRecord
from nonebot_plugin_skland.schemas import EndfieldPoolType
from nonebot_plugin_skland.utils import get_all_ef_gacha_records

from .core import Queue, TZ, record_values

__plugin_meta__ = PluginMetadata(
    name="Skland 自动终末地抽卡同步", description="绑定后同步及北京时间每天 01:00 更新",
    usage="绑定后后台更新；/zmd抽卡记录 查看缓存；手动 -u 仍可用。", type="application",
)

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data/skland_auto_gacha/queue.sqlite3"
DAILY_ID = "skland_auto_efgacha_daily_v1"
WORKER_ID = "skland_auto_efgacha_worker_v1"
TRIGGER = CronTrigger(hour=1, minute=0, second=0, timezone=TZ)
_queue: Queue | None = None
_busy = asyncio.Lock()
_active: asyncio.Task | None = None
_stopping = False
BIND_NOTICE = "\n终末地抽卡记录已加入自动更新队列，稍后可使用 /zmd抽卡记录 查看"


def install_binding_notice():
    """One-shot, current matcher context only; preserve existing send extensions."""
    previous = current_send_wrapper.get(None)
    consumed = False

    async def with_notice(bot, event, message):
        nonlocal consumed
        if (not consumed and isinstance(message, UniMessage)
                and message.extract_plain_text() in {"绑定成功", "更新成功"}):
            consumed = True
            current_send_wrapper.set(previous)
            message = message.copy()
            message += BIND_NOTICE
        if previous is not None:
            return await previous(bot, event, message)
        return message

    current_send_wrapper.set(with_notice)

# Current installation includes user-approved local Skland changes. Preserve them.
_HASHES = {
    "commands/bind.py": "3e27cf331aa7363c5dec9cb3e12680d702a9275cef33af6c2477ab1539631bf3",
    "utils.py": "67e725d5bc9c25ee29934e31bb8ea2f42acb21a238708b4770e890aecb4f3191",
    "model.py": "bb0ed289b8f4da2f0c6b66a78dc642ab2b265d3ca5963cb55231e276259f40bb",
    "api/login.py": "a777ee74f7574b3184b21ef41496ce856827706fc2536fcd47452aa4f2bbf04b",
    "api/request.py": "a041670ed1bfc245cffc094861ba738e0572e4fed06e1fe82dc16b6a93f1d5fb",
}


def verify_upstream():
    if version("nonebot-plugin-skland") != "0.7.1":
        raise RuntimeError("Skland auto-gacha requires audited version 0.7.1")
    root = Path(binding.__file__).parents[1]
    for name, expected in _HASHES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Skland auto-gacha source compatibility check failed: " + name)


verify_upstream()
_original_bind = binding.get_characters_and_bind
if getattr(_original_bind, "_auto_gacha", False):
    raise RuntimeError("Skland auto-gacha binding hook already installed")


@functools.wraps(_original_bind)
async def _bind_then_enqueue(user, session):
    uid = user.id
    await _original_bind(user, session)  # Commits binding and characters first.
    if _queue is None:
        raise RuntimeError("Skland auto-gacha queue not initialized")
    try:
        _queue.enqueue(uid)
    except Exception as exc:
        # Binding already committed; never misreport it as a failed binding.
        logger.error("auto-efgacha enqueue_failed error_type={}", type(exc).__name__)
    else:
        install_binding_notice()
        logger.info("auto-efgacha binding_committed queued=1")


_bind_then_enqueue._auto_gacha = True
binding.get_characters_and_bind = _bind_then_enqueue


async def sync_user(uid: int) -> tuple[str, int]:
    # Read credentials in a short session; do not hold a transaction during HTTP requests.
    async with get_session() as session:
        user = await session.get(SkUser, uid)
        if user is None:
            return "unbound", 0
        token = user.access_token
        if not token:
            return "no_access_token", 0
        characters = list(await session.scalars(select(Character).where(
            Character.id == uid, Character.app_code == "endfield")))
    if not characters:
        return "no_endfield_character", 0
    grant = await SklandLoginAPI.get_grant_code(token, 1)
    total = 0
    for character in characters:
        role = await SklandLoginAPI.get_role_token_by_uid(character.uid, grant)
        records = {}
        for pool in EndfieldPoolType:
            fetched = await get_all_ef_gacha_records(character, pool, role, concurrency=1)
            for record in fetched:
                values = record_values(uid, character, record)
                records[(values["gacha_ts"], values["pos"])] = values
            await asyncio.sleep(0.5)
        async with get_session() as session:
            # Serializes the short save against manual -u/unbind/rebind transactions.
            from sqlalchemy import text
            await session.execute(text("BEGIN IMMEDIATE"))
            current = await session.get(SkUser, uid)
            char = await session.get(Character, (uid, character.uid))
            if current is None or char is None:
                return "unbound", total
            if current.access_token != token or char.app_code != "endfield" or (
                char.role_id, char.channel_master_id
            ) != (character.role_id, character.channel_master_id):
                return "binding_changed", total
            for values in records.values():
                statement = insert(GachaRecord).values(**values).on_conflict_do_nothing(
                    index_elements=["char_uid", "app_code", "gacha_ts", "pos"])
                result = await session.execute(statement)
                total += max(result.rowcount, 0)
            await session.commit()
    return "success", total


# Other local plugins may be told when a queued sync ends:
#   await listener(user_id, status, new_records, final)   (final=False: a retry is scheduled)
sync_listeners: list = []


async def _tell_listeners(uid: int, status: str, records: int, final: bool) -> None:
    for listener in list(sync_listeners):
        try:
            await listener(uid, status, records, final)
        except Exception as exc:  # a listener must never stall the queue
            logger.warning("auto-efgacha listener_failed error_type={}", type(exc).__name__)


async def queue_daily():
    if _queue is None or _stopping:
        return
    now = datetime.now(timezone.utc)
    if not _queue.day_due(now):
        return
    async with get_session() as session:
        users = list(await session.scalars(select(SkUser.id)))
    count = _queue.enqueue_day(now, users)
    logger.info("auto-efgacha daily_queued users={}", count)


async def daily_tick():
    try:
        await queue_daily()
    except Exception as exc:
        logger.error("auto-efgacha daily_queue_failed error_type={}", type(exc).__name__)


async def poll_queue():
    global _active
    if _stopping or (_active is not None and not _active.done()):
        return
    _active = asyncio.create_task(drain_queue(), name="skland-auto-efgacha")


async def drain_queue():
    global _active
    if _queue is None or _busy.locked() or _stopping:
        return
    async with _busy:
        _active = asyncio.current_task()
        try:
            await queue_daily()  # Catch up one missed daily slot after downtime.
            while not _stopping and (item := _queue.next()) is not None:
                try:
                    async with asyncio.timeout(600):
                        status, records = await sync_user(item[0])
                except asyncio.CancelledError:
                    raise  # Persistent item remains queued for the next startup.
                except Exception as exc:
                    _queue.finish(item, "failed", retry=True)
                    logger.warning("auto-efgacha update_failed reason={} attempt={} error_type={}",
                                   item[2], item[3] + 1, type(exc).__name__)
                    await _tell_listeners(item[0], "failed", 0, item[3] >= 2)
                else:
                    _queue.finish(item, status, records)
                    logger.info("auto-efgacha update_done reason={} status={} new_records={}",
                                item[2], status, records)
                    await _tell_listeners(item[0], status, records, True)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("auto-efgacha worker_failed error_type={}", type(exc).__name__)
        finally:
            _active = None


@get_driver().on_startup
async def start_auto_gacha():
    global _queue, _stopping
    verify_upstream()
    hook = binding.get_characters_and_bind
    while hook is not _bind_then_enqueue and getattr(hook, "_bind_sign", False):  # skland_bind_sign wraps this hook
        hook = hook.__wrapped__
    if hook is not _bind_then_enqueue:
        raise RuntimeError("Skland auto-gacha binding hook replaced")
    _queue = Queue(STATE)
    _queue.initialize_day(datetime.now(timezone.utc))
    _stopping = False
    scheduler.add_job(daily_tick, TRIGGER, id=DAILY_ID, replace_existing=True,
                      max_instances=1, coalesce=True, misfire_grace_time=86400)
    scheduler.add_job(poll_queue, "interval", seconds=10, id=WORKER_ID,
                      replace_existing=True, max_instances=1, coalesce=True)
    logger.info("auto-efgacha ready timezone=Asia/Shanghai daily=01:00 worker=serial")


@get_driver().on_shutdown
async def stop_auto_gacha():
    global _stopping
    _stopping = True
    if _active is not None and _active is not asyncio.current_task():
        _active.cancel()
        await asyncio.gather(_active, return_exceptions=True)
