"""Background Endfield history updates after binding and daily in Asia/Shanghai.

The queue is keyed by the member (nonebot-plugin-user id, `SkUser.owner_id`). One item syncs
every Endfield role of every Skland account that member has bound.
"""
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
from sqlalchemy import event, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

require("nonebot_plugin_skland")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_alconna.uniseg.message import current_send_wrapper
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.model import SkUser, Character, GachaRecord
from nonebot_plugin_skland.schemas import EndfieldPoolType
from nonebot_plugin_skland.services import binding
from nonebot_plugin_skland.services.gacha import get_all_ef_gacha_records

from plugins.skland_pools import RERUN

from .core import Queue, TZ, record_values

# The record API pages 5 rows at a time, newest first. Reading every member's whole history each night
# took ~110k requests and 8+ hours at ~600 members (2026-11-13), for a few hundred new pulls. Each pool
# is now read from the newest page back to the first row already stored. Rows of a role are saved all at
# once after every pool was read, so what is stored is always a complete newest-to-oldest stretch.
# Safety net: every member is read in full on one day of the week (owner_id % 7), and what that finds
# beyond the newest stretch is logged as `missed`.
FULL_CHECK_DAYS = 7


def full_check_due(owner_id: int, today) -> bool:
    return owner_id % FULL_CHECK_DAYS == today.toordinal() % FULL_CHECK_DAYS


def newest_stretch(records, known: set) -> list:
    """The rows before the first stored one, newest first (what an incremental read returns)."""
    stretch = []
    for record in sorted(records, key=lambda r: r.seq_id_int, reverse=True):
        if (record.gacha_ts_sec, record.seq_id_int) in known:
            break
        stretch.append(record)
    return stretch


async def new_ef_gacha_records(pool, server_id: str, role_token: str, known: set, fetch=None) -> list:
    """Pages newest first until a page holds a stored row (or the history ends)."""
    fetch = fetch or SklandAPI.get_ef_gacha_history
    found, seq_id = [], None
    while True:
        page = await fetch(pool, server_id, role_token, seq_id)
        rows = [r for r in page.gacha_list if seq_id is None or r.seq_id_int < int(seq_id)]
        if not rows:
            return found
        fresh = [r for r in rows if (r.gacha_ts_sec, r.seq_id_int) not in known]
        found.extend(fresh)
        if len(fresh) < len(rows) or not page.hasMore:
            return found
        seq_id = str(min(r.seq_id_int for r in rows))

__plugin_meta__ = PluginMetadata(
    name="Skland 自动终末地抽卡同步", description="绑定后同步及北京时间每天 01:00 更新",
    usage="绑定后后台更新；/zmd抽卡记录 查看缓存；手动 -u 仍可用。", type="application",
)

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data/skland_auto_gacha/queue.sqlite3"
DAILY_ID = "skland_auto_efgacha_daily_v1"
WORKER_ID = "skland_auto_efgacha_worker_v1"
TRIGGER = CronTrigger(hour=1, minute=0, second=0, timezone=TZ)
POOL_TYPES = (*EndfieldPoolType, RERUN)  # every kind of pool the official API keeps records for
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
                and message.extract_plain_text() in {"绑定成功", "账号更新成功"}):
            consumed = True
            current_send_wrapper.set(previous)
            message = message.copy()
            message += BIND_NOTICE
        if previous is not None:
            return await previous(bot, event, message)
        return message

    current_send_wrapper.set(with_notice)

# The upstream sources this plugin was written against.
_HASHES = {
    "services/binding.py": "846e7b85cc5aa66105542a8fe87934762db6fb2a5991f48a2f08109684c7cea8",
    "services/gacha.py": "6e482b41fdf0a9a03aa3c9d6e145854599237f24d444781da2e67732b0477590",
    "model.py": "eefa679be150f03d5942777bc6105d155110fade7af74f54da05d0c7fb303354",
    "api/login.py": "a777ee74f7574b3184b21ef41496ce856827706fc2536fcd47452aa4f2bbf04b",
    "api/request.py": "d3b3ea49428d64d9cc7e47ff4ac20abb2687fb9d9efbd26abeb087877251ded7",
}


def verify_upstream():
    if version("nonebot-plugin-skland") != "0.7.2":
        raise RuntimeError("Skland auto-gacha requires audited version 0.7.2")
    root = Path(binding.__file__).parents[1]
    for name, expected in _HASHES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Skland auto-gacha source compatibility check failed: " + name)


verify_upstream()
_original_commit = binding.commit_account_binding
if getattr(_original_commit, "_auto_gacha", False):
    raise RuntimeError("Skland auto-gacha binding hook already installed")


@functools.wraps(_original_commit)
async def _commit_then_enqueue(prepared, session):
    """Every saved binding (first account, another account, renewed login) queues a sync for its owner."""
    await _original_commit(prepared, session)  # Commits the account and its roles first.
    if _queue is None:
        raise RuntimeError("Skland auto-gacha queue not initialized")
    try:
        _queue.enqueue(prepared.owner_id)
    except Exception as exc:
        # Binding already committed; never misreport it as a failed binding.
        logger.error("auto-efgacha enqueue_failed error_type={}", type(exc).__name__)
    else:
        install_binding_notice()
        logger.info("auto-efgacha binding_committed queued=1")


_commit_then_enqueue._auto_gacha = True
binding.commit_account_binding = _commit_then_enqueue


async def sync_user(owner_id: int) -> tuple[str, int]:
    """Sync every Endfield role of the member: ("success" | "partial" | reason, new rows).

    "partial": some roles were synced and at least one failed. When every role fails, the first
    error is raised so the queue retries.
    """
    # Read credentials in a short session; do not hold a transaction during HTTP requests.
    async with get_session() as session:
        accounts = (await session.execute(select(SkUser.id, SkUser.access_token).where(SkUser.owner_id == owner_id))).all()
        if not accounts:
            return "unbound", 0
        roles = (await session.execute(
            select(Character.id, Character.account_id, Character.uid, Character.channel_master_id, Character.role_id)
            .join(Character.account).where(SkUser.owner_id == owner_id, Character.app_code == "endfield")
            .order_by(Character.account_id, Character.id))).all()
    tokens = dict(accounts)
    async with get_session() as session:
        stored = (await session.execute(
            select(GachaRecord.character_id, GachaRecord.gacha_ts, GachaRecord.pos)
            .where(GachaRecord.character_id.in_([role.id for role in roles]), GachaRecord.pos > 0))).all()
    known_by_role: dict[int, set] = {}
    for character_id, gacha_ts, pos in stored:
        known_by_role.setdefault(character_id, set()).add((gacha_ts, pos))
    full = full_check_due(owner_id, datetime.now(TZ).date())
    # A Skland account can carry a second Endfield binding that has no role in it. The record
    # API rejects it ("Token is invalid"), which used to fail the member's real role as well.
    roles = [role for role in roles if role.role_id]
    if not roles:
        return "no_endfield_character", 0
    roles = [role for role in roles if tokens.get(role.account_id)]
    if not roles:
        return "no_access_token", 0
    total, synced, errors, grants = 0, 0, [], {}
    for role in roles:
        try:
            token = tokens[role.account_id]
            if role.account_id not in grants:
                grants[role.account_id] = await SklandLoginAPI.get_grant_code(token, 1)
            role_token = await SklandLoginAPI.get_role_token_by_uid(role.uid, grants[role.account_id])
            records = {}
            known = known_by_role.get(role.id, set())
            missed = 0
            for pool in POOL_TYPES:
                if full or not known:
                    fetched = await get_all_ef_gacha_records(role.channel_master_id, pool, role_token, concurrency=1)
                    if known:
                        stretch = {(r.gacha_ts_sec, r.seq_id_int) for r in newest_stretch(fetched, known)}
                        missed += sum(1 for r in fetched if (r.gacha_ts_sec, r.seq_id_int) not in known and (r.gacha_ts_sec, r.seq_id_int) not in stretch)
                else:
                    fetched = await new_ef_gacha_records(pool, role.channel_master_id, role_token, known)
                for record in fetched:
                    values = record_values(role.id, record)
                    records[(values["gacha_ts"], values["pos"])] = values
                await asyncio.sleep(0.5)
            if full and known:
                logger.info("auto-efgacha full_check missed={}", missed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(exc)
            logger.warning("auto-efgacha role_failed error_type={}", type(exc).__name__)
            continue
        async with get_session() as session:
            # Serializes the short save against manual -u/unbind/rebind transactions.
            from sqlalchemy import text
            await session.execute(text("BEGIN IMMEDIATE"))
            current = (await session.execute(
                select(SkUser.owner_id, SkUser.access_token, Character.app_code, Character.role_id, Character.channel_master_id)
                .join(Character.account).where(Character.id == role.id))).first()
            if current is None:
                return "unbound", total
            if (current.owner_id, current.access_token, current.app_code, current.role_id, current.channel_master_id) != (
                owner_id, token, "endfield", role.role_id, role.channel_master_id
            ):
                return "binding_changed", total
            for values in records.values():
                statement = insert(GachaRecord).values(**values).on_conflict_do_nothing(
                    index_elements=["character_id", "gacha_ts", "pos"])
                result = await session.execute(statement)
                total += max(result.rowcount, 0)
            await session.commit()
        synced += 1
    if not synced:
        raise errors[0]
    return ("partial" if errors else "success"), total


# Other local plugins may be told when a queued sync ends:
#   await listener(user_id, status, new_records, final)   (final=False: a retry is scheduled)
sync_listeners: list = []


def bind_sync_pending(user_id: int) -> bool:
    """The sync queued by a binding has not had its first attempt finish yet."""
    return _queue is not None and _queue.waiting(user_id)


# ── manual update racing the background sync ────────────────────────────
# Right after binding, the queue is already fetching the member's records; a member who sends
# /zmd抽卡记录更新 at that moment fetches the same pulls. Upstream decides what is new when the
# command starts and saves at its very end, so the later of the two hit the unique key and the
# command died without a reply. Rows that reached the table in the meantime are dropped from the
# pending insert instead.


@event.listens_for(Session, "before_flush")
def _skip_saved_records(session, flush_context, instances) -> None:
    pending: dict[int, list[GachaRecord]] = {}
    for obj in session.new:
        if isinstance(obj, GachaRecord) and obj.character_id is not None:
            pending.setdefault(obj.character_id, []).append(obj)
    for character_id, records in pending.items():
        with session.no_autoflush:
            saved = set(session.execute(select(GachaRecord.gacha_ts, GachaRecord.pos).where(GachaRecord.character_id == character_id)).all())
        duplicates = [r for r in records if (int(r.gacha_ts), int(r.pos)) in saved]
        for record in duplicates:
            session.expunge(record)
        if duplicates:
            logger.info("auto-efgacha manual_overlap skipped={} kept={}", len(duplicates), len(records) - len(duplicates))


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
        users = list(await session.scalars(select(SkUser.owner_id).distinct()))
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
    hook = binding.commit_account_binding
    while hook is not _commit_then_enqueue and getattr(hook, "_bind_sign", False):  # skland_bind_sign wraps this hook
        hook = hook.__wrapped__
    if hook is not _commit_then_enqueue:
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
