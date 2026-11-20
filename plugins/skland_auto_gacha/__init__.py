"""绑定后以及每天（Asia/Shanghai 时区）在后台更新终末地抽卡记录。

队列以成员（nonebot-plugin-user 的 id，即 `SkUser.owner_id`）为键。一项任务同步该成员绑定的
每个森空岛账号下的每个终末地角色。
"""
from __future__ import annotations

import asyncio
import re
import functools
import hashlib
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from nonebot import get_driver, logger, require
from nonebot.plugin import PluginMetadata
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import event, select, update
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


from .core import Queue, TZ, record_values

# 抽卡记录 API 每页 5 行，最新的在前。每晚把每个成员的全部记录读一遍，在约 600 名成员时
# 要约 11 万次请求、8 小时以上，而新增的抽卡只有几百次。现在每个卡池从最新一页
# 往回读，读到第一条已存的记录为止。一个角色的记录在所有卡池都读完后一次性保存，所以存下的
# 始终是从最新往旧、连续完整的一段。
# 兜底：每个成员每周有一天（owner_id % 7）完整读一遍，读到的超出最新那一段的记录记为 `missed`。
FULL_CHECK_DAYS = 7
WORKERS = 5  # 同时同步的成员数，与竞速榜采集相同
REQUEST_GAP = 2  # 同一个 worker 两个成员之间的秒数


def full_check_due(owner_id: int, today) -> bool:
    return owner_id % FULL_CHECK_DAYS == today.toordinal() % FULL_CHECK_DAYS


def newest_stretch(records, known: set) -> list:
    """第一条已存记录之前的行，最新的在前（增量读取返回的内容）。"""
    stretch = []
    for record in sorted(records, key=lambda r: r.seq_id_int, reverse=True):
        if (record.gacha_ts_sec, record.seq_id_int) in known:
            break
        stretch.append(record)
    return stretch


async def new_ef_gacha_records(pool, server_id: str, role_token: str, known: set, fetch=None) -> list:
    """从最新一页往回翻，直到某页含有已存的行（或记录到头）。

    下一页从这一页过滤前的最后一条往下读（page.next_seq）：一整页都是被上游过滤掉的非抽卡条目时
    （寻访情报书、保底赠送的武器），照样翻到下一页，而不是当成到头了。
    """
    fetch = fetch or SklandAPI.get_ef_gacha_history
    found, seq_id = [], None
    while True:
        page = await fetch(pool, server_id, role_token, seq_id)
        rows = [r for r in page.gacha_list if seq_id is None or r.seq_id_int < int(seq_id)]
        fresh = [r for r in rows if (r.gacha_ts_sec, r.seq_id_int) not in known]
        found.extend(fresh)
        following = page.next_seq
        if len(fresh) < len(rows) or not page.hasMore or not following or following == seq_id:
            return found
        seq_id = following

__plugin_meta__ = PluginMetadata(
    name="Skland 自动终末地抽卡同步", description="绑定后同步及北京时间每天 01:00 更新",
    usage="绑定后后台更新；/zmd抽卡记录 查看缓存；手动 -u 仍可用。", type="application",
)

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data/skland_auto_gacha/queue.sqlite3"
DAILY_ID = "skland_auto_efgacha_daily_v1"
WORKER_ID = "skland_auto_efgacha_worker_v1"
TRIGGER = CronTrigger(hour=1, minute=0, second=0, timezone=TZ)
POOL_TYPES = tuple(EndfieldPoolType)  # 官方 API 保存记录的每一种卡池（含重构寻访）
_queue: Queue | None = None
_busy = asyncio.Lock()
_active: asyncio.Task | None = None
_stopping = False
BIND_NOTICE = "\n终末地抽卡记录已加入自动更新队列，稍后可使用 /zmd抽卡记录 查看"


def install_binding_notice():
    """一次性，只作用于当前 matcher 的上下文；保留已有的发送扩展。"""
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

# 编写本插件时所依据的上游源码。
_HASHES = {
    "services/binding.py": "846e7b85cc5aa66105542a8fe87934762db6fb2a5991f48a2f08109684c7cea8",
    "services/gacha.py": "cb6ddcc782860b3a1d170adb9f2f17cfc082964d3c8997925b7b34918b441fce",
    "model.py": "94ec29e96e1855101b15cff39b0cd0f1ebd1cc7b8b2b0259e29b7a19ff50c370",
    "api/login.py": "fd35e3557de88bdb932ae71d9f37925489016e3535d2d273563bd798f0a12dc3",
    "api/request.py": "da7182e873a4c9ee9f40db7fbf620fe725dee846829f1da34b5fb35d4551118e",
}


def verify_upstream():
    if version("nonebot-plugin-skland") != "0.7.4":
        raise RuntimeError("Skland auto-gacha requires audited version 0.7.4")
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
    """每次保存绑定（第一个账号、另一个账号、重新登录）都为其所有者排一次同步。"""
    await _original_commit(prepared, session)  # 先提交账号及其角色。
    if _queue is None:
        raise RuntimeError("Skland auto-gacha queue not initialized")
    try:
        _queue.enqueue(prepared.owner_id)
    except Exception as exc:
        # 绑定已经提交了；绝不能把它误报成绑定失败。
        logger.error("auto-efgacha enqueue_failed error_type={}", type(exc).__name__)
    else:
        install_binding_notice()
        logger.info("auto-efgacha binding_committed queued=1")


_commit_then_enqueue._auto_gacha = True
binding.commit_account_binding = _commit_then_enqueue


async def sync_user(owner_id: int) -> tuple[str, int]:
    """同步成员的每个终末地角色：返回（"success" | "partial" | 原因，新增行数）。

    "partial"：部分角色已同步，至少一个失败。所有角色都失败时，抛出第一个错误，
    让队列重试。
    """
    # 在短会话里读取凭据；HTTP 请求期间不持有事务。
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
    # 一个森空岛账号可能带有第二个终末地绑定，里面没有角色。抽卡记录 API 会拒绝它
    # （"Token is invalid"），以前这会连成员真正的角色也一起判为失败。
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
                    fetched = await get_all_ef_gacha_records(role.channel_master_id, pool, role_token)
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
            logger.warning("auto-efgacha role_failed error_type={} said={}", type(exc).__name__, re.sub(r"\d{5,}", "<id>", str(exc))[:80])  # 号码不进日志
            continue
        async with get_session() as session:
            # 让这次短暂的保存与手动 -u/解绑/重新绑定的事务排队串行。
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
                if result.rowcount <= 0 and values["pool_version"] is not None:  # 升级前存下的行补上卡池期次
                    await session.execute(update(GachaRecord).where(
                        GachaRecord.character_id == values["character_id"], GachaRecord.gacha_ts == values["gacha_ts"],
                        GachaRecord.pos == values["pos"], GachaRecord.pool_version.is_(None)).values(pool_version=values["pool_version"]))
            await session.commit()
        synced += 1
    if not synced:
        raise errors[0]
    return ("partial" if errors else "success"), total


# 其他本地插件可以在排队的同步结束时收到通知：
#   await listener(user_id, status, new_records, final)   （final=False：已安排重试）
sync_listeners: list = []


def bind_sync_pending(user_id: int) -> bool:
    """绑定时排入的同步，第一次尝试还没结束。"""
    return _queue is not None and _queue.waiting(user_id)


# ── 手动更新与后台同步撞车 ────────────────────────────
# 刚绑定完时，队列已经在拉取成员的记录；成员这时发 /zmd抽卡记录更新，会拉到同样的抽卡。
# 上游在指令开始时判断哪些是新的，到最后才保存，所以两者中后保存的那个撞上唯一键，
# 指令直接出错、没有回复。现在改为把这期间已经进表的行从待插入的记录里去掉。


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
        except Exception as exc:  # 监听者绝不能卡住队列
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


_working: set[int] = set()  # 正在被某个 worker 同步的成员


async def _work():
    """一个 worker：反复取下一个没人在处理的成员来同步，直到队列里没有到期的任务项。"""
    while not _stopping and (item := _queue.next(_working)) is not None:
        _working.add(item[0])
        try:
            try:
                async with asyncio.timeout(600):
                    status, records = await sync_user(item[0])
            except asyncio.CancelledError:
                raise  # 持久化的任务项留在队列里，等下次启动。
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
        finally:
            _working.discard(item[0])
        await asyncio.sleep(REQUEST_GAP)


async def drain_queue():
    global _active
    if _queue is None or _busy.locked() or _stopping:
        return
    async with _busy:
        _active = asyncio.current_task()
        try:
            await queue_daily()  # 停机后补上一次错过的每日时段。
            async with asyncio.TaskGroup() as group:
                for _ in range(WORKERS):
                    group.create_task(_work())
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
    while hook is not _commit_then_enqueue and getattr(hook, "_bind_sign", False):  # skland_bind_sign 会包住这个钩子
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
    logger.info(f"auto-efgacha ready timezone=Asia/Shanghai daily=01:00 workers={WORKERS}")


@get_driver().on_shutdown
async def stop_auto_gacha():
    global _stopping
    _stopping = True
    if _active is not None and _active is not asyncio.current_task():
        _active.cancel()
        await asyncio.gather(_active, return_exceptions=True)
