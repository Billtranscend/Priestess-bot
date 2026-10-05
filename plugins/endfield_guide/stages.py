"""Stage menus for 影拓丰碑 and 战争回响: game order, cover pictures, enemy-only cards.

The AKEData tables list the stages of a 影拓丰碑 series in a different order than the game
does and have no series cover; both come from the layout that echoes.py saves from Skland.
Covers are kept as small WebP thumbnails, because the stage art is a 20 MB PNG each.
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from PIL import Image

CN = timezone(timedelta(hours=8))
USER_AGENT = "QQBot-EndfieldWiki/1.0 (non-commercial group bot; data from AKEData)"
COVER_TIMEOUT = 180  # a stage cover downloads at about 1 MB/s
COVER_WAIT = 8  # seconds a command waits for covers that are not cached yet
COVER_QUALITY = 82
POSTER_WIDTH, STAGE_WIDTH, TOWER_WIDTH = 520, 760, 480


# ── order ──────────────────────────────────────────────────────────────


def ordered(endgame: dict, layout: dict) -> dict:
    """The stage index with 影拓丰碑 in the game's order: series as listed there, stages numbered from 1.

    Anything the layout does not know yet (a series newer than the last collection) comes first,
    as the game puts the newest series on top.
    """
    known = {entry.get("name"): (rank, entry) for rank, entry in enumerate(layout.get("monument") or [])}
    newest_first = list(reversed(endgame.get("monument") or []))
    monument = []
    for series in sorted(newest_first, key=lambda s: known.get(s["name"], (-1, {}))[0]):
        entry = known.get(series["name"], (0, {}))[1]
        position = {name: i for i, name in enumerate(entry.get("stages") or [])}
        groups = sorted(series["groups"], key=lambda g: position.get(g["name"], len(position)))
        monument.append(
            {
                **series,
                "groups": [{**group, "no": number} for number, group in enumerate(groups, 1)],
                "pic": entry.get("pic", ""),
                "activity": entry.get("activity", ""),
                "open": entry.get("open", 0),
                "close": entry.get("close", 0),
            }
        )
    return {**endgame, "monument": monument}


def live(series: dict, now: float) -> bool:
    """The series' opening event is running (afterwards the series stays playable)."""
    return bool(series.get("close")) and series.get("open", 0) <= now < series["close"]


# ── cover thumbnails ───────────────────────────────────────────────────


class Covers:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.tasks: dict[str, asyncio.Task] = {}

    def path(self, name: str) -> Path:
        return self.folder / f"cover_{name}.webp"

    async def _fetch(self, url: str, name: str, width: int) -> None:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=COVER_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path(name).with_suffix(".part")
        await asyncio.to_thread(_thumbnail, response.content, width, tmp)
        tmp.replace(self.path(name))

    def start(self, url: str, name: str, width: int) -> asyncio.Task | None:
        """Begin downloading a missing cover; one download per cover at a time."""
        if not url or not name or self.path(name).exists():
            return None
        if name not in self.tasks:
            task = asyncio.get_running_loop().create_task(self._fetch(url, name, width))
            self.tasks[name] = task
            task.add_done_callback(lambda done: (self.tasks.pop(name, None), done.cancelled() or done.exception()))
        return self.tasks[name]

    async def get(self, wanted: list[tuple[str, str, int]], wait: float = COVER_WAIT) -> dict[str, str]:
        """File URIs by name of the covers that are there after waiting `wait` seconds for the missing ones.

        Covers still downloading are left out of this picture and finish in the background.
        """
        pending = [task for url, name, width in wanted if (task := self.start(url, name, width))]
        if pending and wait:
            await asyncio.wait(pending, timeout=wait)
        return {name: self.path(name).as_uri() for _, name, _ in wanted if name and self.path(name).exists()}


def _thumbnail(data: bytes, width: int, out: Path) -> None:
    Image.MAX_IMAGE_PIXELS = None  # the stage art is 2640 x 1920
    image = Image.open(io.BytesIO(data))
    image = image.convert("RGBA" if "A" in image.getbands() or image.mode == "P" else "RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    image.save(out, "WEBP", quality=COVER_QUALITY, method=4)


def poster(series: dict) -> tuple[str, str, int]:
    return series.get("pic", ""), f"series_{series['id']}" if series.get("pic") else "", POSTER_WIDTH


def stage_cover(group: dict, url: str) -> tuple[str, str, int]:
    return url, group.get("cover", ""), TOWER_WIDTH if group.get("mode") == "战争回响" else STAGE_WIDTH


# ── pages ──────────────────────────────────────────────────────────────

ELEMENTS = ("物理", "灼热", "电磁", "寒冷", "自然")
# Shared with the stage card of /攻略: the line of bonuses under an enemy row.
NOTES_CSS = """
tr.bn td { border-top: 0; padding: 0 8px 9px; font-size: 13px; line-height: 1.7; color: var(--ef-sub); white-space: normal; }
.bs { display: inline-block; margin-right: 18px; }
.bt { display: inline-block; margin-right: 7px; padding: 0 6px; font-size: 12px; line-height: 18px; background: var(--ef-ink); color: #f4f4f0; }
.bt.own { background: var(--ef-yellow); color: var(--ef-ink); box-shadow: 0 0 0 1px var(--ef-ink); }
"""

CSS = """
body { width: %dpx; padding: 24px; }
.ef-head h1 { font-size: 46px; }
.ef-head p { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; color: #b9b9b2; }
.ef-head p span { font-size: 15px; padding: 3px 12px; border: 1px solid #55554f; color: #b9b9b2; }
.ef-head p span i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 17px; letter-spacing: .3px; }
.ef-head p span.live { border-color: var(--ef-yellow); color: var(--ef-yellow); }
.dim { color: var(--ef-faint); }
.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-top: 16px; }
.card { background: var(--ef-panel); border: 1px solid var(--ef-line); border-bottom: 3px solid var(--ef-ink); }
.card .art { position: relative; aspect-ratio: 4 / 5; background: var(--ef-dark); }
.card .art img { display: block; width: 100%%; height: 100%%; object-fit: cover; }
.card .art .tag { position: absolute; left: 0; top: 10px; padding: 2px 10px; font-size: 13px; font-weight: 700; background: var(--ef-yellow); color: var(--ef-ink); }
.card h3 { padding: 10px 12px 8px; font-size: 21px; font-weight: 900; border-bottom: 2px solid var(--ef-ink); }
.card ol { list-style: none; padding: 8px 12px 10px; }
.card li { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 16px; }
.no { flex: none; display: inline-block; min-width: 24px; height: 24px; line-height: 24px; text-align: center; font-family: var(--ef-num);
  font-weight: 700; font-size: 16px; background: var(--ef-ink); color: #f4f4f0; }
.split { display: flex; gap: 16px; margin-top: 16px; align-items: flex-start; }
.split .poster { flex: none; width: 250px; background: var(--ef-dark); border: 1px solid var(--ef-line); border-bottom: 3px solid var(--ef-ink); }
.split .poster img { display: block; width: 100%%; }
.split .poster p { padding: 8px 12px; font-size: 13px; line-height: 1.6; color: #b9b9b2; }
.split .poster p b { color: var(--ef-yellow); font-weight: 700; }
.rows { flex: 1; min-width: 0; }
.st { display: flex; align-items: stretch; background: var(--ef-panel); border: 1px solid var(--ef-line); margin-bottom: 12px; }
.st:last-child { margin-bottom: 0; }
.st.me { box-shadow: inset 6px 0 0 var(--ef-yellow); background: #fbf7d2; }
.st .ord { flex: none; width: 74px; display: flex; flex-direction: column; align-items: center; justify-content: center; background: var(--ef-dark); color: #f4f4f0; }
.st .ord b { font-family: var(--ef-num); font-weight: 700; font-size: 40px; line-height: 1; color: var(--ef-yellow); }
.st .ord small { margin-top: 4px; font-size: 13px; letter-spacing: 2px; color: #b9b9b2; }
.st .pic { position: relative; flex: none; width: 250px; min-height: 140px; background: var(--ef-dark-2); }
.st .pic img { position: absolute; left: 0; top: 0; width: 100%%; height: 100%%; object-fit: cover; }
.st .pic.tower { background: var(--ef-dark); min-height: 160px; }
.st .pic.tower img { object-fit: contain; padding: 6px; }
.st .inf { flex: 1; min-width: 0; padding: 12px 16px; }
.st h3 { font-size: 26px; font-weight: 900; line-height: 1.2; }
.st .lv { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.st .lv span { font-size: 13px; padding: 1px 8px; border: 1px solid var(--ef-ink); background: var(--ef-panel); }
.st .lv span i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 15px; }
.st .foes { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 10px; font-size: 15px; color: var(--ef-sub); }
.st .foes span { display: inline-flex; align-items: center; gap: 6px; }
.st .foes img { width: 30px; height: 30px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
.next { margin-top: 12px; padding: 10px 16px; background: var(--ef-panel); border: 1px solid var(--ef-line); font-size: 16px; }
.next b { margin-right: 10px; } .next i { font-style: normal; font-family: var(--ef-num); font-weight: 600; color: var(--ef-sub); margin-left: 10px; }
.ef-sec h3 .no { margin-right: 2px; }
table { width: 100%%; border-collapse: collapse; font-size: 16px; table-layout: fixed; }
col.c-df { width: 94px; } col.c-ei { width: 64px; } col.c-fe { width: 220px; } col.c-lv { width: 84px; } col.c-num { width: 124px; } col.c-po { width: 96px; }
th { text-align: left; padding: 6px 8px; font-size: 13px; font-weight: 400; color: var(--ef-sub); border-bottom: 1px solid var(--ef-ink); white-space: nowrap; }
td { text-align: left; padding: 6px 8px; border-top: 1px solid var(--ef-line-2); white-space: nowrap; vertical-align: middle; }
tr.first td { border-top: 2px solid var(--ef-ink); } tr.top td { border-top: 0; }
th.num, td.num { text-align: right; }
td.num, td.lv { font-family: var(--ef-num); font-weight: 600; font-size: 18px; letter-spacing: .3px; }
td.df { font-weight: 900; font-size: 17px; background: var(--ef-panel-2); border-right: 1px solid var(--ef-line); text-align: center; }
td.df small { display: block; font-family: var(--ef-num); font-weight: 600; font-size: 13px; color: var(--ef-sub); }
td.ei { width: 56px; padding: 4px 4px 4px 12px; }
td.ei img, td.ei .ph { display: block; width: 44px; height: 44px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
td.fe { font-weight: 700; }
td.rs { white-space: normal; color: var(--ef-sub); font-size: 14px; padding-left: 20px; }
th:last-child { padding-left: 20px; }
td.rs b { font-family: var(--ef-num); font-weight: 700; font-size: 17px; color: var(--ef-ink); }
.note { font-size: 13px; color: var(--ef-sub); margin-top: 12px; line-height: 1.7; }
.mech { display: flex; gap: 14px; padding: 12px 16px; border-bottom: 1px solid var(--ef-line); }
.mech .dt { flex: none; width: 62px; align-self: flex-start; padding: 2px 0; text-align: center; font-size: 14px; font-weight: 900; background: var(--ef-ink); color: #f4f4f0; }
.mech .desc { flex: 1; min-width: 0; font-size: 15px; line-height: 1.75; }
.mech .sb { margin-top: 6px; padding-top: 6px; border-top: 1px dashed var(--ef-line); }
.mech .sb b { margin-right: 8px; }
.up { font-family: var(--ef-num); font-weight: 600; font-size: 16px; padding: 0 4px; margin: 0 1px; background: var(--ef-ink); color: var(--ef-yellow); }
.down { font-weight: 700; color: var(--ef-red); }
.key { font-weight: 700; color: var(--ef-ink); box-shadow: inset 0 -.45em 0 var(--ef-yellow); }
.term { font-weight: 700; color: var(--ef-ink); border-bottom: 1px dashed var(--ef-ink); }
.info { color: var(--ef-sub); }
""" + NOTES_CSS

WIDTH = 1100
PLACEHOLDER = '<i class="ph"></i>'


def _img(src: str, missing: str = "") -> str:
    return f'<img src="{escape(src)}">' if src else missing


def res_html(foe: dict) -> str:
    """Resistances of one enemy; the five elements collapse into one figure when a stage levelled them."""
    res = dict(foe.get("res", []))
    if len(res) >= len(ELEMENTS) and len({res.get(e) for e in ELEMENTS}) == 1:
        rest = "".join(f"　{label} <b>{value}</b>" for label, value in res.items() if label not in ELEMENTS)
        return f"五种属性均为 <b>{res[ELEMENTS[0]]}</b>{rest}"
    return "　".join(f"{label} <b>{value}</b>" for label, value in res.items()) or '<span class="dim">无</span>'


def notes_html(foe: dict) -> str:
    """The bonuses already contained in the enemy's numbers: its own and the stage's."""
    parts = [
        f'<span class="bt {style}">{label}</span>{"　".join(escape(note) for note in foe[key])}'
        for key, label, style in (("born", "出生加成", "own"), ("buff", "关卡加成", ""))
        if foe.get(key)
    ]
    return "".join(f'<span class="bs">{part}</span>' for part in parts)


def _stamp(ts: float, pattern: str = "%m/%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, CN).strftime(pattern)


def _levels(group: dict) -> str:
    return "".join(f'<span>{escape(s["difficulty"])} <i>Lv{s["recommend_lv"]}</i></span>' for s in group["stages"])


def _foes_line(group: dict, icons: dict[str, str]) -> str:
    """Enemies of the hardest difficulty, by name."""
    cells = []
    for foe in group["stages"][-1]["enemies"]:
        cells.append(f"<span>{_img(icons.get(foe.get('icon', ''), ''))}{escape(foe['name'])}</span>")
    return "".join(dict.fromkeys(cells))


def _row(group: dict, number: int, covers: dict[str, str], icons: dict[str, str], marked: bool = False) -> str:
    cover = covers.get(group.get("cover", ""), "")
    tower = " tower" if group.get("mode") == "战争回响" else ""
    return (
        f'<div class="st{" me" if marked else ""}"><div class="ord"><b>{number:02d}</b><small>第{number}关</small></div>'
        f'<div class="pic{tower}">{_img(cover)}</div>'
        f'<div class="inf"><h3>{escape(group["name"])}</h3><div class="lv">{_levels(group)}</div>'
        f'<div class="foes">{_foes_line(group, icons)}</div></div></div>'
    )


def overview_html(theme, monument: list[dict], covers: dict[str, str], now: float, version: str) -> str:
    cards = []
    for series in monument:
        art = covers.get(poster(series)[1], "")
        tag = f'<span class="tag">活动中 · {_stamp(series["close"])} 截止</span>' if live(series, now) else ""
        stages = "".join(f'<li><span class="no">{g["no"]}</span>{escape(g["name"])}</li>' for g in series["groups"])
        cards.append(
            f'<div class="card"><div class="art">{_img(art)}{tag}</div>'
            f'<h3>{escape(series["name"])}</h3><ol>{stages}</ol></div>'
        )
    total = sum(len(s["groups"]) for s in monument)
    running = next((s for s in monument if live(s, now)), None)
    chips = f"<span>共 <i>{len(monument)}</i> 个丰碑 · <i>{total}</i> 关</span><span>苦难模式全通即可为该丰碑的蚀刻章镀层</span>"
    if running:
        chips += f'<span class="live">{escape(running["activity"] or running["name"])} · <i>{_stamp(running["close"])}</i> 截止</span>'
    example = monument[0]["name"] if monument else "幽影刻形"
    return _page(
        theme,
        theme.head("Arknights: Endfield · 影拓丰碑", "影拓丰碑", chips),
        f'<div class="grid">{"".join(cards)}</div>',
        f"关卡按游戏内从上到下的顺序排列并标注序号 · 发 /影拓丰碑 {escape(example)} 查看单个丰碑的关卡封面<br>"
        f"只看机制和敌人属性发 /敌人 关卡名，完整攻略发 /攻略 关卡名 苦难 · 数据版本 {_version(version)}",
    )


def series_html(theme, series: dict, covers: dict[str, str], icons: dict[str, str], now: float, version: str, marked: str = "") -> str:
    art = covers.get(poster(series)[1], "")
    chips = f'<span>影拓丰碑</span><span>共 <i>{len(series["groups"])}</i> 关</span>'
    if live(series, now):
        chips += f'<span class="live">{escape(series["activity"] or "活动")}进行中 · <i>{_stamp(series["close"])}</i> 截止</span>'
    rows = "".join(_row(g, g["no"], covers, icons, g["name"] == marked) for g in series["groups"])
    side = f'<div class="poster">{_img(art)}<p>苦难模式全通<br>即可为<b>蚀刻章镀层</b></p></div>'
    first = series["groups"][0]["name"] if series["groups"] else ""
    return _page(
        theme,
        theme.head("Arknights: Endfield · 影拓丰碑", escape(series["name"]), chips),
        f'<div class="split">{side}<div class="rows">{rows}</div></div>',
        f"顺序与游戏内从上到下一致，图标为苦难模式的敌人 · 只看机制和敌人属性发 /敌人 {escape(series['name'])}（或 /敌人 {escape(first)}）<br>"
        f"完整攻略发 /攻略 {escape(first)} 苦难，也可以用序号：/攻略 {escape(series['name'])} 1 · 数据版本 {_version(version)}",
    )


def rotation_html(theme, season: dict, week: dict, groups: list[dict], upcoming: list[tuple[str, str, list[str]]], covers, icons, now: datetime, version: str) -> str:
    end = datetime.strptime(week["close"], "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN)
    left = end - now
    name = week["name"] or f"轮换{week['week']}"
    chips = (
        f"<span>{escape(season['name'])}</span><span>{escape(name)}</span>"
        f'<span class="live">剩余 <i>{left.days}</i> 天 <i>{left.seconds // 3600}</i> 小时 · <i>{end:%m/%d %H:%M}</i> 轮换</span>'
    )
    rows = "".join(_row(g, number, covers, icons) for number, g in enumerate(groups, 1))
    later = "".join(
        f'<div class="next"><b>{escape(title)}</b>{"　".join(escape(n) for n in names)}<i>{escape(opens)} 开放</i></div>' for title, opens, names in upcoming
    )
    first = groups[0]["name"] if groups else "重伤之围"
    return _page(
        theme,
        theme.head("Arknights: Endfield · 战争回响", "战争回响 · <em>本期轮换</em>", chips),
        f'<div class="rows" style="margin-top:16px">{rows}</div>{later}',
        f"顺序与游戏内一致，图标为残酷难度的敌人 · 只看机制和敌人属性发 /回响敌人（或 /敌人 {escape(first)}）<br>"
        f"完整攻略发 /攻略 {escape(first)} 残酷 · 本群用时排名发 /回响竞速 · 数据版本 {_version(version)}",
    )


def _mechanics(stages: list[dict]) -> str:
    """Stage mechanics per difficulty; difficulties with the same text share one block."""
    blocks: list[tuple[list[str], str]] = []
    for stage in stages:
        text = stage.get("feature") or '<span class="dim">无特殊机制说明</span>'
        if stage.get("special_buff"):
            text += f'<div class="sb"><b>特殊增益</b>{stage["special_buff"]}</div>'
        if blocks and blocks[-1][1] == text:
            blocks[-1][0].append(stage["difficulty"])
        else:
            blocks.append(([stage["difficulty"]], text))
    return "".join(f'<div class="mech"><span class="dt">{"<br>".join(escape(d) for d in names)}</span><div class="desc">{text}</div></div>' for names, text in blocks)


def enemies_html(theme, kicker: str, title: str, chips: str, groups: list[dict], difficulty: str | None, icons: dict[str, str], version: str) -> str:
    """Mechanics and enemy attributes: one section per stage, every difficulty (or just `difficulty`).

    With several stages and no difficulty chosen, only the hardest difficulty's mechanics are
    printed to keep the picture short; the enemy table still lists every difficulty.
    """
    sections, same_def, plain = [], set(), False
    brief = len(groups) > 1 and difficulty is None
    for group in groups:
        stages = [s for s in group["stages"] if difficulty in (None, s["difficulty"])] or group["stages"]
        mechanics = _mechanics(stages[-1:] if brief else stages)
        show_def = len({foe.get("def") for s in stages for foe in s["enemies"]}) > 1
        same_def.update(foe.get("def") for s in stages for foe in s["enemies"])
        rows = []
        for stage_no, stage in enumerate(stages):
            span = sum(2 if notes_html(foe) else 1 for foe in stage["enemies"])
            for foe_no, foe in enumerate(stage["enemies"]):
                plain = plain or bool(foe.get("plain"))
                icon, notes = icons.get(foe.get("icon", ""), ""), notes_html(foe)
                level = f'<td class="df" rowspan="{span}">{escape(stage["difficulty"])}<small>Lv{stage["recommend_lv"]}</small></td>' if not foe_no else ""
                rows.append(
                    f'<tr class="{"top" if not stage_no and not foe_no else "first" if not foe_no else ""}">{level}'
                    f'<td class="ei"{" rowspan=2" if notes else ""}>{_img(icon, PLACEHOLDER)}</td>'
                    f'<td class="fe">{escape(foe["name"])}{" *" if foe.get("plain") else ""}</td><td class="lv">Lv{foe["level"]}</td>'
                    f'<td class="num">{foe.get("hp", 0):,}</td><td class="num">{foe.get("atk", 0):,}</td>'
                    + (f'<td class="num">{foe.get("def", 0):,}</td>' if show_def else "")
                    + f'<td class="num">{foe.get("poise", 0):,}</td><td class="rs">{res_html(foe)}</td></tr>'
                    + (f'<tr class="bn"><td colspan="{7 if show_def else 6}">{notes}</td></tr>' if notes else "")
                )
        numbers = 3 if show_def else 2
        head = (
            '<colgroup><col class="c-df"><col class="c-ei"><col class="c-fe"><col class="c-lv">' + '<col class="c-num">' * numbers + '<col class="c-po"><col></colgroup>'
            '<tr><th>难度</th><th></th><th>敌人</th><th>等级</th><th class="num">生命值</th><th class="num">攻击力</th>'
            + ('<th class="num">防御力</th>' if show_def else "")
            + '<th class="num">失衡值上限</th><th>抗性</th></tr>'
        )
        number = f'<span class="no">{group["no"]}</span>' if group.get("no") else ""
        where = escape(group.get("series") or group["mode"]) + (f" · 第{group['no']}关" if group.get("no") else "")
        small = f'<small class="cjk">{where}</small>'
        sections.append(f'<div class="ef-sec"><h3>{number}{escape(group["name"])}{small}</h3>{mechanics}<table>{head}{"".join(rows)}</table></div>')
    notes = []
    if len(same_def) == 1:
        notes.append(f"防御力均为 {next(iter(same_def))}")
    notes.append("数值为敌人在本关卡内的最终属性，已包含每行下方列出的出生加成（该敌人自带）和关卡加成；抗性越高，受到该类伤害越少")
    if plain:
        notes.append("带 * 的敌人不在关卡的固定刷怪配置中（多为战斗中召唤），按其自身属性显示")
    return _page(
        theme,
        theme.head(kicker, title, chips),
        "".join(sections) + f'<div class="note">{"；".join(notes)}</div>',
        ("多关一起看时只列出最高难度的机制，其他难度发 /敌人 关卡名 · " if brief else "")
        + f"关卡数据 AKEData · 通关阵容和推荐视频见 /攻略 关卡名 · 数据版本 {_version(version)}",
    )


def _version(version: str) -> str:
    return f'<span class="ef-num">{escape(version)}</span>'


def _page(theme, head: str, body: str, foot: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{theme.css()}{CSS % WIDTH}</style></head><body class="ef">'
        + head
        + body
        + theme.foot(foot)
        + "</body></html>"
    )
