"""终末地抽卡欧非榜，覆盖所有通过本机器人绑定了森空岛的人。

/zmd欧非榜 列出限定角色池和武器池各自最欧的十人和最非的十人；/角色欧非榜 和 /武器欧非榜
各列出其中一个榜（榜很长时只列两端）。

榜不分群（机器人现在对任何群开放，小群单独一个榜意义不大）。凡是绑定了、
又没有选择退出的人都会上榜，在任何群和私聊里都一样。在显示榜单的这个群里的成员，用他在本群的
名字显示，并标注“本群”；其他人用游戏角色名显示，这样 QQ 名字不会从一个群流到另一个群。

统计沿用 Skland 自己的分组（group_ef_gacha_records）和 /zmd抽卡记录 的 UP 计算
（plugins/skland_limited.py：限定寻访按当期 UP，辉光庆典和重构寻访按每个限定干员），所以数字和
/zmd抽卡记录 一致。

统计所有人意味着要读每一条保存的记录（六百名成员约一百万行），一条指令根本吃不消。所以每个成员的
数字会保存下来（插件数据目录里的 snapshot.json，以及内存里），连同算出它们所依据的东西：角色、
记录条数、最新一条记录。一条指令只向数据库要每个成员的这三项，只重算有变化的人。什么算 UP 取决于
卡池表，而卡池表随游戏更新变化：保存的数字带着卡池表的指纹，指纹不同时所有人都重算（而且每晚
无论如何重算一次）；卡池表没加载时什么都不算。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from nonebot import get_driver, logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import MatcherException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_skland")  # 要在 plugins.skland_limited 之前，它会从这里导入
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_apscheduler")

from plugins import ef_theme, skland_limited
from plugins.strict_command import strict

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.data_source import ef_gacha_pool_data
from nonebot_plugin_skland.model import Character, CharacterDefault, GachaRecord
from nonebot_plugin_skland.services.gacha import group_ef_gacha_records
from sqlalchemy import func, select
from plugins.shared import REACTION_DONE, REACTION_FAIL, REACTION_PROCESSING, SLOW_UPLOAD, upload_timed_out, write_json, react as _react
from plugins.skland_roles import member_numbers, owner_id_of

__plugin_meta__ = PluginMetadata(
    name="Endfield gacha leaderboard",
    description="Ranks everyone who has bound Skland by Endfield pulls per UP operator (limited pools) and per UP weapon.",
    usage="/zmd欧非榜 | /角色欧非榜 | /武器欧非榜 | /zmd欧非榜 退出 | /zmd欧非榜 加入",
    type="application",
)

TOP = 10  # 总览里欧 / 非每张表的行数
MIN_UP = 4  # UP 少于这个数，样本太小，不参与排名（设成 2 时会让一对特别欧或特别非的人登顶）
WIDTH = 1000
FULL_COLUMNS = 3  # 完整榜单按列从上往下读
FULL_ENDS = 150  # 上榜人数超过这个数两倍的完整榜单，只从两端各列这么多人
NAMELESS = "未命名"  # 不知道名字的游戏角色
REFRESH_AFTER = 600  # 保存的数字用多少秒后，再去核对一次保存的记录
CHUNK = 4  # 一次读取并计算记录的成员数；中间机器人可以回应别人（设成 8 时每次要占用 0.85 秒）
COLD_START_DELAY = 90  # 启动后多少秒做第一次计算（此时还没有保存的数字）
OPTOUT_FILE = store.get_plugin_data_file("optout.json")
SNAPSHOT_FILE = store.get_plugin_data_file("snapshot.json")
SNAPSHOT_VERSION = 3  # 计算方法变了就加一，这样之前保存的数字都不会再用

driver = get_driver()

gacha_rank = on_command(
    "zmd欧非榜", aliases={"ef欧非榜", "终末地欧非榜", "欧非榜"}, rule=strict, priority=5, block=True
)
char_rank = on_command(
    "zmd角色欧非榜", aliases={"ef角色欧非榜", "终末地角色欧非榜", "角色欧非榜"}, rule=strict, priority=5, block=True
)
weapon_rank = on_command(
    "zmd武器欧非榜", aliases={"ef武器欧非榜", "终末地武器欧非榜", "武器欧非榜"}, rule=strict, priority=5, block=True
)

Signature = tuple[int, int, int]  # 成员的数字是依据什么算的：默认角色、记录条数、最新一条记录


@dataclass
class Stat:
    """一个成员的数字，来自其默认终末地角色保存的记录。"""

    up_avg: float
    up_count: int
    special_pulls: int
    weapon_avg: float
    weapon_up: int


@dataclass
class Entry(Stat):
    ids: frozenset[str] = frozenset()  # 这个成员在聊天里被认出的那些号码
    name: str = NAMELESS
    local: bool = False  # 是显示榜单的这个群的成员


@dataclass(frozen=True)
class Board:
    pool: str  # 角色池 / 武器池
    command: str
    english: str
    basis: str
    target: str  # 一个 UP 指什么，带量词
    measure: str
    empty: str
    avg: Callable[[Entry], float]
    count: Callable[[Entry], int]

    def luck(self, entries: list[Entry]) -> Luck:
        """欧气的两个基准，取自上榜（UP 不少于 MIN_UP）的所有人。"""
        eligible = [e for e in entries if self.count(e) >= MIN_UP]
        ups = sum(self.count(e) for e in eligible)
        if not ups:
            return Luck(0.0, 1.0)
        mean = sum(self.avg(e) * self.count(e) for e in eligible) / ups
        spread = (sum(self.count(e) * (self.avg(e) - mean) ** 2 for e in eligible) / len(eligible)) ** 0.5
        return Luck(mean, spread or 1.0)

    def ranked(self, entries: list[Entry]) -> list[Entry]:
        """最欧的在前，按欧气排（Luck）；UP 少于 MIN_UP 的不参与排名。"""
        luck = self.luck(entries)
        return sorted((e for e in entries if self.count(e) >= MIN_UP), key=lambda e: (-luck.of(self.avg(e), self.count(e)), -self.count(e)))


@dataclass(frozen=True)
class Luck:
    """欧气 =（全服平均 − 他的平均）× √UP 数 ÷ 正常波动。

    只按平均数排，两端全是 UP 刚够 MIN_UP 的人：UP 少，平均数本来就忽高忽低。乘上 √UP 数，二十个 UP
    一直比平均少用二十多抽，和四个 UP 平均少用六十抽一样难得；再除以正常波动，0 是平均水平，
    +2 以上已经很少见，+3 以上极少见，负数同理是非（抽卡有保底，两头并不对称，所以只说“很少见”，
    不说几里挑一）。同一个 UP 数时，顺序和按平均数排一样。
    """

    mean: float  # 上榜所有人合起来，平均多少抽出一个 UP
    spread: float  # 出一个 UP 要多少抽，平时上下差多少（由上榜所有人的数字估出）

    def of(self, avg: float, count: int) -> float:
        return (self.mean - avg) * count**0.5 / self.spread


def luck_text(value: float) -> str:
    """欧气写出来的样子：+2.4、−1.3（用真正的减号，和加号一样宽）。"""
    return f"{value:+.1f}".replace("-", "−")


CHARACTERS = Board(
    pool="角色池",
    command="/角色欧非榜",
    english="Operator",
    basis="限定池（含庆典、重构寻访）「平均多少抽出一个 UP 干员」，免费十连出的 UP 不计",
    target="一个 UP 干员",
    measure="个",
    empty="还没有可统计的终末地限定池抽卡记录",
    avg=lambda e: e.up_avg,
    count=lambda e: e.up_count,
)
WEAPONS = Board(
    pool="武器池",
    command="/武器欧非榜",
    english="Weapon",
    basis="武器池「平均多少抽出一把 UP 武器」",
    target="一把 UP 武器",
    measure="把",
    empty="还没有可统计的终末地武器池抽卡记录",
    avg=lambda e: e.weapon_avg,
    count=lambda e: e.weapon_up,
)


def _load_optout() -> set[str]:
    # 文件不存在说明没人选择退出；文件读不了时，绝不能悄悄把人重新列上榜。
    if not OPTOUT_FILE.exists():
        return set()
    return set(json.loads(OPTOUT_FILE.read_text("utf-8")))


def _save_optout(qqs: set[str]) -> None:
    write_json(OPTOUT_FILE, sorted(qqs))




# ---------------------------------------------------------------- 所有人的数字，在指令之间保存

Kept = dict[int, tuple[Signature, Stat]]  # 成员（机器人自己的用户 id）-> 数字所依据的东西，以及数字


class NotReady(Exception):
    """卡池表还没加载：判断不了什么算 UP，所以什么都不算。"""


def pools_key() -> str:
    """卡池表所认定的 UP 的指纹；还没加载时为空。"""
    tables = ef_gacha_pool_data.pool_table
    if not tables:
        return ""
    said = sorted((pool_id, str(table.pool_type), str(table.up6_name or ""), sorted(table.up_six_char_ids)) for pool_id, table in tables.items())
    return hashlib.sha256(json.dumps(said, ensure_ascii=False).encode()).hexdigest()[:16]


def load_snapshot(path: Path) -> tuple[str, Kept]:
    """磁盘上保存的数字，以及计算时所依据的卡池表指纹；没有保存、或是按别的规则算的，就什么都不返回。"""
    try:
        saved = json.loads(path.read_text("utf-8"))
        if saved.get("version") != SNAPSHOT_VERSION:
            return "", {}
        return str(saved["pools"]), {int(owner): ((role, count, newest), Stat(*numbers)) for owner, (role, count, newest, *numbers) in saved["members"].items()}
    except FileNotFoundError:
        return "", {}
    except Exception as e:  # 宁可重算也不信它
        logger.warning(f"Endfield gacha leaderboard: the kept numbers could not be read ({type(e).__name__}); everyone is counted again")
        return "", {}


def save_snapshot(kept: Kept, pools: str, path: Path) -> None:
    members = {str(owner): [*signature, *asdict(stat).values()] for owner, (signature, stat) in kept.items()}
    write_json(path, {"version": SNAPSHOT_VERSION, "pools": pools, "members": members})


def stale(now: dict[int, Signature], kept: Kept) -> list[int]:
    """需要（重新）计算数字的成员：新成员，以及角色或记录有变化的成员。"""
    return [owner for owner, signature in now.items() if owner not in kept or kept[owner][0] != signature]


def stat_of(records: list[GachaRecord]) -> Stat:
    stats = group_ef_gacha_records(records)
    # 与 /zmd抽卡记录 用同样的 UP 查法；本地表里没有的武器池，每个 6★ 都算歪。
    for pool in stats.weapon_pools:
        if local_pool := ef_gacha_pool_data.get_pool(pool.pool_id):
            pool.up_six_chars = local_pool.up_six_char_ids
    special, others = skland_limited.split(stats)
    skland_limited.mark(special + others)
    return Stat(
        up_avg=skland_limited.up_avg(special, others) or 0.0,  # 没有付费 UP 时不上榜（付费 UP 少于 MIN_UP）
        up_count=skland_limited.paid_up_count(special + others),
        special_pulls=sum(p.paid_pulls for p in special + others),
        weapon_avg=skland_limited.weapon_up_avg(stats.weapon_pools),
        weapon_up=skland_limited.up_count(stats.weapon_pools),
    )


_kept_pools, _kept = load_snapshot(SNAPSHOT_FILE)  # 数字计算时所依据的卡池表指纹，以及数字
_checked_at: float | None = None  # 上次拿保存的数字核对记录时的 time.monotonic()
_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()


def _of_default_roles(*columns):
    """对每个成员默认终末地角色保存的记录做的查询。"""
    return (
        select(*columns)
        .join(GachaRecord, GachaRecord.character_id == CharacterDefault.character_id)
        .where(CharacterDefault.app_code == "endfield")
    )


async def _refresh(everyone: bool = False) -> None:
    """让保存的数字跟上保存的记录；`everyone` 为真时所有成员都重算。调用时须持有 _lock。"""
    global _checked_at, _kept_pools
    pools = pools_key()
    if not pools:
        raise NotReady
    everyone = everyone or pools != _kept_pools
    async with get_session() as session:
        rows = (
            await session.execute(
                _of_default_roles(CharacterDefault.owner_id, CharacterDefault.character_id, func.count(GachaRecord.id), func.max(GachaRecord.id))
                .group_by(CharacterDefault.owner_id, CharacterDefault.character_id)
            )
        ).all()
    now: dict[int, Signature] = {owner: (role, count, newest) for owner, role, count, newest in rows}
    todo = list(now) if everyone else stale(now, _kept)
    gone = [owner for owner in _kept if owner not in now]
    for owner in gone:
        del _kept[owner]
    started = time.monotonic()
    for at in range(0, len(todo), CHUNK):
        chunk = todo[at : at + CHUNK]
        async with get_session() as session:
            found = (await session.execute(_of_default_roles(CharacterDefault.owner_id, GachaRecord).where(CharacterDefault.owner_id.in_(chunk)))).all()
        records: dict[int, list[GachaRecord]] = {}
        for owner, record in found:
            records.setdefault(owner, []).append(record)
        for owner, theirs in records.items():
            _kept[owner] = (now[owner], stat_of(theirs))
        await asyncio.sleep(0)  # 长时间的计算不能让机器人没法回应
    _checked_at = time.monotonic()
    if todo or gone or pools != _kept_pools:
        save_snapshot(_kept, pools, SNAPSHOT_FILE)
    _kept_pools = pools
    if len(todo) > CHUNK:
        logger.info(f"Endfield gacha leaderboard: counted {len(todo)} of {len(now)} members in {time.monotonic() - started:.0f}s")


async def _stats() -> dict[int, Stat]:
    async with _lock:
        if _checked_at is None or time.monotonic() - _checked_at > REFRESH_AFTER:
            await _refresh()
        return {owner: stat for owner, (_, stat) in _kept.items()}


@scheduler.scheduled_job("cron", hour=4, minute=30, timezone="Asia/Shanghai", id="skland_gacha_rank_recount", misfire_grace_time=3600)
async def _recount() -> None:
    """每晚把所有人重算一次：什么算 UP 取决于卡池表，而卡池表随游戏更新变化。"""
    with contextlib.suppress(NotReady):  # 那就等有人查询时再算
        async with _lock:
            await _refresh(everyone=True)


async def _first_count() -> None:
    await asyncio.sleep(COLD_START_DELAY)
    try:
        await _stats()
    except NotReady:
        logger.info("Endfield gacha leaderboard: the pool tables are not loaded yet; the first count waits for a command")
    except Exception as e:
        logger.warning(f"Endfield gacha leaderboard: the first count failed, the next command tries again: {type(e).__name__}: {e}")


@driver.on_startup
async def _count_where_nothing_is_kept() -> None:
    if not _kept:  # 这个版本第一次启动：在有人查询之前先把耗时的计算做完
        task = asyncio.create_task(_first_count())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------- 谁上榜，用什么名字


def entries_of(stats: dict[int, Stat], ids: dict[int, set[str]], roles: dict[int, str], cards: dict[str, str], optout: set[str]) -> list[Entry]:
    """要排名的成员：有数字、仍在绑定、且没有选择退出的所有人。

    ids：每个成员在聊天里被认出的号码；roles：每个成员的游戏角色名；
    cards：号码 -> 名字，用于显示榜单的这个群的成员（私聊里没有）。
    """
    entries = []
    for owner, stat in stats.items():
        known = ids.get(owner)
        if not known or known & optout or f"o{owner}" in optout:
            continue
        here = next((number for number in sorted(known) if number in cards), None)
        name = (cards[here] if here is not None else "") or (roles.get(owner) or "").strip() or NAMELESS
        entries.append(Entry(**asdict(stat), ids=frozenset(known), name=name, local=here is not None))
    return entries


async def _cards(bot: Bot, event: MessageEvent) -> dict[str, str]:
    """号码 -> 群成员在本群的名字；私聊里、或拿不到成员列表时为空。"""
    if not isinstance(event, GroupMessageEvent):
        return {}
    try:
        members = await bot.get_group_member_list(group_id=event.group_id)
    except Exception as e:  # 榜单仍然值得显示：只是没人会被标为本群
        logger.info(f"Endfield gacha leaderboard: no member list for this group ({type(e).__name__}); nobody is marked 本群")
        return {}
    return {str(m["user_id"]): str(m.get("card") or m.get("nickname") or "").strip() for m in members}


async def _collect(bot: Bot, event: MessageEvent) -> list[Entry]:
    stats = await _stats()
    if not stats:
        return []
    async with get_session() as session:
        ids = await member_numbers(session)
        roles = (
            await session.execute(
                select(CharacterDefault.owner_id, Character.nickname)
                .join(Character, Character.id == CharacterDefault.character_id)
                .where(CharacterDefault.app_code == "endfield")
            )
        ).all()
    return entries_of(stats, ids, {owner: name for owner, name in roles}, await _cards(bot, event), _load_optout())


# ---------------------------------------------------------------- 图片

CSS = """
body { width: %dpx; padding: 26px; }
.num { font-family: var(--ef-num); font-variant-numeric: tabular-nums; }
.summary { display: flex; gap: 12px; margin-top: 16px; }
.summary div { flex: 1; padding: 10px 16px 12px; background: var(--ef-panel); border: 1px solid var(--ef-line); border-top: 3px solid var(--ef-ink);
  font-size: 16px; color: var(--ef-sub); }
.summary b { display: block; margin-top: 2px; font-family: var(--ef-num); font-weight: 700; font-size: 40px; line-height: 1.05; color: var(--ef-ink); }
.summary div.hl b { display: inline-block; padding: 0 8px; background: var(--ef-yellow); border: 1px solid var(--ef-ink); }
.mine { position: relative; margin-top: 12px; padding: 11px 16px 11px 24px; background: var(--ef-dark); color: #f4f4f0; font-size: 19px; }
.mine:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 8px; background: var(--ef-yellow); }
.mine b { font-family: var(--ef-num); font-weight: 700; font-size: 24px; color: var(--ef-yellow); padding: 0 2px; }
.mine i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 22px; }
.mine div + div { margin-top: 5px; }
.mine em { font-style: normal; color: #b9b9b2; }
.pair { display: flex; gap: 14px; align-items: flex-start; }
.pair .ef-sec { flex: 1; min-width: 0; }
.unlucky > h3:before { background: var(--ef-ink); }
table { width: 100%%; border-collapse: collapse; table-layout: fixed; }
td { padding: 7px 12px; border-top: 1px solid var(--ef-line-2); font-size: 19px; white-space: nowrap; }
tr:first-child td { border-top: 0; }
td.rk { width: 48px; padding-right: 0; }
td.rk span { display: inline-block; width: 28px; height: 28px; line-height: 27px; text-align: center; font-family: var(--ef-num); font-weight: 700;
  font-size: 18px; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
tr:nth-child(1) td.rk span { background: var(--ef-yellow); border-color: var(--ef-ink); }
tr:nth-child(2) td.rk span { background: var(--ef-silver); color: var(--ef-ink); border-color: var(--ef-ink); } tr:nth-child(3) td.rk span { background: var(--ef-copper); color: #fff; border-color: var(--ef-ink); }
td.nm { overflow: hidden; text-overflow: ellipsis; }
td.nm s, .grid s { text-decoration: none; flex: none; margin-left: 7px; padding: 0 5px; font-size: 12px; font-weight: 400; line-height: 1.5;
  color: var(--ef-ink); background: var(--ef-panel); border: 1px solid var(--ef-ink); vertical-align: 2px; }
td.val { width: 104px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 25px; line-height: 1; }
td.val small { font-family: var(--ef-cjk); font-size: 14px; color: var(--ef-sub); font-weight: 400; margin-left: 3px; }
tr:nth-child(1) td.val b { font-weight: 700; background: var(--ef-yellow); padding: 0 4px; }
td.val b { font-weight: 700; }
td.cnt { width: 112px; text-align: right; font-size: 14px; color: var(--ef-sub); line-height: 1.25; }
td.cnt small { display: block; font-size: 12px; }
td.cnt b { font-family: var(--ef-num); font-weight: 600; font-size: 18px; color: var(--ef-ink); margin-right: 2px; }
tr.me td { background: rgba(255, 225, 0, .3); }
tr.me td.nm { font-weight: 700; }
tr.me td:first-child { box-shadow: inset 4px 0 0 var(--ef-ink); }
.grid { display: grid; grid-auto-flow: column; grid-template-columns: repeat(%d, 1fr); gap: 0 18px; padding: 8px 16px 10px; }
.grid span { display: flex; align-items: center; gap: 10px; font-size: 17px; padding: 5px 6px; border-bottom: 1px solid var(--ef-line-2); overflow: hidden; }
.grid span.me { background: var(--ef-yellow); box-shadow: inset 0 0 0 1px var(--ef-ink); }
.grid i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 17px; color: var(--ef-sub); width: 30px; flex: none; text-align: right; }
.grid span.me i { color: var(--ef-ink); }
.grid em { font-style: normal; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.grid s { margin-left: -3px; }
.grid u { margin-left: auto; text-decoration: none; font-size: 13px; color: var(--ef-sub); flex: none; }
.grid q { width: 52px; text-align: right; font-family: var(--ef-num); font-size: 14px; color: var(--ef-sub); flex: none; }
.grid q:before, .grid q:after { content: none; }
.grid b { width: 52px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 20px; flex: none; }
.grid span.top i { color: var(--ef-ink); font-weight: 700; }
.how { margin-top: 16px; padding-bottom: 14px; }
.how .eq { padding: 8px 22px 0; font-size: 21px; }
.how math[display="block"] { display: block; text-align: left; margin: 0; }
.how mtext, .how mo, .how mn { font-family: var(--ef-cjk); }
.how p { margin: 12px 22px 0; font-size: 15px; line-height: 1.7; color: var(--ef-sub); }
.how .nums { margin: 10px 22px 0; display: flex; gap: 28px; font-size: 16px; }
.how .nums b { font-family: var(--ef-num); font-size: 20px; }
.skip { margin-top: 16px; padding: 9px 16px; text-align: center; font-size: 16px; color: var(--ef-sub); background: var(--ef-panel-2); border: 1px dashed var(--ef-line); }
"""

HERE = "<s>本群</s>"


def _name(entry: Entry) -> str:
    return escape(entry.name) + (HERE if entry.local else "")


def _rows(board: Board, entries: list[Entry], luck: Luck, requester: str) -> str:
    """按显示的顺序；每张表从 1 数起。"""
    return "".join(
        f'<tr class="{"me" if requester in e.ids else ""}"><td class="rk"><span>{shown}</span></td><td class="nm">{_name(e)}</td>'
        f'<td class="val"><b>{board.avg(e):.1f}</b><small>抽</small></td>'
        f'<td class="cnt"><b>{board.count(e)}</b> {board.measure} UP<small>欧气 {luck_text(luck.of(board.avg(e), board.count(e)))}</small></td></tr>'
        for shown, e in enumerate(entries, 1)
    )


def _tables(board: Board, ranked: list[Entry], requester: str) -> str:
    """某个榜最欧的十人和最非的十人，并排显示。"""

    luck = board.luck(ranked)

    def table(title: str, kind: str, entries: list[Entry]) -> str:
        return (
            f'<div class="ef-sec {kind}"><h3>{board.pool} · {title}<small>{kind.upper()} · TOP {TOP}</small></h3>'
            f"<table>{_rows(board, entries, luck, requester)}</table></div>"
        )

    return f'<div class="pair">{table("欧皇榜", "lucky", ranked[:TOP])}{table("非酋榜", "unlucky", ranked[::-1][:TOP])}</div>'


def _mine(board: Board, ranked: list[Entry], entries: list[Entry], requester: str) -> str:
    """一行字，写出查询者在某个榜上的名次；没有保存的记录时为空。"""
    if not any(requester in e.ids for e in entries):
        return ""
    label = f"你的{board.pool}排名："
    pos = next((i for i, e in enumerate(ranked, 1) if requester in e.ids), None)
    if pos is None:
        return f"<div>{label}<em>UP 不足 {MIN_UP} {board.measure}，暂未上榜</em></div>"
    me = ranked[pos - 1]
    return (
        f"<div>{label}第 <b>{pos}</b> / <i>{len(ranked)}</i> 名，欧气 <b>{luck_text(board.luck(ranked).of(board.avg(me), board.count(me)))}</b>，"
        f"平均 <b>{board.avg(me):.1f}</b> 抽出{board.target}（共 <i>{board.count(me)}</i> {board.measure}）</div>"
    )


def _average(board: Board, ranked: list[Entry]) -> float:
    """平均 UP 抽数：上榜所有人合起来算（总抽数 ÷ 总 UP 数），和欧气用的全服平均是同一个数。"""
    return board.luck(ranked).mean


# 公式用 MathML 排（浏览器自带，和 LaTeX 排出来的一样：分数线、随内容伸长的根号、上标）
def _t(words: str) -> str:
    return f"<mtext>{words}</mtext>"


def _o(sign: str) -> str:
    return f"<mo>{sign}</mo>"


def _sqrt(inside: str) -> str:
    return f"<msqrt><mrow>{inside}</mrow></msqrt>"


def _frac(top: str, bottom: str) -> str:
    return f"<mfrac><mrow>{top}</mrow><mrow>{bottom}</mrow></mfrac>"


def _math(body: str, block: bool = True) -> str:
    return f'<math{" display=\"block\"" if block else ""}><mrow>{body}</mrow></math>'


def _how(boards: list[tuple[Board, Luck]]) -> str:
    """欧气怎么算：公式，以及这次用到的两个基准数，谁都能自己算一遍。"""
    numbers = "".join(
        f"<div>{b.pool}：全服平均 <b>{luck.mean:.1f}</b> 抽，正常波动 <b>{luck.spread:.1f}</b> 抽</div>" for b, luck in boards
    )
    return f"""<div class="ef-sec how"><h3>欧气怎么算<small>HOW IT WORKS</small></h3>
<div class="eq">{_math(_t("欧气") + _o("=") + _frac(_o("(") + _t("全服平均") + _o("−") + _t("他的平均") + _o(")") + _o("×") + _sqrt(_t("UP 数")), _t("正常波动")))}</div>
<div class="eq">{_math(_t("全服平均") + _o("=") + _frac(_o("∑") + _t("每人的抽数"), _o("∑") + _t("每人的 UP 数")))}</div>
<div class="eq">{_math("<msup><mrow>" + _t("正常波动") + "</mrow><mn>2</mn></msup>" + _o("=") + _frac(_o("∑") + _t("UP 数") + _o("×") + "<msup><mrow>" + _o("(") + _t("全服平均") + _o("−") + _t("他的平均") + _o(")") + "</mrow><mn>2</mn></msup>", _t("上榜人数")))}</div>
<p>正常波动是从榜上所有人的数字算出来的：每个人的平均离全服平均差多少，平方后乘他的 UP 数，全部加起来除以上榜人数，得到的是正常波动的平方，开平方就是正常波动。
这样全体的欧气以 0 为中心、上下大约差 1：0 是平均水平，越大越欧，越小越非；+2 以上已经很少见，+3 以上极少见，负数同理。
乘上 {_math(_sqrt(_t('UP 数')), block=False)} 是因为 UP 越多，平均数越稳：二十个 UP 一直比平均少用二十多抽，和四个 UP 平均少用六十抽一样难得。</p>
<div class="nums">{numbers}</div></div>"""


def _page(code: str, title: str, subtitle: str, body: str, note: str, how: str = "") -> str:
    """how：欧气算法那一块（_how），放在榜单之后、页脚之前。"""
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS % (WIDTH, FULL_COLUMNS)}</style></head><body class="ef">'
        + ef_theme.head(code, f"终末地 · <em>{title}</em>", subtitle, f"DATE<b>{datetime.now(ZoneInfo('Asia/Shanghai')):%y-%m-%d}</b>")
        + body
        + how
        + ef_theme.foot(
            f"数据来自已保存的抽卡记录（每天 01:00 自动更新，不含免费十连和当前垫抽），至少出过 {MIN_UP} 个 UP 才参与排名<br>"
            f"榜单不分群：本群成员显示群名片并标有「本群」，其他人显示游戏内的角色名<br>"
            f"{note}不想上榜可发送 /zmd欧非榜 退出，重新加入发送 /zmd欧非榜 加入"
        )
        + "</body></html>"
    )


def render_overview(entries: list[Entry], requester: str) -> str | None:
    """两个榜，各列前十和后十；角色榜上一个能排名的人都没有时为 None。"""
    boards = [(b, ranked) for b in (CHARACTERS, WEAPONS) if (ranked := b.ranked(entries))]
    if not boards or boards[0][0] is not CHARACTERS:
        return None
    tiles = "".join(
        f'<div>{b.pool}上榜<b>{len(ranked)}</b></div><div class="hl">{b.pool}平均 UP 抽数<br><b>{_average(b, ranked):.1f}</b></div>'
        for b, ranked in boards
    )
    if len(boards) == 1:
        tiles += f"<div>有抽卡记录<b>{len(entries)}</b></div>"
    mine = "".join(_mine(b, ranked, entries, requester) for b, ranked in boards)
    return _page(
        "Endfield · Headhunting Luck Board",
        "欧非榜",
        f"所有绑定用户一起排名，不分群；按欧气排：平均多少抽出一个 UP，同时看出过多少个 UP；每个榜只列前 {TOP} 名和后 {TOP} 名",
        f'<div class="summary">{tiles}</div>'
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + "".join(_tables(b, ranked, requester) for b, ranked in boards),
        f"完整榜单发送 {CHARACTERS.command} 或 {WEAPONS.command}；",
        _how([(b, b.luck(ranked)) for b, ranked in boards]),
    )


def render_board(board: Board, entries: list[Entry], requester: str) -> str | None:
    """一个榜：榜上所有人，或者超过 FULL_ENDS 两倍时只列两端；榜上没人能排名时为 None。"""
    ranked = board.ranked(entries)
    if not ranked:
        return None
    numbered = list(enumerate(ranked, 1))
    luck = board.luck(ranked)

    def section(title: str, part: list[tuple[int, Entry]], from_worst: bool = False) -> str:
        """part：（在整个榜上的名次，成员）。from_worst：非酋那一段，从最非的数起，第 1 就是最非的。"""
        shown = list(enumerate((e for _, e in part[::-1]), 1)) if from_worst else part
        cells = "".join(
            f'<span class="{"me" if requester in e.ids else "top" if n <= 3 else ""}"><i>{n}</i><em>{escape(e.name)}</em>{HERE if e.local else ""}'
            f"<u>{board.count(e)} {board.measure}</u><b>{board.avg(e):.1f}</b><q>{luck_text(luck.of(board.avg(e), board.count(e)))}</q></span>"
            for n, e in shown
        )
        rows = -(-len(part) // FULL_COLUMNS)
        return (
            f'<div class="ef-sec rest"><h3>{title}<small class="cjk">名次 · 名字 · UP 数 · 平均抽数 · 欧气</small></h3>'
            f'<div class="grid" style="grid-template-rows: repeat({rows}, auto)">{cells}</div></div>'
        )

    if len(ranked) > 2 * FULL_ENDS:
        listing = (
            section(f"欧皇榜 · 前 {FULL_ENDS} 名", numbered[:FULL_ENDS])
            + f'<div class="skip">中间 {len(ranked) - 2 * FULL_ENDS} 人未列出</div>'
            + section(f"非酋榜 · 最非的 {FULL_ENDS} 名", numbered[-FULL_ENDS:], from_worst=True)
        )
    else:
        listing = section("完整榜单", numbered)
    mine = _mine(board, ranked, entries, requester)
    return _page(
        f"Endfield · {board.english} Luck Board",
        f"{board.pool}欧非榜",
        f"所有绑定用户一起排名，不分群；按欧气排：{board.basis}，同时看出过多少个 UP",
        f"""<div class="summary">
    <div>参与排名<b>{len(ranked)}</b></div>
    <div>最欧<b>{board.avg(ranked[0]):.1f}</b></div>
    <div>最非<b>{board.avg(ranked[-1]):.1f}</b></div>
    <div class="hl">平均 UP 抽数<br><b>{_average(board, ranked):.1f}</b></div>
  </div>"""
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + listing,
        "前后十名总览发送 /zmd欧非榜；",
        _how([(board, luck)]),
    )


async def _send(matcher, bot: Bot, event: MessageEvent, render: Callable[[list[Entry], str], str | None], empty: str) -> None:
    await _react(REACTION_PROCESSING)
    try:
        try:
            entries = await _collect(bot, event)
        except NotReady:
            await _react(REACTION_FAIL)
            await matcher.finish("卡池数据还在加载，请过一会儿再试")
        html = render(entries, str(event.user_id))
        if html is None:
            await _react(REACTION_DONE)
            await matcher.finish(empty)
        image = await ef_theme.render_page(html, WIDTH, height=600)
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:  # 出图或发送出错：至少告诉他没成功，不能一声不响
        if upload_timed_out(e):  # 图已经做好，只是上传太久
            logger.warning("Endfield gacha leaderboard: picture upload timed out")
            await matcher.finish(SLOW_UPLOAD)
        logger.warning(f"Endfield gacha leaderboard failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await matcher.finish("欧非榜生成失败，请稍后再试")
    await _react(REACTION_DONE)


@gacha_rank.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    qq = str(event.user_id)

    if action in ("退出", "加入"):
        async with get_session() as session:
            owner = await owner_id_of(session, qq)
        # 号码之外也记下“这个人”（o<所有者 id>）：他的号以后变了（占位编号换成认出的 QQ 号），照样不上榜
        marks = {qq} | ({f"o{owner}"} if owner is not None else set())
        optout = _load_optout()
        if action == "退出":
            optout |= marks
        else:
            optout -= marks
        _save_optout(optout)
        await _react(REACTION_DONE)
        await gacha_rank.finish("已退出欧非榜，不会再出现在榜单上" if action == "退出" else "已重新加入欧非榜")

    await _send(gacha_rank, bot, event, render_overview, CHARACTERS.empty)


@char_rank.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    await _send(char_rank, bot, event, lambda entries, qq: render_board(CHARACTERS, entries, qq), CHARACTERS.empty)


@weapon_rank.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    await _send(weapon_rank, bot, event, lambda entries, qq: render_board(WEAPONS, entries, qq), WEAPONS.empty)
