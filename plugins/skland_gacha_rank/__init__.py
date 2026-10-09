"""Group-scoped Endfield gacha luck leaderboards.

/zmd欧非榜 shows the ten luckiest and ten unluckiest members for the limited character pools and for the
weapon pools; /角色欧非榜 and /武器欧非榜 list everyone on one of the two boards.

Stats reuse Skland's own grouping and averaging (group_ef_gacha_records), so the
numbers match /zmd抽卡记录. Only members of the current group who have bound
Skland and have not opted out are ranked.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape

from nonebot import logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import MatcherException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from plugins import ef_theme
from plugins.strict_command import strict

require("nonebot_plugin_skland")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage, message_reaction
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.data_source import ef_gacha_pool_data
from nonebot_plugin_skland.model import CharacterDefault, GachaRecord
from nonebot_plugin_skland.services.gacha import group_ef_gacha_records
from nonebot_plugin_user.models import Bind
from sqlalchemy import select

__plugin_meta__ = PluginMetadata(
    name="Endfield gacha leaderboard",
    description="Ranks group members by Endfield pulls per UP operator (limited pools) and per UP weapon.",
    usage="/zmd欧非榜 | /角色欧非榜 | /武器欧非榜 | /zmd欧非榜 退出 | /zmd欧非榜 加入",
    type="application",
)

PLATFORM = "QQClient"
TOP = 10  # rows of each lucky / unlucky table on the overview
MIN_UP = 2  # fewer UPs than this is too small a sample to rank
WIDTH = 1000
FULL_COLUMNS = 3  # the full boards read down each column
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"
OPTOUT_FILE = store.get_plugin_data_file("optout.json")

gacha_rank = on_command(
    "zmd欧非榜", aliases={"ef欧非榜", "终末地欧非榜", "欧非榜"}, rule=strict, priority=5, block=True
)
char_rank = on_command(
    "zmd角色欧非榜", aliases={"ef角色欧非榜", "终末地角色欧非榜", "角色欧非榜"}, rule=strict, priority=5, block=True
)
weapon_rank = on_command(
    "zmd武器欧非榜", aliases={"ef武器欧非榜", "终末地武器欧非榜", "武器欧非榜"}, rule=strict, priority=5, block=True
)


@dataclass
class Entry:
    qq: str
    name: str
    up_avg: float
    up_count: int
    special_pulls: int
    weapon_avg: float
    weapon_up: int


@dataclass(frozen=True)
class Board:
    pool: str  # 角色池 / 武器池
    command: str
    english: str
    basis: str
    target: str  # what one UP is, with its measure word
    measure: str
    empty: str
    avg: Callable[[Entry], float]
    count: Callable[[Entry], int]

    def ranked(self, entries: list[Entry]) -> list[Entry]:
        """Luckiest first; fewer UPs than MIN_UP is not ranked."""
        return sorted((e for e in entries if self.count(e) >= MIN_UP), key=lambda e: (self.avg(e), -self.count(e)))


CHARACTERS = Board(
    pool="角色池",
    command="/角色欧非榜",
    english="Operator",
    basis="限定池「平均多少抽出一个 UP 干员」",
    target="一个 UP 干员",
    measure="个",
    empty="本群还没有可统计的终末地限定池抽卡记录",
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
    empty="本群还没有可统计的终末地武器池抽卡记录",
    avg=lambda e: e.weapon_avg,
    count=lambda e: e.weapon_up,
)


def _load_optout() -> set[str]:
    # A missing file means nobody opted out; an unreadable one must not silently re-list people.
    if not OPTOUT_FILE.exists():
        return set()
    return set(json.loads(OPTOUT_FILE.read_text("utf-8")))


def _save_optout(qqs: set[str]) -> None:
    tmp = OPTOUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(qqs)), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(OPTOUT_FILE)


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


async def _collect(bot: Bot, group_id: int) -> list[Entry]:
    members = await bot.get_group_member_list(group_id=group_id)
    names = {str(m["user_id"]): (m.get("card") or m.get("nickname") or str(m["user_id"])) for m in members}
    candidates = set(names) - _load_optout()
    if not candidates:
        return []

    async with get_session() as session:
        binds = (
            await session.scalars(
                select(Bind).where(Bind.platform == PLATFORM, Bind.platform_id.in_(candidates))
            )
        ).all()
        qq_by_skuser = {b.bind_id: b.platform_id for b in binds}
        if not qq_by_skuser:
            return []
        # One entry per member: the records of their default Endfield role.
        rows = (
            await session.execute(
                select(CharacterDefault.owner_id, GachaRecord)
                .join(GachaRecord, GachaRecord.character_id == CharacterDefault.character_id)
                .where(CharacterDefault.app_code == "endfield", CharacterDefault.owner_id.in_(qq_by_skuser))
            )
        ).all()

    by_user: dict[int, list[GachaRecord]] = {}
    for owner_id, record in rows:
        by_user.setdefault(owner_id, []).append(record)

    entries = []
    for uid, user_records in by_user.items():
        stats = group_ef_gacha_records(user_records)
        # Same UP lookup as /zmd抽卡记录; pools missing from the local table count every 6★ as off-banner.
        for pool in stats.special_pools + stats.joint_pools + stats.weapon_pools:
            if local_pool := ef_gacha_pool_data.get_pool(pool.pool_id):
                pool.up_six_chars = local_pool.up_six_char_ids
        qq = qq_by_skuser[uid]
        entries.append(
            Entry(
                qq=qq,
                name=names[qq],
                up_avg=stats.special_up_avg,
                up_count=stats.special_up_count,
                special_pulls=sum(p.paid_pulls for p in stats.special_pools),
                weapon_avg=stats.weapon_up_avg,
                weapon_up=stats.weapon_up_count,
            )
        )
    return entries


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
td.val { width: 104px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 25px; line-height: 1; }
td.val small { font-family: var(--ef-cjk); font-size: 14px; color: var(--ef-sub); font-weight: 400; margin-left: 3px; }
tr:nth-child(1) td.val b { font-weight: 700; background: var(--ef-yellow); padding: 0 4px; }
td.val b { font-weight: 700; }
td.cnt { width: 96px; text-align: right; font-size: 14px; color: var(--ef-sub); }
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
.grid u { margin-left: auto; text-decoration: none; font-size: 13px; color: var(--ef-sub); flex: none; }
.grid b { width: 52px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 20px; flex: none; }
.grid span.top i { color: var(--ef-ink); font-weight: 700; }
"""


def _rows(board: Board, entries: list[Entry], requester: str) -> str:
    return "".join(
        f'<tr class="{"me" if e.qq == requester else ""}"><td class="rk"><span>{i}</span></td><td class="nm">{escape(e.name)}</td>'
        f'<td class="val"><b>{board.avg(e):.1f}</b><small>抽</small></td>'
        f'<td class="cnt"><b>{board.count(e)}</b> {board.measure} UP</td></tr>'
        for i, e in enumerate(entries, 1)
    )


def _tables(board: Board, ranked: list[Entry], requester: str) -> str:
    """The ten luckiest and the ten unluckiest of one board, side by side."""

    def table(title: str, kind: str, entries: list[Entry]) -> str:
        return (
            f'<div class="ef-sec {kind}"><h3>{board.pool} · {title}<small>{kind.upper()} · TOP {TOP}</small></h3>'
            f"<table>{_rows(board, entries, requester)}</table></div>"
        )

    return f'<div class="pair">{table("欧皇榜", "lucky", ranked[:TOP])}{table("非酋榜", "unlucky", ranked[::-1][:TOP])}</div>'


def _mine(board: Board, ranked: list[Entry], entries: list[Entry], requester: str) -> str:
    """One line with the requester's place on a board; empty when they have no saved records in this group."""
    if not any(e.qq == requester for e in entries):
        return ""
    label = f"你的{board.pool}排名："
    pos = next((i for i, e in enumerate(ranked, 1) if e.qq == requester), None)
    if pos is None:
        return f"<div>{label}<em>UP 不足 {MIN_UP} {board.measure}，暂未上榜</em></div>"
    me = ranked[pos - 1]
    return (
        f"<div>{label}第 <b>{pos}</b> / <i>{len(ranked)}</i> 名，"
        f"平均 <b>{board.avg(me):.1f}</b> 抽出{board.target}（共 <i>{board.count(me)}</i> {board.measure}）</div>"
    )


def _average(board: Board, ranked: list[Entry]) -> float:
    return sum(board.avg(e) for e in ranked) / len(ranked)


def _page(code: str, title: str, subtitle: str, body: str, note: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS % (WIDTH, FULL_COLUMNS)}</style></head><body class="ef">'
        + ef_theme.head(code, f"终末地 · <em>{title}</em>", subtitle, f"DATE<b>{datetime.now(ZoneInfo('Asia/Shanghai')):%y-%m-%d}</b>")
        + body
        + ef_theme.foot(
            f"数据来自已保存的抽卡记录（每天 01:00 自动更新，不含免费十连和当前垫抽），至少出过 {MIN_UP} 个 UP 才参与排名<br>"
            f"{note}不想上榜可发送 /zmd欧非榜 退出，重新加入发送 /zmd欧非榜 加入"
        )
        + "</body></html>"
    )


def _render_overview(entries: list[Entry], requester: str) -> str | None:
    """Both boards, top and bottom ten each; None when nobody in the group can be ranked on the character board."""
    boards = [(b, ranked) for b in (CHARACTERS, WEAPONS) if (ranked := b.ranked(entries))]
    if not boards or boards[0][0] is not CHARACTERS:
        return None
    tiles = "".join(
        f'<div>{b.pool}上榜<b>{len(ranked)}</b></div><div class="hl">{b.pool}群平均 UP 抽数<br><b>{_average(b, ranked):.1f}</b></div>'
        for b, ranked in boards
    )
    if len(boards) == 1:
        tiles += f"<div>本群有抽卡记录<b>{len(entries)}</b></div>"
    mine = "".join(_mine(b, ranked, entries, requester) for b, ranked in boards)
    return _page(
        "Endfield · Headhunting Luck Board",
        "本群欧非榜",
        f"按「平均多少抽出一个 UP」排名，越少越欧；每个榜只列前 {TOP} 名和后 {TOP} 名",
        f'<div class="summary">{tiles}</div>'
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + "".join(_tables(b, ranked, requester) for b, ranked in boards),
        f"完整榜单发送 {CHARACTERS.command} 或 {WEAPONS.command}；",
    )


def _render_board(board: Board, entries: list[Entry], requester: str) -> str | None:
    """Everyone on one board; None when nobody in the group can be ranked on it."""
    ranked = board.ranked(entries)
    if not ranked:
        return None
    cells = "".join(
        f'<span class="{"me" if e.qq == requester else "top" if i <= 3 else ""}"><i>{i}</i><em>{escape(e.name)}</em>'
        f"<u>{board.count(e)} {board.measure}</u><b>{board.avg(e):.1f}</b></span>"
        for i, e in enumerate(ranked, 1)
    )
    mine = _mine(board, ranked, entries, requester)
    rows = -(-len(ranked) // FULL_COLUMNS)
    return _page(
        f"Endfield · {board.english} Luck Board",
        f"本群{board.pool}欧非榜",
        f"按{board.basis}排名，越少越欧",
        f"""<div class="summary">
    <div>参与排名<b>{len(ranked)}</b></div>
    <div>最欧<b>{board.avg(ranked[0]):.1f}</b></div>
    <div>最非<b>{board.avg(ranked[-1]):.1f}</b></div>
    <div class="hl">群平均 UP 抽数<br><b>{_average(board, ranked):.1f}</b></div>
  </div>"""
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + f'<div class="ef-sec rest"><h3>完整榜单<small class="cjk">名次 · 群名片 · UP 数 · 平均抽数</small></h3><div class="grid" style="grid-template-rows: repeat({rows}, auto)">{cells}</div></div>',
        "前后十名总览发送 /zmd欧非榜；",
    )


async def _send(matcher, bot: Bot, event: MessageEvent, render: Callable[[list[Entry], str], str | None], empty: str) -> None:
    if not isinstance(event, GroupMessageEvent):
        await matcher.finish("欧非榜只能在群里使用")

    await _react(REACTION_PROCESSING)
    try:
        entries = await _collect(bot, event.group_id)
        html = render(entries, str(event.user_id))
        if html is None:
            await _react(REACTION_DONE)
            await matcher.finish(empty)
        image = await ef_theme.render_page(html, WIDTH, height=600)
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Endfield gacha leaderboard failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        raise
    await _react(REACTION_DONE)


@gacha_rank.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    qq = str(event.user_id)

    if action in ("退出", "加入"):
        optout = _load_optout()
        if action == "退出":
            optout.add(qq)
        else:
            optout.discard(qq)
        _save_optout(optout)
        await _react(REACTION_DONE)
        await gacha_rank.finish("已退出欧非榜，不会再出现在任何群的榜单上" if action == "退出" else "已重新加入欧非榜")

    await _send(gacha_rank, bot, event, _render_overview, CHARACTERS.empty)


@char_rank.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    await _send(char_rank, bot, event, lambda entries, qq: _render_board(CHARACTERS, entries, qq), CHARACTERS.empty)


@weapon_rank.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    await _send(weapon_rank, bot, event, lambda entries, qq: _render_board(WEAPONS, entries, qq), WEAPONS.empty)
