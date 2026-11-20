"""终末地和明日方舟的活动日历及开放通知。

/zmd活动   /终末地活动 /zmd日历 ...        终末地：正在开放和即将开放的活动，以及卡池
/mrfz活动  /明日方舟活动 /粥活动 /舟日历 ... 明日方舟，内容同上
/活动      /日历                            两款游戏；如果机器人能在消息下挂按钮（官方 QQ 机器人），
                                            先让用户选：其中一款，或两款都看
/全部活动  /所有活动 /两款活动               两款游戏，不再询问
/活动通知预览 [明日方舟]  （超级用户）        下一个将开放活动的通知，只发到当前会话

每隔几分钟检查一次两边的日历：开放时间已过的活动，会在每个群发一张带截止时间的通知图。
所有时间均为 Asia/Shanghai。夜间开放的活动等免打扰时段结束后再通知。

终末地数据取自图鉴索引里的日历（AKEData 解包的 ActivityTable）；明日方舟数据由 arknights.py 获取
（游戏数据表，加上 PRTS Wiki 已经收录的活动）。
每个游戏的图片都仿照该游戏自己的风格（themes.py），每一行活动都显示该活动自己的横幅
（终末地：来自 AKEData 的游戏页签图；明日方舟：来自 PRTS Wiki 的公告横幅），
只下载一次，保存在插件的数据目录里。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Callable

import httpx
from nonebot import get_driver, logger, on_command, on_regex, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.exception import MatcherException
from nonebot.params import CommandArg, RegexDict
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_alconna")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_skland")
require("plugins.endfield_wiki")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_skland.config import GACHA_DATA_PATH
from nonebot_plugin_skland.data_source import ef_gacha_pool_data

from plugins import ef_theme
from plugins.quick_buttons import send_buttons
from plugins import endfield_wiki as wiki
from plugins.endfield_wiki import data as wiki_data

from . import arknights, themes
from plugins.shared import REACTION_DONE, REACTION_FAIL, REACTION_PROCESSING, first_bot, write_json, react as _react
from plugins.skland_pools import POOL_CONTENT_URL

__plugin_meta__ = PluginMetadata(
    name="Game calendar",
    description="终末地与明日方舟的活动日历，活动开启时在各群发送通知图。",
    usage="/zmd活动  /mrfz活动  /活动",
    type="application",
)

CN = timezone(timedelta(hours=8))  # 北京时间
WIDTH = 1000
STATE_FILE = store.get_plugin_data_file("announced.json")
ARKNIGHTS_FILE = store.get_plugin_data_file("arknights.json")
RENDER = "#render"  # 武器自身图片的 art 键
POOL_ART_FILE = store.get_plugin_data_file("endfield_pools.json")  # 卡池 id -> 官方卡池页返回的横幅 URL
POOL_ART_AGE = 6 * 3600  # 一次结果（包括“还没有横幅”）保留多久
ART_DIR = store.get_plugin_data_dir() / "img"  # 已下载的活动和卡池横幅
ART_ATTEMPTS = 3
ART_LIMIT = 4 * 1024 * 1024  # 字节；比这更大的不会是横幅
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
SPRITE_NAME = re.compile(r"[A-Za-z0-9_]+")
SEND_GAP = 6.0  # 两个群之间间隔的秒数
CHECK_MINUTES = 5
ARKNIGHTS_REFRESH_MINUTES = 30
LATE_LIMIT = timedelta(hours=24)  # 晚于这个时间才发现的开放（停机、数据来迟）不再通知
QUIET_HOURS = (0, 8)  # 北京时间 [起, 止) 小时：这段时间内的开放等时段结束再通知；None 表示立即通知
UPCOMING_DAYS = 14  # 日历往后看多少天
LONG_WINDOW = timedelta(days=7)  # 持续一年的明日方舟模式（集成战略、生息演算、卫戍协议·规程）只在开放前后这么长时间内显示
SOON = timedelta(days=3)  # 截止时间比这更近的会高亮
WEEKDAYS = "一二三四五六日"
# 终末地活动 id 前缀 -> 简短的类型标签；什么都没匹配上的 id 不加标签。
ENDFIELD_KINDS = (
    ("activity_high_difficulty", "高难"), ("activity_stamina", "理智"), ("activity_checkin", "签到"), ("activity_char_trial", "试用"),
    ("characterguide", "干员"), ("web_", "网页"), ("activity_map", "探索"), ("activity_limited_formula", "制造"),
    ("activity_submit", "物资"), ("activity_contingency", "合约"), ("activity_seasontower", "回响"), ("activity_puzzle", "解谜"),
)
# 群友可以写在 活动 / 日历 前面的词。
ENDFIELD_WORDS = ("zmd", "终末地", "终末", "endfield", "ef")
ARKNIGHTS_WORDS = ("mrfz", "明日方舟", "方舟", "arknights", "ark", "粥", "舟")
BOTH_WORDS = ("全部", "所有", "两款")  # 明说两款游戏都要：选择按钮的最后一个发的就是这个
COMMAND = re.compile(
    rf"^/\s*(?:(?P<endfield>{'|'.join(ENDFIELD_WORDS)})|(?P<arknights>{'|'.join(ARKNIGHTS_WORDS)})|(?P<both>{'|'.join(BOTH_WORDS)}))?\s*的?(?:活动日历|活动列表|活动|日历)\s*$",
    re.IGNORECASE,
)

calendar_cmd = on_regex(COMMAND.pattern, flags=re.IGNORECASE, priority=5, block=True)
preview_cmd = on_command("活动通知预览", permission=SUPERUSER, priority=5, block=True)


# ── 数据 ────────────────────────────────────────────────────────────────


def _parse(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN)
    except (TypeError, ValueError):
        return None


def _unique(rows: list[dict]) -> list[dict]:
    """每个 (名字, 开放时间) 一行：数据表里有些活动列了好几次。

    `key` 是通知用来识别活动的；`keys` 还包含另一个数据源给这个活动用的
    名字和开放时间。
    """
    result, seen = [], set()
    for row in sorted(rows, key=lambda a: a["start"]):
        stamps = [f"{moment:%Y/%m/%d %H:%M}" for moment in (row["start"], *(row.get("alt_starts") or []))]
        key = f'{row["name"]}|{stamps[0]}'
        if key not in seen:
            seen.add(key)
            keys = {f"{name}|{stamp}" for name in (row["name"], *(row.get("aliases") or [])) for stamp in stamps}
            if row.get("id"):  # 游戏数据里的活动 id：开放时间推迟、改了名字，也认得是同一个活动，不再通知第二次
                keys.add(f'id:{row["id"]}')
            result.append({**row, "key": key, "keys": keys})
    return result


def _endfield() -> dict:
    """从图鉴索引得到 {"activities", "pools", "source"}。"""
    loaded = wiki._resolver()
    index = loaded[0] if loaded else {}
    calendar = index.get("calendar") or {}
    rows = []
    for row in calendar.get("activities") or []:
        start = _parse(row.get("open", ""))
        if start and row.get("name"):
            lowered = row.get("id", "").lower()
            kind = next((label for prefix, label in ENDFIELD_KINDS if lowered.startswith(prefix)), "")
            tab, color = row.get("tab") or "", row.get("color") or ""
            rows.append(
                {
                    "name": row["name"], "id": row.get("id", ""), "kind": kind, "start": start, "end": _parse(row.get("close", "")), "shop_end": _parse(row.get("shop_close", "")),
                    "art": wiki_data.activity_banner_url(tab) if SPRITE_NAME.fullmatch(tab) else "",
                    "color": color if COLOR.fullmatch(color) else "",
                }
            )
    pool_rows = []
    for row in calendar.get("pools") or []:
        start, end = _parse(row.get("open", "")), _parse(row.get("close", ""))
        if start and end:
            pool_rows.append(
                {"key": row.get("id", ""), "name": row["name"], "kind": "武器" if row.get("kind") == "weapon" else "角色", "up": row.get("up") or [], "start": start, "end": end, "link": row.get("link") or ""}
            )
    return {"activities": _unique(rows), "pools": _pair_reruns(pool_rows), "source": f'AKEData 游戏解包（版本 {index.get("version", "")}）'}


def _pair_reruns(pool_rows: list[dict]) -> list[dict]:
    """复刻的武器池和它的角色池同时开放：两者合为一行。"""
    by_key = {p["key"]: p for p in pool_rows}
    paired = []
    for p in pool_rows:
        char = by_key.get(p.get("link", ""))
        if char and (char["start"], char["end"]) == (p["start"], p["end"]) and "weapon" not in char:
            char["weapon"] = {"key": p["key"], "name": p["name"], "up": p["up"]}
            paired.append(p)
    return [p for p in pool_rows if p not in paired]


def _arknights() -> dict:
    index = arknights.load(ARKNIGHTS_FILE)
    moment = lambda seconds: datetime.fromtimestamp(seconds, CN)
    rows = [
        {
            "name": a["name"], "kind": a["kind"], "start": moment(a["start"]), "end": moment(a["end"]),
            "aliases": a.get("aliases") or [], "alt_starts": [moment(t) for t in a.get("alt_starts") or []], "art": a.get("image") or "",
            "long": a["end"] - a["start"] > arknights.PERMANENT,
        }
        for a in index.get("activities") or []
    ]
    images = index.get("pool_images") or {}
    pool_rows = [
        {"key": p["id"], "name": p["name"], "kind": p["kind"], "up": p["up"], "note": p.get("note", ""), "start": moment(p["start"]), "end": moment(p["end"]), "art": images.get(p["id"], "")}
        for p in index.get("pools") or []
        if p["up"] or p.get("note")  # 没有固定 UP 也没列出任何内容（中坚甄选、UP 还没公布）：不显示
    ]
    return {"activities": _unique(rows), "pools": pool_rows, "source": "ArknightsGameResource 游戏解包与 PRTS Wiki"}


@dataclass(frozen=True)
class Game:
    key: str
    name: str
    kicker: str
    command: str
    load: Callable[[], dict]
    theme: themes.EndfieldTheme | themes.ArknightsTheme


ENDFIELD = Game("endfield", "终末地", "Endfield", "/zmd活动", _endfield, themes.ENDFIELD)
ARKNIGHTS = Game("arknights", "明日方舟", "Arknights", "/mrfz活动", _arknights, themes.ARKNIGHTS)
GAMES = (ENDFIELD, ARKNIGHTS)


def _in_window(activity: dict, now: datetime) -> bool:
    """长期模式只在开放前后显示：一年后的截止时间对谁都没用。"""
    return not activity.get("long") or activity["start"] - LONG_WINDOW <= now < activity["start"] + LONG_WINDOW


def open_now(items: list[dict], now: datetime) -> list[dict]:
    """正在进行的限时活动，最先结束的排最上面。常驻活动没有截止时间可显示。"""
    return sorted((a for a in items if a["start"] <= now and a["end"] and now < a["end"] and _in_window(a, now)), key=lambda a: a["end"])


def upcoming(items: list[dict], now: datetime, days: int = UPCOMING_DAYS) -> list[dict]:
    return [a for a in items if now < a["start"] <= now + timedelta(days=days) and _in_window(a, now)]


def current_pools(rows: list[dict], now: datetime) -> list[dict]:
    shown = [p for p in rows if now < p["end"] and p["start"] <= now + timedelta(days=UPCOMING_DAYS)]
    return sorted(shown, key=lambda p: (p["start"] > now, p["end"]))


# ── 文字 ────────────────────────────────────────────────────────────────


def _moment(value: datetime) -> str:
    day = f"{value:%m/%d}" if value.year == datetime.now(CN).year else f"{value:%Y/%m/%d}"  # 卫戍协议·规程 要一年后才结束
    return f"{day} 周{WEEKDAYS[value.weekday()]} {value:%H:%M}"


def _span(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, hours = minutes // 1440, minutes % 1440 // 60
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    return f"{hours} 小时 {minutes % 60} 分" if hours else f"{minutes % 60} 分钟"


FAR_END = timedelta(days=5 * 365)  # 比这更远的截止时间等于没有截止时间


def _deadline(end: datetime | None, now: datetime, shop_end: datetime | None = None) -> str:
    """一行右侧的单元格：截止时间、剩余时间（以及商店兑换到什么时候）。"""
    if end is None or end - now > FAR_END:  # 卫戍协议:规程 现在的结束时间是 2099 年：等于永久开放
        return '<div class="dl"><b class="perm">常驻开放</b></div>'
    left = end - now
    shop = f'<span>兑换至 {_moment(shop_end)}</span>' if shop_end else ""
    return f'<div class="dl"><b>{_moment(end)} 截止</b><span class="{"hot" if left < SOON else ""}">还剩 {_span(left)}</span>{shop}</div>'


def _art_box(src: str, kind: str = "") -> str:
    return f"<div class=\"art{' ' + kind if kind else ''}\" style=\"background-image:url('{escape(src)}')\"></div>" if src else ""


def _row(theme, activity: dict, right: str, art: dict[str, str]) -> str:
    """一个活动：类型和名字，有横幅就放横幅，然后是截止时间单元格。"""
    src, color = art.get(activity["key"], ""), activity.get("color") or ""
    classes = "row" + (" pic" if src else "") + (" tint" if color else "")
    tint = f' style="--c:{color}"' if color else ""
    return f'<div class="{classes}"{tint}><div class="nm">{theme.tag(activity["kind"])}<b>{escape(activity["name"])}</b></div>{_art_box(src)}{right}</div>'


def _open_rows(theme, items: list[dict], now: datetime, art: dict[str, str]) -> str:
    return "".join(_row(theme, a, _deadline(a["end"], now, a.get("shop_end")), art) for a in items)


def _upcoming_rows(theme, items: list[dict], now: datetime, art: dict[str, str]) -> str:
    rows = ""
    for a in items:
        until = f'{_moment(a["end"])} 截止 · 持续 {_span(a["end"] - a["start"])}' if a["end"] else "常驻开放"
        rows += _row(theme, a, f'<div class="dl long"><b>{_moment(a["start"])} 开启</b><span>{until}</span></div>', art)
    return rows


def _pool_rows(theme, items: list[dict], now: datetime, art: dict[str, str]) -> str:
    rows = ""
    for p in items:
        note = "UP：" + "、".join(p["up"]) if p["up"] else p.get("note", "")  # 没有固定 UP：显示卡池列出的干员
        weapon = p.get("weapon")
        if weapon:
            note += f" · {weapon['name']}" + (f" UP：{'、'.join(weapon['up'])}" if weapon["up"] else "")
        up = f"<small>{escape(note)}</small>" if note else ""
        if p["start"] > now:
            right = f'<div class="dl"><b>{_moment(p["start"])} 开启</b><span>{_moment(p["end"])} 截止</span></div>'
        else:
            right = _deadline(p["end"], now)
        name = f'<b>{escape(p["name"])}</b>' if p["name"] != p["kind"] else ""
        tags = theme.tag(p["kind"], True)
        src, box = art.get(p["key"], ""), ""
        if weapon:
            tags += theme.tag("武器", True)
            render = art.get(weapon["key"] + RENDER, "")
            box = f'<div class="duo">{_art_box(src, "pool")}<i class="wpn" style="background-image:url(\'{escape(render)}\')"></i></div>' if src and render else ""
        box = box or _art_box(src, "pool")
        rows += f'<div class="row{" pic" if src else ""}"><div class="nm">{tags}{name}{up}</div>{box}{right}</div>'
    return rows


# ── 页面 ────────────────────────────────────────────────────────────────

def _page(game: Game, source: str, head: str, body: str) -> str:
    theme = game.theme
    foot = theme.foot(f"时间均为北京时间 · 数据来自 {escape(source)}，以游戏内公告为准<br>活动日历发 {game.command}")
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{theme.css(WIDTH)}</style></head><body class="{theme.body_class}">{head}{body}{foot}</body></html>'


def calendar_html(game: Game, now: datetime, art: dict[str, str] | None = None) -> str:
    data, theme, art = game.load(), game.theme, art or {}
    current, soon, pool_rows = open_now(data["activities"], now), upcoming(data["activities"], now), current_pools(data["pools"], now)
    head = theme.head(
        f"{game.kicker} · Event Calendar",
        f"{game.name} <em>活动日历</em>",
        f"{now:%Y/%m/%d} 周{WEEKDAYS[now.weekday()]} {now:%H:%M} · 北京时间",
        f"OPEN<b>{len(current)}</b>",
    )
    body = theme.card("正在开放", _open_rows(theme, current, now, art) or theme.empty("当前没有限时活动"), f"{len(current)} 个限时活动 · 按截止时间排序")
    body += theme.card("即将开启", _upcoming_rows(theme, soon, now, art) or theme.empty(f"未来 {UPCOMING_DAYS} 天暂无已公布的新活动"), f"未来 {UPCOMING_DAYS} 天")
    if pool_rows:
        body += theme.card("卡池", _pool_rows(theme, pool_rows, now, art), "寻访")
    return _page(game, data["source"], head, body)


def notice_html(game: Game, opened: list[dict], now: datetime, art: dict[str, str] | None = None) -> str:
    """开放通知：每个新活动一块大板，带截止时间，下面是其他正在进行的活动。"""
    data, theme, art = game.load(), game.theme, art or {}
    keys = {a["key"] for a in opened}
    head = theme.head(
        f"{game.kicker} · Event Notice",
        f"{game.name} <em>活动开启</em>",
        f"{now:%Y/%m/%d} 周{WEEKDAYS[now.weekday()]} · 北京时间",
        f"NEW<b>{len(opened)}</b>",
    )
    body = ""
    for a in opened:
        if a["end"]:
            end = f'<div class="end"><span>截止</span><b>{_moment(a["end"])}</b><em>还剩 {_span(a["end"] - now)}</em></div>'
        else:
            end = '<div class="end"><span>截止</span><b>常驻开放</b></div>'
        src = art.get(a["key"], "")
        body += (
            f'<div class="hero{" pic" if src else ""}">{_art_box(src)}<div class="hk"><i>已开启</i>{theme.tag(a["kind"])}</div><h2>{escape(a["name"])}</h2>{end}'
            f'<div class="from">{_moment(a["start"])} 开启</div></div>'
        )
    others = [a for a in open_now(data["activities"], now) if a["key"] not in keys]
    if others:
        body += theme.card("同时进行中", _open_rows(theme, others, now, art), "按截止时间排序")
    soon = [a for a in upcoming(data["activities"], now, 7) if a["key"] not in keys]
    if soon:
        body += theme.card("即将开启", _upcoming_rows(theme, soon, now, art), "未来 7 天")
    return _page(game, data["source"], head, body)


async def _picture(html: str) -> bytes:
    return await ef_theme.render_page(html, WIDTH, height=600, template_path=wiki.DATA_DIR.as_uri())


async def _banner(url: str) -> str:
    """横幅的 file:// URI，第一次用时下载；拿不到时为 ""（那一行就不显示横幅）。"""
    suffix = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
    path = ART_DIR / (hashlib.sha1(url.encode()).hexdigest()[:20] + suffix)
    if not path.exists():
        ART_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        for _ in range(ART_ATTEMPTS):  # 每次都新建连接：图鉴站有一个 CDN 节点不响应这台机器
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=8), follow_redirects=True, headers={"User-Agent": arknights.USER_AGENT}) as client:
                    response = await client.get(url)
                response.raise_for_status()
                if not response.headers.get("content-type", "").startswith("image/") or len(response.content) > ART_LIMIT:
                    return ""
                tmp = path.with_suffix(".part")
                tmp.write_bytes(response.content)
                tmp.replace(path)
                break
            except httpx.HTTPError:
                continue
        else:
            logger.info("Activity banner unavailable; the row is drawn without it")
            return ""
    return path.as_uri()


async def _banners(items: list[dict]) -> dict[str, str]:
    """活动 key -> 本地横幅，只含有横幅的活动。"""
    wanted = [a for a in items if a.get("art")]
    sources = await asyncio.gather(*(_banner(a["art"]) for a in wanted))
    return {a["key"]: src for a, src in zip(wanted, sources) if src}


def _pool_cache() -> dict:
    try:
        return json.loads(POOL_ART_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


async def _endfield_pool_url(pool_id: str, render: bool = False) -> str:
    """终末地卡池的横幅：先查 nonebot-plugin-skland 保存的表，没有再查游戏自己的卡池页（仅限当前卡池）。
    render：改取武器自身的图片（卡池页的 up6_banner，透明底），用于复刻合并的那一行。"""
    known = ef_gacha_pool_data.get_pool(pool_id) if getattr(ef_gacha_pool_data, "pool_table", None) else None
    if not render and known and (known.up6_image or known.rotate_image):
        return known.up6_image or known.rotate_image
    cache = _pool_cache()
    answer = cache.get(pool_id)
    if answer and time.time() - answer.get("ts", 0) < POOL_ART_AGE:
        return answer.get("render" if render else "url", "")
    url = render_url = ""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(POOL_CONTENT_URL, params={"lang": "zh-cn", "pool_id": pool_id, "server_id": "1"})
        pool = (response.json().get("data") or {}).get("pool") or {}
        url = pool.get("up6_image") or pool.get("rotate_image") or ""
        render_url = pool.get("up6_banner") or ""
    except (httpx.HTTPError, ValueError):
        return ""  # 不记下来：下次再问
    cache[pool_id] = {"url": url, "render": render_url, "ts": time.time()}
    POOL_ART_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(POOL_ART_FILE, cache, ensure_ascii=False)
    return render_url if render else url


async def _pool_banners(game: Game, pool_rows: list[dict]) -> dict[str, str]:
    """卡池 key -> 本地横幅。"""
    if game.key == "endfield":
        urls = [await _endfield_pool_url(p["key"]) for p in pool_rows]  # 逐个请求：每个结果都写进同一个缓存文件
    else:
        urls = [p.get("art", "") for p in pool_rows]
    keys = [p["key"] for p in pool_rows]
    for p in pool_rows:
        if game.key == "endfield" and p.get("weapon"):
            keys.append(p["weapon"]["key"] + RENDER)
            urls.append(await _endfield_pool_url(p["weapon"]["key"], render=True))
    wanted = [(key, url) for key, url in zip(keys, urls) if url and url.startswith("https://")]
    sources = await asyncio.gather(*(_banner(url) for _, url in wanted))
    return {key: src for (key, _), src in zip(wanted, sources) if src}


async def calendar_picture(game: Game, now: datetime) -> bytes:
    data = game.load()
    art = await _banners(open_now(data["activities"], now) + upcoming(data["activities"], now))
    art.update(await _pool_banners(game, current_pools(data["pools"], now)))
    return await _picture(calendar_html(game, now, art))


async def notice_picture(game: Game, opened: list[dict], now: datetime) -> bytes:
    items = game.load()["activities"]
    return await _picture(notice_html(game, opened, now, await _banners(opened + open_now(items, now) + upcoming(items, now, 7))))


# ── 开放通知 ────────────────────────────────────────────────────────────


def _load_state() -> dict[str, list[str]]:
    try:
        state = json.loads(STATE_FILE.read_text("utf-8")).get("announced")
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(state: dict[str, list[str]]) -> None:
    STATE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(STATE_FILE, {"announced": {game: sorted(keys) for game, keys in state.items()}}, ensure_ascii=False)


def due(items: list[dict], announced: set[str], now: datetime) -> tuple[list[dict], set[str]]:
    """(现在要通知的活动, 因开放太久而不通知、直接记为已处理的 key)。"""
    started = [a for a in items if a["start"] <= now and not a["keys"] & announced]
    fresh = [a for a in started if now - a["start"] < LATE_LIMIT and (a["end"] is None or now < a["end"])]
    return fresh, {key for a in started if a not in fresh for key in a["keys"]}


def _quiet(now: datetime) -> bool:
    return QUIET_HOURS is not None and QUIET_HOURS[0] <= now.hour < QUIET_HOURS[1]


UNPROMPTED_REFUSED = 40_034_105  # 平台：群里没开“机器人主动在群聊内发言”，重试也一样


async def _broadcast(image: bytes) -> dict[str, int]:
    """把图发到每个群，不 @全体成员。不让机器人主动发言的群只试一次（refused），其他失败再试一次（fail）。"""
    stats = {"ok": 0, "refused": 0, "fail": 0}
    bot = first_bot()
    if bot is None:
        return stats
    for group in await bot.get_group_list():
        refused = False
        for attempt in range(2):
            current = first_bot()  # 发送时间较长，期间连接可能已经重建
            try:
                if current is None:
                    raise RuntimeError("bot offline")
                await current.send_group_msg(group_id=group["group_id"], message=MessageSegment.image(image))
                stats["ok"] += 1
                break
            except Exception as e:
                refused = getattr(e, "retcode", None) == UNPROMPTED_REFUSED
                if refused:
                    stats["refused"] += 1
                    break
                if attempt:
                    stats["fail"] += 1
                    logger.warning(f"Activity notice failed for one group: {type(e).__name__}")
                else:
                    await asyncio.sleep(SEND_GAP)
        if not refused:  # 被拒的群什么也没收到，不用为它等发送间隔
            await asyncio.sleep(SEND_GAP)
    return stats


_check_lock = asyncio.Lock()


async def _check_game(game: Game, state: dict[str, list[str]], now: datetime) -> None:
    items = game.load()["activities"]
    if not items:  # 数据还没同步
        return
    if game.key not in state:  # 这个游戏第一次运行：已经开放的都算旧消息
        state[game.key] = sorted({key for a in items if a["start"] <= now for key in a["keys"]})
        _save_state(state)
        logger.info(f"Activity notices armed for {game.key}: activities that are already open will not be announced")
        return
    announced = set(state[game.key])
    fresh, stale = due(items, announced, now)
    if stale:
        announced |= stale
        state[game.key] = sorted(announced)
        _save_state(state)
    if not fresh or _quiet(now):
        return
    started = time.time()
    stats = await _broadcast(await notice_picture(game, fresh, now))
    if stats["ok"] or (stats["refused"] and not stats["fail"]):  # 一条都没发出去（机器人离线、出错）：下次检查时再试；全被拒收的再试也一样
        state[game.key] = sorted(announced | {key for a in fresh for key in a["keys"]})
        _save_state(state)
    logger.info(f"Activity notice for {len(fresh)} {game.key} activities sent in {time.time() - started:.0f}s: {stats}")


@scheduler.scheduled_job("cron", minute=f"*/{CHECK_MINUTES}", second=30, timezone="Asia/Shanghai", id="game_calendar_notice")
async def _check() -> None:
    if _check_lock.locked():
        return
    async with _check_lock:
        state = _load_state()
        for game in GAMES:
            try:
                await _check_game(game, state, datetime.now(CN))
            except Exception as e:
                logger.warning(f"Activity notice check failed for {game.key}: {type(e).__name__}")


@scheduler.scheduled_job("interval", minutes=ARKNIGHTS_REFRESH_MINUTES, id="game_calendar_arknights")
async def _refresh_arknights() -> None:
    try:
        outcome = await arknights.refresh(ARKNIGHTS_FILE, GACHA_DATA_PATH / "character_table.json")
    except Exception as e:
        logger.warning(f"Arknights calendar data not refreshed: {type(e).__name__}")
        return
    if outcome != "unchanged":
        logger.info(f"Arknights calendar data {outcome}")


_background: set[asyncio.Task] = set()


@get_driver().on_startup
async def _first_refresh() -> None:
    task = asyncio.get_running_loop().create_task(_refresh_arknights())
    _background.add(task)
    task.add_done_callback(_background.discard)


# ── 指令 ────────────────────────────────────────────────────────────────




def calendar_choice() -> tuple[str, list[list[tuple[str, str]]]]:
    """能显示按钮时，两款游戏共用的指令提供的选项：单看某一款，或两款一起。"""
    return "**活动日历** 看哪一款？", [[("明日方舟：终末地", ENDFIELD.command), ("明日方舟", ARKNIGHTS.command)], [("一起看", f"/{BOTH_WORDS[0]}活动")]]


async def _drop(tasks: list[asyncio.Task]) -> None:
    """中途出错时还没用上的图：停掉并等它收尾，不留下没人取的异常。"""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@calendar_cmd.handle()
async def _(words: dict = RegexDict()) -> None:
    named = [game for game in GAMES if words.get(game.key)]
    # 没指明游戏：能用按钮时先问看哪个。日历本身就是最终回答，下面不再挂按钮。
    # 无文字兜底：显示不了按钮时不用先问，下面直接发两款游戏的日历
    if not named and not words.get("both") and await send_buttons(*calendar_choice()):
        await calendar_cmd.finish()
    asked = named or list(GAMES)
    ready = [game for game in asked if game.load()["activities"]]
    if not ready:
        await calendar_cmd.finish("活动数据还没有同步好，请稍后再试")
    await _react(REACTION_PROCESSING)
    now = datetime.now(CN)
    drawing = [asyncio.create_task(calendar_picture(game, now)) for game in ready]  # 几张图一起画，按 ready 的顺序发
    try:
        for task in drawing:
            await UniMessage.image(raw=await task).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Game calendar render failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await calendar_cmd.finish("活动日历生成失败，请稍后再试")
    finally:
        await _drop(drawing)
    await _react(REACTION_DONE)


@preview_cmd.handle()
async def _(arg: Message = CommandArg()) -> None:
    game = ARKNIGHTS if any(word in arg.extract_plain_text().lower() for word in ARKNIGHTS_WORDS) else ENDFIELD
    now = datetime.now(CN)
    items = game.load()["activities"]
    coming = [a for a in items if a["start"] > now]
    sample = [a for a in coming if a["start"] == coming[0]["start"]] if coming else open_now(items, now)[-1:]
    if not sample:
        await preview_cmd.finish("没有可预览的活动")
    await _react(REACTION_PROCESSING)
    moment = max(now, sample[0]["start"])  # 显示成开放那一刻的样子
    await UniMessage.image(raw=await notice_picture(game, sample, moment)).send()
    await _react(REACTION_DONE)
