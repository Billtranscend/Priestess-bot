"""两款游戏本周的任务清单，成员发指令时才绘制。

/本周任务   （所有人；也可用 /每周任务、/周常任务）明日方舟的剿灭作战、周常任务和保全派驻；
            终末地的每周事务和通行证的每周任务

这里没有任何东西会定时发送。
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from html import escape

from nonebot import logger, on_command, require
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_alconna")
require("nonebot_plugin_htmlrender")
require("plugins.endfield_wiki")
require("plugins.endfield_guide")


from plugins import endfield_guide as guide
from plugins import ef_theme
from plugins import endfield_wiki as wiki
from plugins.shared import REACTION_DONE, REACTION_FAIL, REACTION_PROCESSING, react as _react

__plugin_meta__ = PluginMetadata(
    name="Weekly tasks",
    description="两款游戏的本周任务，发指令时生成。",
    usage="/本周任务",
    type="application",
)

CN = guide.CN
WIDTH = 1000

tasks_command = on_command("本周任务", aliases={"每周任务", "周常任务"}, rule=strict, priority=5, block=True)


# ── helpers ─────────────────────────────────────────────────────────────


def _parse(value: str) -> datetime | None:
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        with contextlib.suppress(ValueError, TypeError):
            return datetime.strptime(value, fmt).replace(tzinfo=CN)
    return None


def _left(end: datetime, now: datetime) -> str:
    seconds = max(0, int((end - now).total_seconds()))
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    if days:
        return f"{days} 天 {hours} 小时"
    return f"{hours} 小时 {minutes} 分" if hours else f"{minutes} 分钟"


def _week_of(opened: datetime, moment: datetime) -> int:
    """每周事务的周序号，从 1 开始（每周从活动开放时算起，周一 04:00）。"""
    return int((moment - opened).total_seconds() // (7 * 86400)) + 1


def _next_monday_4am(now: datetime) -> datetime:
    reset = (now + timedelta(days=(7 - now.weekday()) % 7)).replace(hour=4, minute=0, second=0, microsecond=0)
    return reset if reset > now else reset + timedelta(days=7)


def _card(title: str, body: str, note: str = "") -> str:
    small = f'<small class="cjk">{note}</small>' if note else ""
    return f'<div class="ef-sec"><h3>{title}{small}</h3>{body}</div>'


# ── 各个部分 ────────────────────────────────────────────────────────────


def _ark_weekly() -> dict:
    from plugins.game_calendar import ARKNIGHTS_FILE, arknights

    return arknights.load(ARKNIGHTS_FILE).get("weekly") or {}


def _ark_rows(ark: dict, now: datetime) -> list[str]:
    reset = _next_monday_4am(now)
    when = f"周一 {reset:%m/%d} 04:00 重置，还剩 {_left(reset, now)}"
    stamp = now.timestamp()
    rotation = next((r for r in ark.get("annihilation", []) if r["open"] <= stamp <= r["close"] and r["stage"]), None)
    current = f"；本期轮换「{escape(rotation['stage'])}」，{datetime.fromtimestamp(rotation['close'], CN):%m/%d} 结束" if rotation else ""
    rows = [f'<tr><td class="tag ak"><span>明日方舟</span></td><td><b>剿灭作战</b><div class="sub">{when}{current}</div></td></tr>']
    if ark.get("missions"):
        items = "".join(f'<li>{escape(m["desc"])}<em>{m["points"]} 点</em></li>' for m in ark["missions"])
        goal = f"，满 {ark['goal']} 点领取全部奖励" if ark.get("goal") else ""
        rows.append(
            f'<tr><td class="tag ak"><span>明日方舟</span></td><td><b>周常任务</b>'
            f'<div class="sub">{when}；共 {ark["mission_count"]} 个、{ark["points"]} 点{goal}</div><ul class="tasks">{items}</ul></td></tr>'
        )
    for season in ark.get("sss", []):
        if season["open"] <= stamp <= season["close"]:
            end = datetime.fromtimestamp(season["close"] + 1, CN)
            rows.append(
                f'<tr><td class="tag ak"><span>明日方舟</span></td><td><b>保全派驻 · {escape(season["name"])}</b>'
                f'<div class="sub">{end:%m/%d %H:%M} 结束，还剩 {_left(end, now)}</div></td></tr>'
            )
    return rows


def _tasks_section(index: dict, now: datetime) -> str:
    rows = _ark_rows(_ark_weekly(), now)
    weekly = index.get("calendar", {}).get("weekly") or {}
    opened = _parse(weekly.get("open", ""))
    if opened and weekly.get("weeks") and opened <= now and _week_of(opened, now) > max(int(w) for w in weekly["weeks"]):
        # 数据只到上一周（AKEData 还没收录新的一周）：说明一下，不拿旧任务冒充本周的
        rows.append(
            f'<tr><td class="tag ef"><span>终末地</span></td><td><b>{escape(weekly.get("name") or "周常任务")}</b>'
            f'<div class="sub">本周的任务数据还没更新（资料库目前只到第 {max(int(w) for w in weekly["weeks"])} 周），过一阵再来看</div></td></tr>'
        )
    elif opened and weekly.get("weeks") and opened <= now:
        last = max(int(w) for w in weekly["weeks"])
        week = min(last, _week_of(opened, now))
        reset = opened + timedelta(days=7 * week)
        tasks = weekly["weeks"].get(str(week), [])
        version = next(
            (v for v in index.get("calendar", {}).get("versions", []) if (_parse(v["open"]) or now) <= now < (_parse(v["close"]) or now)),
            None,
        )
        if version:  # 从版本开始时数周数：包含版本开始的那个刷新周是第 1 周
            first = _week_of(opened, _parse(version["open"]))
            label = f'{escape(version["name"])} 版本第 {week - first + 1} 周（共 {_week_of(opened, _parse(version["close"]) - timedelta(seconds=1)) - first + 1} 周）'
        else:
            label = f"第 {week} 周"
        items = "".join(f'<li>{escape(t["desc"])}<em>{t["score"]} 分</em></li>' for t in tasks)
        goal = max(weekly.get("milestones") or [0])
        total = sum(t["score"] for t in tasks)
        rows.append(
            f'<tr><td class="tag ef"><span>终末地</span></td><td><b>{escape(weekly["name"])} · {label}</b>'
            f'<div class="sub">{reset:%m/%d %H:%M} 重置，还剩 {_left(reset, now)}；共 {total} 分'
            + (f"，满 {goal} 分领取全部里程碑奖励" if goal else "")
            + f'</div><ul class="tasks">{items}</ul></td></tr>'
        )
    for bp in index.get("calendar", {}).get("passes", []):
        start, end = _parse(bp["open"]), _parse(bp["close"])
        if not (start and end and start <= now < end):
            continue
        opened = [w for w in bp["weeks"] if (_parse(w["open"]) or end) <= now]
        upcoming = [w for w in bp["weeks"] if (_parse(w["open"]) or start) > now]
        if opened:
            week = opened[-1]
            tasks = "".join(f"<li>{escape(t)}</li>" for t in week["tasks"])
            nxt = f"第 {upcoming[0]['week']} 周任务 {_parse(upcoming[0]['open']):%m/%d %H:%M} 开放；" if upcoming else ""
            rows.append(
                f'<tr><td class="tag ef"><span>终末地</span></td><td><b>通行证 第 {week["week"]} 周周常</b>'
                f'<div class="sub">{nxt}本期通行证 {end:%m/%d %H:%M} 结束</div><ul class="tasks">{tasks}</ul></td></tr>'
            )
    return _card("每周任务提醒", f'<table class="list">{"".join(rows)}</table>')


# ── page ────────────────────────────────────────────────────────────────

CSS = """
body { width: %dpx; padding: 26px; }
.sub { font-size: 15px; color: var(--ef-sub); margin-top: 3px; } .sub em { color: var(--ef-ink); font-style: normal; font-weight: 700; background: var(--ef-yellow); padding: 0 4px; }
table.list { width: 100%%; border-collapse: collapse; }
table.list td { padding: 11px 14px; border-top: 1px solid var(--ef-line-2); font-size: 18px; vertical-align: top; }
table.list tr:first-child td { border-top: 0; }
td.tag { width: 96px; white-space: nowrap; padding-top: 13px; }
td.tag span { display: inline-block; font-size: 13px; padding: 1px 7px; border: 1px solid var(--ef-ink); }
td.tag.ef span { background: var(--ef-yellow); } td.tag.ak span { background: var(--ef-ink); color: #f4f4f0; }
ul.tasks { list-style: none; margin-top: 8px; display: grid; grid-template-columns: 1fr 1fr; gap: 4px 18px; }
ul.tasks li { font-size: 14px; color: #3c3c38; padding-left: 13px; position: relative; }
ul.tasks li em { white-space: nowrap; font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 14px; margin-left: 6px; padding: 0 4px; background: var(--ef-ink); color: var(--ef-yellow); }
ul.tasks li:before { content: ""; position: absolute; left: 0; top: 7px; width: 6px; height: 6px; background: var(--ef-ink); }
"""


async def build_tasks() -> bytes:
    now = datetime.now(CN)
    loaded = wiki._resolver()
    index = loaded[0] if loaded else {}
    reset = _next_monday_4am(now)
    html = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS % WIDTH}</style></head><body class="ef">'
        + ef_theme.head("Weekly Tasks", "本周 <em>任务</em>", f"{reset - timedelta(days=7):%m/%d} – {reset:%m/%d} 周一 04:00 重置")
        + _tasks_section(index, now)
        + ef_theme.foot(f'明日方舟数据来自游戏数据表，终末地数据来自 AKEData（版本 {escape(index.get("version", ""))}）<br>全部指令发 /skl帮助')
        + "</body></html>"
    )
    return await ef_theme.render_page(html, WIDTH, height=600, template_path=wiki.DATA_DIR.as_uri())




@tasks_command.handle()
async def _(bot: Bot, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    if action:
        await tasks_command.finish("本周任务已不再定时推送，想看的时候直接发 /本周任务")
    await _react(REACTION_PROCESSING)
    try:
        image = await build_tasks()
    except Exception as e:
        logger.warning(f"Weekly tasks page failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await tasks_command.finish("本周任务生成失败，请稍后再试")
    await tasks_command.send(MessageSegment.image(image))
    await _react(REACTION_DONE)
