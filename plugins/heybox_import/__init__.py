"""Import older Endfield gacha history from 小黑盒 (Heybox) "抽卡分析".

/zmd导入小黑盒 <小黑盒ID>   rebuild the pools / periods the official API no longer returns
/zmd撤销小黑盒导入          remove everything this command imported for the caller

The official record API only reaches back a few months; Heybox keeps what its users uploaded
earlier. Its public overview (readable by Heybox id, no login) is fetched by opening the tool page
in the headless browser, which signs the request itself. See plan.py for how the aggregate is
turned into rows and which checks protect the data:

  - the Heybox account must be bound to the same game UID as the caller's Skland binding, and
    every 6-star id must carry that UID: nobody can import another player's history;
  - official rows are never changed; a pool is only extended backwards, and only when both
    sources agree where they overlap;
  - imported rows have a negative `pos`, so they are identifiable and removable.

The import is a function of (Heybox answer, official rows). Official rows can also arrive after
an import: the first sync of a new binding, or pulls the nightly sync had not fetched yet. The
pulls they cover must then leave the import or they would be counted twice, so the Heybox answer
is kept and the import is rebuilt from it (`reconcile`) whenever official rows are added.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from nonebot import logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.exception import MatcherException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from nonebot.adapters import Event
from nonebot.consts import PREFIX_KEY, RAW_CMD_KEY
from nonebot.rule import Rule
from nonebot.typing import T_State

from plugins.strict_command import strict

require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")
require("nonebot_plugin_user")
require("plugins.skland_auto_gacha")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage, message_reaction
from nonebot_plugin_htmlrender import get_new_page
from nonebot_plugin_orm import async_scoped_session, get_session
from nonebot_plugin_skland.data_source import ef_gacha_pool_data
from nonebot_plugin_skland.model import Character, CharacterDefault, GachaRecord, SkUser
from nonebot_plugin_user import UserSession
from sqlalchemy import delete, event, func, insert, select

from plugins import skland_auto_gacha as auto_gacha

from .plan import Official, Plan, build_plan

__plugin_meta__ = PluginMetadata(
    name="Heybox gacha import",
    description="从小黑盒「抽卡分析」补回官方接口已无法拉取的终末地抽卡记录。",
    usage="/zmd导入小黑盒 小黑盒ID  /zmd撤销小黑盒导入",
    type="application",
)

TOOL_URL = "https://www.xiaoheihe.cn/game/endfield/card_statistic?user_id={heybox_id}"
OVERVIEW_PATH = "/game/endfield/player/overview"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
DATA_DIR = store.get_plugin_data_dir()
COOLDOWN = 300
FETCH_TIMEOUT = 15000  # ms per attempt
FETCH_ATTEMPTS = 3
RECONCILE_DELAY = 10  # s after an official row is flushed; its transaction has to commit first
RECONCILE_ATTEMPTS = 3
CN = timezone(timedelta(hours=8))
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"

async def _id_may_follow(event: Event, state: T_State) -> bool:
    """Like strict_command, but "/zmd导入小黑盒12345678" (digits glued to the command) is accepted too."""
    message = event.get_message()
    if not message or message[0].type != "text":
        return True
    raw = state.get(PREFIX_KEY, {}).get(RAW_CMD_KEY) or ""
    text = str(message[0]).lstrip()
    rest = text[len(raw) :] if raw and text.startswith(raw) else ""
    return not rest or rest[0].isspace() or rest[0].isdigit()


import_cmd = on_command("zmd导入小黑盒", aliases={"终末地导入小黑盒", "ef导入小黑盒", "导入小黑盒"}, rule=Rule(_id_may_follow), priority=5, block=True)
undo_cmd = on_command("zmd撤销小黑盒导入", aliases={"终末地撤销小黑盒导入", "ef撤销小黑盒导入", "撤销小黑盒导入"}, rule=strict, priority=5, block=True)
_lock = asyncio.Lock()
_last_run: dict[int, float] = {}
WHERE_ID = "小黑盒 ID 是小黑盒 App 个人主页里的 ID 数字，不是游戏 UID"
NOT_BOUND = "你还没有在机器人这里绑定森空岛。请先发送 /skl绑定 扫码绑定，绑定成功后再发送 /zmd导入小黑盒 小黑盒ID"
NO_ROLE = "你绑定的森空岛账号下没有终末地角色，无法导入。如果刚创建角色，请先发送 /skl角色更新"


async def _roles(owner_id: int, session) -> list[Character]:
    """The member's Endfield roles (every account), default role first."""
    default_id = await session.scalar(select(CharacterDefault.character_id).where(CharacterDefault.owner_id == owner_id, CharacterDefault.app_code == "endfield"))
    roles = (
        await session.scalars(
            select(Character).join(Character.account).where(SkUser.owner_id == owner_id, Character.app_code == "endfield", Character.role_id != "").order_by(Character.id)
        )
    ).all()
    return sorted(roles, key=lambda role: role.id != default_id)


async def _bound_roles(owner_id: int, session):
    """(roles, "") or ([], reason text)."""
    if (await session.scalars(select(SkUser.id).where(SkUser.owner_id == owner_id).limit(1))).first() is None:
        return [], NOT_BOUND
    roles = await _roles(owner_id, session)
    return (roles, "") if roles else ([], NO_ROLE)


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


async def _fetch_once(heybox_id: str) -> dict:
    async with get_new_page(device_scale_factor=1, viewport={"width": 430, "height": 900}, user_agent=USER_AGENT) as page:
        async with page.expect_response(lambda r: OVERVIEW_PATH in r.url, timeout=FETCH_TIMEOUT) as waiter:
            await page.goto(TOOL_URL.format(heybox_id=heybox_id), wait_until="domcontentloaded", timeout=FETCH_TIMEOUT)
        response = await waiter.value
        return await response.json()


async def fetch_overview(heybox_id: str) -> dict:
    """Heybox's overview for one Heybox id; the tool page signs and sends the request itself.

    A normal load answers in ~3 s. The page's scripts sit on Heybox's CDN, which now and then
    times out from this host; the app then never asks for the data, so retry on a fresh page.
    """
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            payload = await _fetch_once(heybox_id)
        except Exception as e:
            last = e
            logger.info(f"Heybox fetch attempt {attempt + 1} failed: {type(e).__name__}")
            continue
        if payload.get("status") != "ok":
            raise RuntimeError(f"heybox status {payload.get('status')}")
        return payload.get("result") or {}
    raise last or RuntimeError("heybox fetch failed")


def _imported(character_id: int):
    """The rows an import put under one role: official seqIds are positive, imported ones negative."""
    return (GachaRecord.character_id == character_id, GachaRecord.pos < 0)


def _endfield_official(*columns):
    """SELECT over the official Endfield rows of every member."""
    return select(*columns).join(Character, Character.id == GachaRecord.character_id).where(Character.app_code == "endfield", GachaRecord.pos >= 0)


async def _known_names(session) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """(item_type, name) -> item id and pool_id -> pool name, learned from every official row we hold."""
    ids = {
        (item_type, name): item_id
        for item_type, name, item_id in await session.execute(
            _endfield_official(GachaRecord.item_type, GachaRecord.char_name, GachaRecord.char_id)
            .group_by(GachaRecord.item_type, GachaRecord.char_name, GachaRecord.char_id)
        )
        if item_id
    }
    with contextlib.suppress(Exception):  # names nobody has pulled recently: fall back to the wiki index
        from plugins import endfield_wiki

        loaded = endfield_wiki._resolver()
        index = loaded[0] if loaded else {}
        for item_type, table in (("char", index.get("operators", {})), ("weapon", index.get("weapons", {}))):
            for item_id, item in table.items():
                if item.get("name") and not item_id.startswith("chr_9000"):
                    ids.setdefault((item_type, item["name"]), item_id)
    pools = dict(
        (await session.execute(
            _endfield_official(GachaRecord.pool_id, func.max(GachaRecord.pool_name))
            .group_by(GachaRecord.pool_id)
        )).all()
    )
    return ids, pools


def _pool_name(pool_id: str, fallback: str, known: dict[str, str]) -> str:
    if pool_id in known:
        return known[pool_id]
    local = ef_gacha_pool_data.get_pool(pool_id)
    return (local.pool_name if local and local.pool_name else fallback) or pool_id


def _pool_windows() -> dict[str, tuple[int, int]]:
    """Pool id -> (open, close) in unix seconds, from the wiki index's calendar; empty when unavailable."""
    windows: dict[str, tuple[int, int]] = {}
    with contextlib.suppress(Exception):
        from plugins import endfield_wiki

        loaded = endfield_wiki._resolver()
        for pool in ((loaded[0] if loaded else {}).get("calendar") or {}).get("pools") or []:
            with contextlib.suppress(KeyError, ValueError):
                opened, closed = (datetime.strptime(pool[key], "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN) for key in ("open", "close"))
                windows[pool["id"]] = (int(opened.timestamp()), int(closed.timestamp()))
    return windows


async def _build(session, character, result: dict) -> tuple[Plan, list[dict]]:
    """The plan for this role and the database rows it stands for."""
    official = [
        Official(pool_id, name, rarity, int(ts), pos, bool(is_free))
        for pool_id, name, rarity, ts, pos, is_free in await session.execute(
            select(GachaRecord.pool_id, GachaRecord.char_name, GachaRecord.rarity, GachaRecord.gacha_ts, GachaRecord.pos, GachaRecord.is_free)
            .where(GachaRecord.character_id == character.id, GachaRecord.pos >= 0)
        )
    ]
    plan = build_plan(result, str(character.role_id), official, _pool_windows())
    if plan.error or not plan.rows:
        return plan, []
    ids, pool_names = await _known_names(session)
    rows = [
        {
            "character_id": character.id,
            "item_type": row["item_type"], "pool_id": row["pool_id"],
            "pool_name": _pool_name(row["pool_id"], row["pool_name"], pool_names),
            "char_id": ids.get((row["item_type"], row["name"]), ""), "char_name": row["name"],
            "rarity": row["rarity"], "is_new": False, "is_free": row["is_free"], "gacha_ts": row["ts"], "pos": row["pos"],
        }
        for row in plan.rows
    ]
    return plan, rows


async def _replace(session, character_id: int, rows: list[dict]) -> None:
    await session.execute(delete(GachaRecord).where(*_imported(character_id)))
    for start in range(0, len(rows), 500):
        await session.execute(insert(GachaRecord), rows[start : start + 500])


async def run_import(session, character, result: dict) -> Plan:
    """Replace this role's earlier import with a fresh one built from `result`; commits on success."""
    plan, rows = await _build(session, character, result)
    if rows:
        await _replace(session, character.id, rows)
        await session.commit()
    return plan


def _snapshot_path(owner_id: int, role_id: str) -> Path:
    return DATA_DIR / f"{owner_id}-{role_id}.json"


def _keep_raw(owner_id: int, role_id: str, heybox_id: str, result: dict) -> None:
    """Provenance: the Heybox answer the import was built from (0600, one file per role)."""
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _snapshot_path(owner_id, role_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"heybox_id": heybox_id, "fetched": time.time(), "result": result}, ensure_ascii=False), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    legacy = DATA_DIR / f"{owner_id}.json"  # written before a member could have several roles
    with contextlib.suppress(OSError, ValueError):
        if str((json.loads(legacy.read_text("utf-8")).get("result") or {}).get("user_info", {}).get("uid")) == str(role_id):
            legacy.unlink()


def _snapshot(owner_id: int, role_id: str) -> dict | None:
    for path in (_snapshot_path(owner_id, role_id), DATA_DIR / f"{owner_id}.json"):
        with contextlib.suppress(OSError, ValueError):
            snapshot = json.loads(path.read_text("utf-8"))
            if str((snapshot.get("result") or {}).get("user_info", {}).get("uid")) == str(role_id):
                return snapshot
    return None


async def reconcile(character_id: int) -> str:
    """Rebuild one role's import from the Heybox answer kept at import time.

    Nothing happens for a role that never imported or undid its import. A pool the plan no
    longer accepts loses its imported rows: official rows are the truth where the two overlap.
    """
    async with _lock, get_session() as session:
        character = await session.get(Character, character_id)
        if character is None or character.app_code != "endfield" or not character.role_id:
            return "no role"
        owner_id = await session.scalar(select(SkUser.owner_id).where(SkUser.id == character.account_id))
        snapshot = await asyncio.to_thread(_snapshot, owner_id, character.role_id)
        if not snapshot or not isinstance(snapshot.get("result"), dict):
            return "no snapshot"
        key = (GachaRecord.pool_id, GachaRecord.gacha_ts, GachaRecord.pos, GachaRecord.char_name, GachaRecord.is_free)
        before = sorted(tuple(row) for row in await session.execute(select(*key).where(*_imported(character_id))))
        if not before:
            return "nothing imported"
        plan, rows = await _build(session, character, snapshot["result"])
        if plan.error:
            return "snapshot rejected"
        after = sorted((row["pool_id"], row["gacha_ts"], row["pos"], row["char_name"], row["is_free"]) for row in rows)
        if after == before:
            return "unchanged"
        await _replace(session, character_id, rows)
        await session.commit()
    return f"{len(before)} -> {len(after)} rows"


_due: set[int] = set()
_reconciling: set[asyncio.Task] = set()


async def _reconcile_later(character_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    _due.discard(character_id)
    for attempt in range(RECONCILE_ATTEMPTS):
        try:
            outcome = await reconcile(character_id)
        except Exception as e:  # e.g. the database is busy with the sync that triggered this
            logger.warning(f"Heybox reconcile attempt {attempt + 1} failed: {type(e).__name__}")
            await asyncio.sleep(RECONCILE_DELAY)
            continue
        if outcome not in ("no role", "no snapshot", "nothing imported", "unchanged"):
            logger.info(f"Heybox import reconciled after new official rows: {outcome}")
        return


def _schedule_reconcile(character_id: int, delay: float = RECONCILE_DELAY) -> None:
    if character_id in _due:
        return
    _due.add(character_id)
    task = asyncio.get_running_loop().create_task(_reconcile_later(character_id, delay))
    _reconciling.add(task)
    task.add_done_callback(_reconciling.discard)


@event.listens_for(GachaRecord, "after_insert")
def _official_row_added(mapper, connection, target) -> None:
    """/zmd抽卡记录更新 saves its new rows through the ORM; the nightly sync is covered by the listener below."""
    if (target.pos or 0) >= 0 and target.character_id is not None and target.character_id not in _due:
        with contextlib.suppress(Exception):
            _schedule_reconcile(target.character_id)


async def _after_official_sync(owner_id: int, status: str, records: int, final: bool) -> None:
    if status in ("success", "partial") and records:
        async with get_session() as session:
            roles = [role.id for role in await _roles(owner_id, session)]
        for character_id in roles:
            _schedule_reconcile(character_id, 1)


if _after_official_sync not in auto_gacha.sync_listeners:
    auto_gacha.sync_listeners.append(_after_official_sync)


def summary(plan: Plan) -> str:
    if plan.error:
        return f"导入失败：{plan.error}"
    lines = []
    if plan.imported:
        first = min(row["ts"] for row in plan.rows)
        lines.append(
            f"小黑盒导入完成：补回 {len(plan.imported)} 个卡池、{sum(p.pulls for p in plan.imported)} 抽、"
            f"{sum(p.six_stars for p in plan.imported)} 个六星，最早到 {datetime.fromtimestamp(first, CN):%Y-%m-%d}"
        )
        lines.append("六星与每个六星的抽数来自小黑盒，四星不记具体干员；发 /zmd抽卡记录 查看，/zmd撤销小黑盒导入 可撤回")
    else:
        lines.append(
            f"小黑盒里没有比机器人已有记录更早的抽卡数据，无需导入（小黑盒 {plan.heybox_pulls} 抽，机器人已有 {plan.official_pulls} 抽）。"
            "如果你在小黑盒里很久没更新，先去终末地「抽卡分析」点一次更新再试"
        )
    for pool in plan.skipped[:6]:
        lines.append(f"未导入「{pool.pool_name}」：{pool.skipped}")
    return "\n".join(lines)


@import_cmd.handle()
async def _(event: MessageEvent, user_session: UserSession, session: async_scoped_session, arg: Message = CommandArg()) -> None:
    heybox_id = arg.extract_plain_text().strip()
    if not (heybox_id.isdigit() and 4 <= len(heybox_id) <= 12):
        await import_cmd.finish(
            "用法：/zmd导入小黑盒 小黑盒ID，例如 /zmd导入小黑盒 12345678\n"
            f"{WHERE_ID}。导入前请先在小黑盒的终末地「抽卡分析」里点一次更新。\n"
            "只能导入与你已绑定的终末地 UID 相同的小黑盒账号。"
        )
    user_id = user_session.user_id
    roles, reason = await _bound_roles(user_id, session)
    if not roles:
        await import_cmd.finish(reason, at_sender=True)
    if any(heybox_id == str(role.role_id) for role in roles):
        await import_cmd.finish(f"你填的是自己的游戏 UID。{WHERE_ID}，请换成小黑盒 ID 再试", at_sender=True)
    wait = COOLDOWN - (time.time() - _last_run.get(user_id, 0))
    if wait > 0:
        await import_cmd.finish(f"刚导入过，请 {int(wait // 60) + 1} 分钟后再试", at_sender=True)
    if _lock.locked():
        await import_cmd.finish("有其他人正在导入，请稍后再试", at_sender=True)
    await _react(REACTION_PROCESSING)
    try:
        async with _lock:
            try:
                result = await fetch_overview(heybox_id)
            except Exception as e:
                logger.warning(f"Heybox fetch failed: {type(e).__name__}: {e}")
                await _react(REACTION_FAIL)
                await import_cmd.finish("从小黑盒读取数据失败，请稍后再试", at_sender=True)
            # The Heybox account names its role; the import goes to that role, whichever of the member's it is.
            heybox_uid = str((result.get("user_info") or {}).get("uid"))
            character = next((role for role in roles if str(role.role_id) == heybox_uid), roles[0])
            role_id = str(character.role_id)  # ORM objects expire at commit; keep plain values for afterwards
            plan = await run_import(session, character, result)
            if plan.rows and not plan.error:
                _last_run[user_id] = time.time()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_keep_raw, user_id, role_id, heybox_id, result)
        logger.info(
            f"Heybox import: error={plan.error[:16] or None} pools={len(plan.imported)} rows={len(plan.rows)} "
            f"skipped={len(plan.skipped)} heybox_pulls={plan.heybox_pulls} official_pulls={plan.official_pulls}"
        )
    except MatcherException:
        raise
    except Exception as e:
        await session.rollback()
        logger.opt(exception=True).error(f"Heybox import failed while building or writing rows: {type(e).__name__}: {e}")
        await _react(REACTION_FAIL)
        await import_cmd.finish("导入时程序出错，没有写入任何数据。问题已记录，请稍后再试", at_sender=True)
    await _react(REACTION_FAIL if plan.error else REACTION_DONE)
    await UniMessage.text(summary(plan)).send(at_sender=True)


@undo_cmd.handle()
async def _(user_session: UserSession, session: async_scoped_session) -> None:
    user_id = user_session.user_id
    roles, reason = await _bound_roles(user_id, session)
    if not roles:
        await undo_cmd.finish(reason, at_sender=True)
    role_ids = [role.id for role in roles]
    removed = (await session.execute(delete(GachaRecord).where(GachaRecord.character_id.in_(role_ids), GachaRecord.pos < 0))).rowcount
    await session.commit()
    _last_run.pop(user_id, None)
    await _react(REACTION_DONE)
    await undo_cmd.finish(f"已撤销小黑盒导入，删除 {removed} 条导入记录；官方记录不受影响" if removed else "你没有从小黑盒导入过记录", at_sender=True)
