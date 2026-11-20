"""卫戍协议的图：战绩（/卫，数据是上游算好的 GarrisonView）和卫戍榜。

明日方舟终端风格（plugins/ark_theme），部件与 /mrfz开盒 相同。上游的图以图标为主，这里每个图标旁都写上名字和数字。
干员头像用 /mrfz开盒 缓存在本地的方头像；其他图标是上游给的地址，渲染时经 render_image_cache 走磁盘缓存。
"""

from __future__ import annotations

import re
import time
from html import escape

from plugins import ark_theme

from .stats import Standing

WIDTH = 1080
BOARD_WIDTH = 900
SCALE, QUALITY = 1.5, 80  # 全部图：1.25x q65 的小字发虚；2x 只有放大才看得出差别，图却大一倍
NAMELESS = "未命名"
SUMMARY_RECORDS = 4  # 第一张图上的最近对局数
PAGE_RECORDS = 6  # 全部记录每张图的对局数

CSS = """
body { width: %dpx; padding: 24px; }
img { display: block; object-fit: contain; }
.ph { background: var(--ak-panel-2); }
.num { font-family: var(--ak-num); }

.ak-head.gs { padding-right: 230px; }
.who { display: flex; gap: 18px; align-items: center; margin-top: 12px; }
.who .avatar { width: 78px; height: 78px; flex: none; border: 1px solid var(--ak-line-2); background: var(--ak-panel-2); position: relative; }
.who .avatar:after { content: ""; position: absolute; right: -1px; bottom: -1px; width: 12px; height: 12px; background: var(--ak-blue); clip-path: polygon(100%% 0, 100%% 100%%, 0 100%%); }
.who .avatar img, .who .avatar .ph { width: 76px; height: 76px; }
.who h1 { margin-top: 0; font-size: 34px; }
.who p { margin-top: 4px; font-size: 16px; }
.ak-head .code b { font-size: 46px; color: var(--ak-yellow); }
.ak-head .code b small { font-family: var(--ak-cjk); font-size: 17px; font-weight: 700; letter-spacing: 0; color: var(--ak-sub); margin-left: 4px; }

.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 16px; }
.tiles div { position: relative; background: var(--ak-panel); border: 1px solid var(--ak-line); padding: 9px 14px 10px; }
.tiles div:before { content: ""; position: absolute; left: -1px; top: -1px; width: 26px; height: 3px; background: var(--ak-blue); }
.tiles small { font-size: 14px; font-weight: 700; color: var(--ak-sub); }
.tiles b { display: block; margin-top: 2px; font-family: var(--ak-num); font-weight: 700; font-size: 30px; line-height: 1.1; color: #fff; white-space: nowrap; }
.tiles b span { font-size: 18px; color: var(--ak-faint); }
.tiles b em { font-style: normal; font-family: var(--ak-cjk); font-size: 16px; font-weight: 700; color: var(--ak-yellow); margin-left: 8px; }
.tiles b.text { font-family: var(--ak-cjk); font-size: 24px; }
.tiles i { font-style: normal; font-size: 13px; color: var(--ak-faint); }
.tiles .bar { height: 4px; margin-top: 6px; background: var(--ak-line); } .tiles .bar u { display: block; height: 4px; background: var(--ak-blue); }

.pair { display: grid; grid-template-columns: 400px 1fr; gap: 16px; }
.band { display: flex; gap: 12px; align-items: center; padding: 10px 16px; border-top: 1px solid var(--ak-line); }
.band:first-child { border-top: 0; }
.band .ic { width: 56px; height: 56px; flex: none; background: #111; border: 1px solid var(--ak-line-2); }
.band .ic img, .band .ic .ph { width: 54px; height: 54px; }
.band .nm { font-size: 20px; font-weight: 900; color: #fff; }
.band .nm .ak-tag { margin-left: 6px; vertical-align: 3px; }
.band .meta { margin-top: 2px; font-size: 14px; color: var(--ak-sub); }
.band .meta b { font-family: var(--ak-num); font-size: 20px; color: #fff; margin-right: 2px; }
.band .rate { margin-left: auto; text-align: right; width: 100px; }
.band .rate b { font-family: var(--ak-num); font-size: 26px; color: var(--ak-blue); }
.band .rate small { display: block; font-size: 12px; color: var(--ak-faint); }
.bonds { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; padding: 14px 16px 16px; }
.bond { background: var(--ak-panel-2); border: 1px solid var(--ak-line); padding: 10px 6px 9px; text-align: center; }
.bond .ic { width: 58px; height: 58px; margin: 0 auto; border-radius: 50%%; background: radial-gradient(#1d3b33, #101012 70%%); border: 1px solid #2f6b5c; }
.bond .ic img, .bond .ic .ph { width: 56px; height: 56px; border-radius: 50%%; }
.bond .nm { margin-top: 6px; font-size: 17px; font-weight: 900; color: #fff; }
.bond .ct { margin-top: 2px; font-size: 13px; color: var(--ak-sub); }
.bond .ct b { font-family: var(--ak-num); font-size: 19px; color: #fff; margin-right: 1px; }
.bond .best { margin-top: 3px; font-size: 12px; color: var(--ak-faint); }
.bond .best b { font-family: var(--ak-num); font-size: 15px; color: var(--ak-yellow); }

.titles { display: grid; grid-template-columns: 340px 1fr; gap: 10px; padding: 12px 16px 16px; }
.radar svg { display: block; }
.radar text { font-family: var(--ak-cjk); font-size: 13px; fill: var(--ak-sub); }
.radar text.v { font-family: var(--ak-num); font-size: 17px; font-weight: 700; fill: #fff; }
.tlist { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px 8px; align-content: start; }
.tlist h4 { grid-column: 1 / -1; font-size: 14px; color: var(--ak-sub); margin-top: 4px; }
.tlist span { display: flex; align-items: center; justify-content: space-between; padding: 4px 9px; font-size: 14px; color: #fff; background: var(--ak-panel-2); border: 1px solid var(--ak-line); }
.tlist span b { font-family: var(--ak-num); color: var(--ak-yellow); }
.tlist span.zero { color: var(--ak-faint); } .tlist span.zero b { color: var(--ak-faint); }

.rec { margin-top: 12px; background: var(--ak-panel); border: 1px solid var(--ak-line); }
.rec:first-child { margin-top: 0; }
.rh { display: flex; align-items: center; gap: 12px; padding: 9px 14px; border-bottom: 1px solid var(--ak-line); background: linear-gradient(90deg, #2c2c30, var(--ak-panel) 70%%); }
.res { font-size: 14px; font-weight: 700; line-height: 26px; padding: 0 10px; border: 1px solid var(--ak-line-2); color: var(--ak-sub); background: var(--ak-panel-2); }
.res.ok { border-color: var(--ak-blue); color: #fff; background: var(--ak-blue-2); }
.res.fail { border-color: var(--ak-red); color: #fff; background: #6b2a22; }
.rh .no { font-family: var(--ak-num); font-weight: 700; font-size: 20px; color: var(--ak-faint); }
.rh .md { font-size: 18px; font-weight: 900; color: #fff; }
.rh .md small { font-size: 14px; font-weight: 400; color: var(--ak-sub); margin-left: 8px; }
.rh .star { color: var(--ak-yellow); font-size: 14px; }
.rh .tr { margin-left: auto; text-align: right; font-size: 13px; color: var(--ak-faint); white-space: nowrap; }
.rh .tr b { font-family: var(--ak-num); font-size: 22px; margin-right: 4px; color: #fff; }
.rh .tr b.up { color: var(--ak-green); } .rh .tr b.down { color: var(--ak-red); }
.rb { display: grid; grid-template-columns: 250px 1fr; gap: 14px; padding: 12px 14px; }
.side .st { display: flex; gap: 10px; align-items: center; }
.side .st .ic { width: 48px; height: 48px; flex: none; background: #111; border: 1px solid var(--ak-line-2); }
.side .st .ic img, .side .st .ic .ph { width: 46px; height: 46px; }
.side .st b { font-size: 17px; color: #fff; } .side .st small { display: block; font-size: 12px; color: var(--ak-faint); }
.side .facts { margin-top: 8px; font-size: 13px; color: var(--ak-sub); line-height: 1.7; }
.side .facts b { font-family: var(--ak-num); font-size: 16px; color: #fff; margin: 0 2px; }
.boss { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
.boss img, .boss .ph { width: 40px; height: 40px; flex: none; border: 1px solid var(--ak-line-2); background: #111; }
.boss b { display: block; font-size: 14px; color: #fff; line-height: 1.3; }
.boss small { font-size: 12px; color: var(--ak-faint); } .boss small.killed { color: var(--ak-green); } .boss small.hidden { color: var(--ak-yellow); }
.ops { display: grid; grid-template-columns: repeat(8, 1fr); gap: 6px; align-content: start; }
.op { background: #121214; border: 1px solid var(--ak-line); position: relative; }
.op .pic { position: relative; height: 76px; overflow: hidden; }
.op .pic > img.av, .op .pic > .ph { width: 100%%; height: 76px; object-fit: cover; }
.op .pic:after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 30px; background: linear-gradient(transparent, rgba(0,0,0,.85)); }
.op .tier { position: absolute; left: 2px; top: 2px; width: 22px; height: 22px; z-index: 2; }
.op .lv { position: absolute; right: 4px; bottom: 1px; z-index: 2; font-family: var(--ak-num); font-weight: 700; font-size: 18px; color: #fff; }
.op .lv small { font-size: 10px; color: var(--ak-sub); margin-right: 1px; }
.op .nm { padding: 3px 4px; font-size: 12px; font-weight: 700; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.op .eq { display: flex; gap: 2px; padding: 0 3px 3px; min-height: 19px; }
.op .eq img { width: 16px; height: 16px; border: 1px solid var(--ak-line-2); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 14px 12px; }
.chip { display: flex; align-items: center; gap: 5px; padding: 2px 9px 2px 3px; font-size: 14px; color: #fff; background: #17322b; border: 1px solid #2f6b5c; border-radius: 15px; }
.chip img, .chip .ph { width: 24px; height: 24px; border-radius: 50%%; }
.chip b { font-family: var(--ak-num); color: var(--ak-yellow); }
.chip.off { background: var(--ak-panel-2); border-color: var(--ak-line); color: var(--ak-faint); }

.det { padding: 0 14px 14px; display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.dop { display: flex; gap: 10px; padding: 8px; background: var(--ak-panel-2); border: 1px solid var(--ak-line); }
.dop .pic { position: relative; width: 70px; height: 70px; flex: none; }
.dop .pic img.av, .dop .pic .ph { width: 70px; height: 70px; object-fit: cover; }
.dop .pic .tier { position: absolute; left: 1px; top: 1px; width: 20px; height: 20px; }
.dop .nm { font-size: 16px; font-weight: 900; color: #fff; }
.dop .nm small { font-size: 12px; font-weight: 400; color: var(--ak-sub); margin-left: 6px; }
.dop .ln { margin-top: 3px; font-size: 12px; color: var(--ak-sub); line-height: 1.5; }
.dop .ln b { color: var(--ak-yellow); font-weight: 700; margin-right: 4px; }
.ev { padding: 0 14px 12px; font-size: 13px; color: var(--ak-sub); }
.pageno { margin-left: auto; font-family: var(--ak-num); font-size: 14px; letter-spacing: 2px; color: var(--ak-faint); }
""" % WIDTH


def _img(src: str, cls: str = "") -> str:
    return f'<img class="{cls}" src="{escape(src)}">' if src else '<div class="ph"></div>'


def _words(text: str) -> str:
    """说明文字里游戏自己的标记（如 $ba.weaknessatk）去掉。"""
    return re.sub(r"\$[A-Za-z_]+(?:\.[A-Za-z_]+)+", "", text or "")


def _avatar(char_id: str, avatars: dict[str, str]) -> str:
    return avatars.get(char_id, "")


def _percent(value: float | None) -> str:
    return f"{value:.0f}" if value is not None else "-"


def _head(view, kicker: str) -> str:
    trophies = next((m.value for m in view.metrics if m.label == "奖杯数量"), "0")
    level = f" · Lv {view.level}" if view.level else ""
    return (
        f'<div class="ak-head gs"><span class="ak-corner tl"></span><span class="ak-corner br"></span>'
        f'<div class="code">TROPHY<b>{escape(trophies)}<small>奖杯</small></b></div>'
        f'<div class="k">Stronghold Protocol · {escape(kicker)}</div>'
        f'<div class="who"><div class="avatar">{_img(view.avatar_url)}</div>'
        f'<div><h1>{escape(view.nickname or NAMELESS)}</h1><p>卫戍协议·{escape(view.season_name)} · {escape(view.server_name)}{level}</p></div></div></div>'
    )


def _tiles(view) -> str:
    metric = {m.label: m for m in view.metrics}
    modes = {m.label: m for m in view.modes}
    total = sum(m.total for m in view.modes)
    wins = sum(m.wins for m in view.modes)
    boss = metric.get("Boss 击杀")
    tiles = [
        ("总场次", f"{total}", " · ".join(f"{m.label} {m.total}" for m in view.modes) or "暂无"),
        ("完成率", f"{round(100 * wins / total) if total else 0}<span>%</span>", f"完成 {wins} 局"),
        ("Boss 击杀", f"{escape(boss.value) if boss else 0}" + (f"<em>隐秘 {escape(boss.secondary_value)}</em>" if boss and boss.secondary_value else ""), "击败的 Boss 数"),
        ("游戏时长", escape(metric["游戏时长"].value) if "游戏时长" in metric else "-", escape(metric["游戏时长"].detail) if "游戏时长" in metric else ""),
    ]
    for label in ("独立", "联合"):
        mode = modes.get(label)
        bar = f'<div class="bar"><u style="width:{mode.win_percent or 0:.0f}%"></u></div>' if mode else ""
        tiles.append((f"{label}模拟", f"{mode.wins if mode else 0}<span> 胜 {mode.losses if mode else 0} 负</span>", f"胜率 {_percent(mode.win_percent if mode else None)}%{bar}"))
    best = metric.get("最佳记录")
    tiles.append(("最佳记录", escape(best.value) if best else "-", "最高的模式与等级", True))
    tiles.append(("已获赞", escape(metric["已获赞"].value) if "已获赞" in metric else "0", "对局被点赞"))
    out = []
    for label, value, note, *words in tiles:
        out.append(f'<div><small>{label}</small><b{' class="text"' if words else ""}>{value}</b><i>{note}</i></div>')
    return f'<div class="tiles">{"".join(out)}</div>'


def _preferences(view) -> str:
    bands = "".join(
        f'<div class="band"><div class="ic">{_img(s.icon_url)}</div><div><div class="nm">{escape(s.name)}'
        + (f'<span class="ak-tag b">{escape(s.badge)}</span>' if s.badge else "")
        + f'</div><div class="meta"><b>{s.recent_count}</b>场 · 完成 {s.recent_wins}</div></div>'
        f'<div class="rate"><b>{_percent(s.win_percent)}%</b><small>完成率</small></div></div>'
        for s in view.strategies[:3]
    ) or '<div class="ak-empty">暂无常用策略</div>'
    shown = [b for b in view.bonds if b.max_layer] or view.bonds  # 从没叠过层的（如「绝技」）不列
    bonds = "".join(
        f'<div class="bond"><div class="ic">{_img(b.icon_url)}</div><div class="nm">{escape(b.name)}</div>'
        f'<div class="ct"><b>{b.recent_count}</b>场</div><div class="best">最高 <b>{b.max_layer or 0}</b> 层</div></div>'
        for b in shown[:5]
    ) or '<div class="ak-empty">暂无常用盟约</div>'
    return (
        f'<div class="pair"><div class="ak-sec"><h3>常用策略<small class="cjk">{escape(view.recent_scope)}</small></h3>{bands}</div>'
        f'<div class="ak-sec"><h3>常用盟约<small>Covenant</small></h3><div class="bonds">{bonds}</div></div></div>'
    )


def _radar(view) -> str:
    dims = view.title_dimensions
    if len(dims) < 3:
        return '<div class="ak-empty">暂无六维评价</div>'
    import math

    cx, cy, r = 170, 150, 92
    scale = max(view.title_scale, 1)

    def point(i: int, k: float) -> tuple[float, float]:
        angle = -math.pi / 2 + 2 * math.pi * i / len(dims)
        return cx + r * k * math.cos(angle), cy + r * k * math.sin(angle)

    rings = "".join(
        f'<polygon points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in (point(i, k / 3) for i in range(len(dims))))}" fill="none" stroke="#3a3a3f" stroke-width="1"/>'
        for k in (1, 2, 3)
    )
    spokes = "".join(f'<line x1="{cx}" y1="{cy}" x2="{point(i, 1)[0]:.1f}" y2="{point(i, 1)[1]:.1f}" stroke="#3a3a3f"/>' for i in range(len(dims)))
    shape = " ".join(f"{x:.1f},{y:.1f}" for x, y in (point(i, min((d.count or 0) / scale, 1)) for i, d in enumerate(dims)))
    labels = []
    for i, d in enumerate(dims):
        x, y = point(i, 1.32)
        labels.append(f'<text x="{x:.1f}" y="{y - 2:.1f}" text-anchor="middle" class="v">{d.count if d.count is not None else "-"}</text>'
                      f'<text x="{x:.1f}" y="{y + 15:.1f}" text-anchor="middle">{escape(d.label)}</text>')
    return (
        f'<div class="radar"><svg width="340" height="300" viewBox="0 0 340 300">{rings}{spokes}'
        f'<polygon points="{shape}" fill="rgba(28,167,230,.35)" stroke="#1ca7e6" stroke-width="2"/>{"".join(labels)}</svg></div>'
    )


def _titles(view) -> str:
    def group(name: str, items) -> str:
        if not items:
            return ""
        spans = "".join(f'<span class="{"zero" if not t.count else ""}">{escape(t.name)}<b>×{t.count or 0}</b></span>' for t in items)
        return f"<h4>{name}</h4>{spans}"

    body = group("普通称号", view.normal_titles) + group("特殊称号", view.special_titles) + group("其他称号", view.other_titles)
    return (
        f'<div class="ak-sec"><h3>称号与六维评价<small>Titles</small></h3><div class="titles">{_radar(view)}'
        f'<div class="tlist">{body or "<h4>暂无称号</h4>"}</div></div></div>'
    )


def _record(r, avatars: dict[str, str], *, detail: bool = False) -> str:
    when = time.strftime("%m-%d %H:%M", time.localtime(r.ended_at)) if r.ended_at else ""
    trophy = f'<b class="{"up" if r.trophy_change > 0 else "down" if r.trophy_change < 0 else ""}">{r.trophy_change:+d}</b>'
    head = (
        f'<div class="rh"><span class="res {"ok" if r.success else "fail"}">{escape(r.result_label)}</span>'
        f'<span class="no">#{r.index:02d}</span>'
        f'<span class="md">{escape(r.difficulty_label)}<small>{escape(r.mode_label)} · {r.rounds} 回合 · 用时 {escape(r.duration_label)}</small></span>'
        + ('<span class="star">★ 已收藏</span>' if r.is_favored else "")
        + f'<span class="tr">{trophy}奖杯（{r.trophy_after}）<br>{when}</span></div>'
    )
    bosses = "".join(
        f'<div class="boss">{_img(b.icon_url)}<div><b>{escape(b.name)}</b>'
        f'<small class="{"hidden" if b.is_hidden else ""}">{"隐秘" if b.is_hidden else "领袖"}</small> '
        f'<small class="{"killed" if b.is_killed else ""}">{"已击败" if b.is_killed else "未击败"}</small></div></div>'
        for b in r.bosses
    )
    side = (
        f'<div class="side"><div class="st"><div class="ic">{_img(r.strategy.icon_url)}</div><div><b>{escape(r.strategy.name)}</b><small>策略</small></div></div>'
        f'<div class="facts">击倒<b>{r.kill_count}</b>· 花费金币<b>{r.coin_cost}</b></div>{bosses}</div>'
    )
    if detail:
        ops = "".join(
            f'<div class="dop"><div class="pic">{_img(_avatar(o.char_id, avatars), "av")}{_img(o.tier_icon_url, "tier") if o.tier_icon_url else ""}</div>'
            f'<div><div class="nm">{escape(o.name)}<small>{escape(o.rank_label)} · 精英 {o.elite_phase} · Lv {o.level}</small></div>'
            + "".join(f'<div class="ln"><b>{escape(e.name)}</b>{escape(_words(e.description))}</div>' for e in o.equipment)
            + "".join(f'<div class="ln"><b>能力</b>{escape(_words(a.description))}</div>' for a in o.abilities)
            + "</div></div>"
            for o in r.operators
        )
        body = f'<div class="rb" style="grid-template-columns: 1fr">{side}</div><div class="det">{ops}</div>'
    else:
        ops = "".join(
            f'<div class="op"><div class="pic">{_img(_avatar(o.char_id, avatars), "av")}{_img(o.tier_icon_url, "tier") if o.tier_icon_url else ""}'
            f'<div class="lv"><small>LV</small>{o.level}</div></div><div class="nm">{escape(o.name)}</div>'
            f'<div class="eq">{"".join(_img(e.icon_url) for e in o.equipment[:3])}</div></div>'
            for o in r.operators
        )
        body = f'<div class="rb">{side}<div class="ops">{ops}</div></div>'
    chips = "".join(
        f'<span class="chip{"" if b.count else " off"}">{_img(b.icon_url)}{escape(b.name)}<b>{b.count or 0}</b></span>' for b in r.bonds
    )
    events = f'<div class="ev">事件：{escape("、".join(r.events))}</div>' if detail and r.events else ""
    return f'<div class="rec">{head}{body}<div class="chips">{chips}</div>{events}</div>'


def _page(body: str, now: float | None) -> str:
    now = now or time.time()
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ark_theme.css()}{CSS}</style></head><body class="ak">{body}'
        + ark_theme.foot(f'数据来源 森空岛 · 官方数据每天 10:00 刷新<br>生成时间 {time.strftime("%Y-%m-%d %H:%M", time.localtime(now))}')
        + "</body></html>"
    )


def summary(view, avatars: dict[str, str], *, page_total: int, now: float | None = None) -> str:
    """第一张图：统计、常用策略与盟约、称号与六维评价、最近几局。"""
    records = "".join(_record(r, avatars) for r in view.records[:SUMMARY_RECORDS]) or '<div class="ak-empty">暂无战绩记录</div>'
    more = f'<small class="cjk">最近 {min(len(view.records), SUMMARY_RECORDS)} 局 · 共 {len(view.records)} 局</small>'
    return _page(
        _head(view, view.season_name) + _tiles(view) + _preferences(view) + _titles(view)
        + f'<div class="ak-sec"><h3>{"收藏的对局" if view.favored else "战绩记录"}{more}</h3><div style="padding:12px 14px 14px">{records}</div></div>',
        now,
    )


def records_page(view, records, avatars: dict[str, str], *, page: int, page_total: int, now: float | None = None) -> str:
    """全部记录的一张图。"""
    body = "".join(_record(r, avatars) for r in records)
    title = "收藏的对局" if view.favored else "战绩记录"
    return _page(
        _head(view, view.season_name)
        + f'<div class="ak-sec"><h3>{title}<span class="pageno">{page}/{page_total}</span></h3><div style="padding:12px 14px 14px">{body}</div></div>',
        now,
    )


def detail(view, avatars: dict[str, str], *, now: float | None = None) -> str:
    """单局详情：每位干员的装备和能力写成文字。"""
    record = view.records[0] if view.records else None
    body = _record(record, avatars, detail=True) if record else '<div class="ak-empty">找不到这一局</div>'
    return _page(_head(view, "Record Detail") + f'<div class="ak-sec"><h3>单局详情</h3><div style="padding:12px 14px 14px">{body}</div></div>', now)


# ---------------------------------------------------------------- 卫戍榜

BOARD_CSS = """
body { width: %dpx; padding: 24px; }
table { width: 100%%; border-collapse: collapse; table-layout: fixed; }
td { padding: 8px 12px; border-top: 1px solid var(--ak-line); font-size: 18px; white-space: nowrap; }
tr:first-child td { border-top: 0; }
td.rk { width: 58px; padding-right: 0; }
td.rk span { display: inline-block; width: 34px; height: 30px; line-height: 29px; text-align: center; font-family: var(--ak-num); font-weight: 700;
  font-size: 19px; color: var(--ak-sub); background: var(--ak-panel-2); border: 1px solid var(--ak-line-2); }
tr.r1 td.rk span { background: var(--ak-yellow); color: #111; border-color: var(--ak-yellow); }
tr.r2 td.rk span { background: #c9ced6; color: #111; border-color: #c9ced6; }
tr.r3 td.rk span { background: #c8794a; color: #fff; border-color: #c8794a; }
td.nm { overflow: hidden; text-overflow: ellipsis; color: #fff; font-weight: 700; }
td.nm s { text-decoration: none; margin-left: 8px; padding: 0 6px; font-size: 12px; font-weight: 400; line-height: 1.5; vertical-align: 2px;
  color: #fff; background: var(--ak-blue-2); border: 1px solid var(--ak-blue); }
td.bd { width: 150px; font-size: 15px; color: var(--ak-sub); overflow: hidden; text-overflow: ellipsis; }
td.gm { width: 150px; text-align: right; font-size: 13px; color: var(--ak-faint); }
td.gm b { font-family: var(--ak-num); font-size: 18px; color: #fff; margin: 0 2px; }
td.tp { width: 110px; text-align: right; font-family: var(--ak-num); font-weight: 700; font-size: 26px; color: var(--ak-yellow); }
td.tp small { font-family: var(--ak-cjk); font-size: 13px; font-weight: 400; color: var(--ak-faint); margin-left: 3px; }
tr.me td { background: rgba(28, 167, 230, .16); }
tr.me td:first-child { box-shadow: inset 4px 0 0 var(--ak-blue); }
.gap td { padding: 2px 12px; text-align: center; color: var(--ak-faint); font-size: 14px; letter-spacing: 6px; }
.summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 16px; }
.summary div { position: relative; background: var(--ak-panel); border: 1px solid var(--ak-line); padding: 9px 14px 10px; font-size: 14px; font-weight: 700; color: var(--ak-sub); }
.summary div:before { content: ""; position: absolute; left: -1px; top: -1px; width: 26px; height: 3px; background: var(--ak-blue); }
.summary b { display: block; margin-top: 2px; font-family: var(--ak-num); font-size: 30px; line-height: 1.1; color: #fff; }
""" % BOARD_WIDTH

TOP = 20


def board(rows: list[Standing], names: dict[int, tuple[str, bool]], me: int | None, counted_at: float, now: float | None = None, season: str = "卫戍协议") -> str:
    """卫戍榜的 HTML。rows 已排好序；names：成员 -> (显示的名字, 是否本群)；me：查榜的人。"""
    now = now or time.time()
    mine = next((i for i, r in enumerate(rows) if r.owner == me), None)
    shown = list(enumerate(rows[:TOP]))
    tail = [(mine, rows[mine])] if mine is not None and mine >= TOP else []

    def row(i: int, r: Standing) -> str:
        name, here = names.get(r.owner, (r.name or NAMELESS, False))
        cls = " ".join(c for c in (f"r{i + 1}" if i < 3 else "", "me" if r.owner == me else "") if c)
        return (
            f'<tr class="{cls}"><td class="rk"><span>{i + 1}</span></td>'
            f'<td class="nm">{escape(name or NAMELESS)}{"<s>本群</s>" if here else ""}</td>'
            f'<td class="bd">{escape(r.band)}</td>'
            f'<td class="gm"><b>{r.total}</b>场 完成率<b>{r.rate}</b>%</td>'
            f'<td class="tp">{r.trophies}<small>奖杯</small></td></tr>'
        )

    body = "".join(row(i, r) for i, r in shown)
    if tail:
        body += '<tr class="gap"><td colspan="5">· · ·</td></tr>' + "".join(row(i, r) for i, r in tail)
    total_games = sum(r.total for r in rows)
    top = rows[0].trophies if rows else 0
    counted = time.strftime("%m 月 %d 日 %H:%M", time.localtime(counted_at)) if counted_at else "—"
    table = f"<table>{body}</table>" if rows else f'<div class="ak-empty">还没有人有{escape(season)}的记录</div>'
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ark_theme.css()}{BOARD_CSS}</style></head><body class="ak">'
        f"""<div class="ak-head"><span class="ak-corner tl"></span><span class="ak-corner br"></span>
  <div class="code">SEASON<b>{escape(season.partition("·")[2] or "—")}</b></div>
  <div class="k">Stronghold Protocol · Ranking</div>
  <h1>卫戍<em>榜</em></h1>
  <p>按奖杯数排名 · 统计于 {counted} · 每天 10:30 刷新</p>
</div>
<div class="summary"><div>上榜博士<b>{len(rows)}</b></div><div>最高奖杯<b>{top}</b></div><div>累计对局<b>{total_games}</b></div></div>
<div class="ak-sec"><h3>排名<small class="cjk">奖杯相同时比完成局数</small></h3>{table}</div>"""
        + ark_theme.foot(
            "榜单不分群：本群成员显示群名片并标有「本群」，其他人显示游戏内的名字<br>"
            "数据来源 森空岛 · 不想上榜可发送 /卫戍榜 退出"
            + f'<br>生成时间 {time.strftime("%Y-%m-%d %H:%M", time.localtime(now))}'
        )
        + "</body></html>"
    )
