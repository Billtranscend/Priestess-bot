"""Group-scoped Endfield gacha luck leaderboard (/zmd欧非榜).

Stats reuse Skland's own grouping and averaging (group_ef_gacha_records), so the
numbers match /zmd抽卡记录. Only members of the current group who have bound
Skland and have not opted out are ranked.
"""

from __future__ import annotations

import contextlib
import json
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
from nonebot_plugin_skland.model import GachaRecord
from nonebot_plugin_skland.utils import group_ef_gacha_records
from nonebot_plugin_user.models import Bind
from sqlalchemy import select

__plugin_meta__ = PluginMetadata(
    name="Endfield gacha leaderboard",
    description="Ranks group members by Endfield limited-pool pulls per UP operator.",
    usage="/zmd欧非榜 | /zmd欧非榜 退出 | /zmd欧非榜 加入",
    type="application",
)

PLATFORM = "QQClient"
CHAR_TOP = 10
WEAPON_TOP = 5
MIN_UP = 2  # fewer UPs than this is too small a sample to rank
WIDTH = 1000
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"
OPTOUT_FILE = store.get_plugin_data_file("optout.json")

gacha_rank = on_command(
    "zmd欧非榜", aliases={"ef欧非榜", "终末地欧非榜", "欧非榜"}, rule=strict, priority=5, block=True
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
        records = (
            await session.scalars(
                select(GachaRecord).where(
                    GachaRecord.app_code == "endfield", GachaRecord.uid.in_(qq_by_skuser)
                )
            )
        ).all()

    by_user: dict[int, list[GachaRecord]] = {}
    for record in records:
        by_user.setdefault(record.uid, []).append(record)

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
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0 18px; padding: 8px 16px 10px; }
.grid span { display: flex; align-items: center; gap: 10px; font-size: 17px; padding: 5px 6px; border-bottom: 1px solid var(--ef-line-2); overflow: hidden; }
.grid span.me { background: var(--ef-yellow); box-shadow: inset 0 0 0 1px var(--ef-ink); }
.grid i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 17px; color: var(--ef-sub); width: 30px; flex: none; text-align: right; }
.grid span.me i { color: var(--ef-ink); }
.grid em { font-style: normal; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.grid b { margin-left: auto; font-family: var(--ef-num); font-weight: 700; font-size: 20px; flex: none; }
"""


def _rows(entries: list[Entry], value, unit: str, count) -> str:
    return "".join(
        f'<tr><td class="rk"><span>{i}</span></td><td class="nm">{escape(e.name)}</td>'
        f'<td class="val"><b>{value(e):.1f}</b><small>{unit}</small></td><td class="cnt">{count(e)}</td></tr>'
        for i, e in enumerate(entries, 1)
    )


def _board(title: str, kind: str, rows: str, top: int) -> str:
    return f'<div class="ef-sec {kind}"><h3>{title}<small>{kind.upper()} · TOP {top}</small></h3><table>{rows}</table></div>'


def _render_html(entries: list[Entry], group_total: int, requester: str) -> str:
    ranked = sorted((e for e in entries if e.up_count >= MIN_UP), key=lambda e: e.up_avg)
    weapons = sorted((e for e in entries if e.weapon_up >= MIN_UP), key=lambda e: e.weapon_avg)
    avg = sum(e.up_avg for e in ranked) / len(ranked)
    char_cols = (lambda e: e.up_avg, "抽", lambda e: f"<b>{e.up_count}</b> 个 UP")
    weapon_cols = (lambda e: e.weapon_avg, "抽", lambda e: f"<b>{e.weapon_up}</b> 把 UP")

    mine = ""
    if requester in {e.qq for e in ranked}:
        pos = next(i for i, e in enumerate(ranked, 1) if e.qq == requester)
        me = ranked[pos - 1]
        mine = (
            f'<div class="mine">你的排名：第 <b>{pos}</b> / <i>{len(ranked)}</i> 名，'
            f"限定池平均 <b>{me.up_avg:.1f}</b> 抽出一个 UP（共 <i>{me.up_count}</i> 个）</div>"
        )

    middle = ranked[CHAR_TOP : max(CHAR_TOP, len(ranked) - CHAR_TOP)]
    middle_block = ""
    if middle:
        cells = "".join(
            f'<span class="{"me" if e.qq == requester else ""}"><i>{i}</i><em>{escape(e.name)}</em><b>{e.up_avg:.1f}</b></span>'
            for i, e in enumerate(middle, CHAR_TOP + 1)
        )
        middle_block = f'<div class="ef-sec rest"><h3>其余排名（第 {CHAR_TOP + 1}～{CHAR_TOP + len(middle)} 名）</h3><div class="grid">{cells}</div></div>'

    weapon_block = ""
    if weapons:
        weapon_block = (
            '<div class="pair">'
            + _board("武器池 · 欧皇", "lucky", _rows(weapons[:WEAPON_TOP], *weapon_cols), WEAPON_TOP)
            + _board("武器池 · 非酋", "unlucky", _rows(weapons[::-1][:WEAPON_TOP], *weapon_cols), WEAPON_TOP)
            + "</div>"
        )

    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS % WIDTH}</style></head><body class="ef">'
        + ef_theme.head(
            "Endfield · Headhunting Luck Board",
            "终末地 · <em>本群欧非榜</em>",
            "按限定池「平均多少抽出一个 UP 干员」排名，越少越欧",
            f"DATE<b>{datetime.now(ZoneInfo('Asia/Shanghai')):%y-%m-%d}</b>",
        )
        + f"""<div class="summary">
    <div>参与排名<b>{len(ranked)}</b></div>
    <div>本群有抽卡记录<b>{group_total}</b></div>
    <div class="hl">群平均 UP 抽数<br><b>{avg:.1f}</b></div>
  </div>{mine}
  <div class="pair">"""
        + _board("欧皇榜", "lucky", _rows(ranked[:CHAR_TOP], *char_cols), CHAR_TOP)
        + _board("非酋榜", "unlucky", _rows(ranked[::-1][:CHAR_TOP], *char_cols), CHAR_TOP)
        + f"</div>{middle_block}{weapon_block}"
        + ef_theme.foot(
            f"数据来自已保存的抽卡记录（每天 01:00 自动更新，不含免费十连和当前垫抽），至少出过 {MIN_UP} 个 UP 才参与排名<br>"
            "不想上榜可发送 /zmd欧非榜 退出，重新加入发送 /zmd欧非榜 加入"
        )
        + "</body></html>"
    )


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

    if not isinstance(event, GroupMessageEvent):
        await gacha_rank.finish("欧非榜只能在群里使用")

    await _react(REACTION_PROCESSING)
    try:
        entries = await _collect(bot, event.group_id)
        if not any(e.up_count >= MIN_UP for e in entries):
            await _react(REACTION_DONE)
            await gacha_rank.finish("本群还没有可统计的终末地限定池抽卡记录")
        html = _render_html(entries, len(entries), qq)
        image = await ef_theme.render_page(html, WIDTH, height=600)
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Endfield gacha leaderboard failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        raise
    await _react(REACTION_DONE)
