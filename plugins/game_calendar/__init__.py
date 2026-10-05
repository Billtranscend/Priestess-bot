"""Activity calendar and opening notices for Endfield and Arknights.

/zmd活动   /终末地活动 /zmd日历 ...        Endfield: activities that are open or about to open, and the pools
/mrfz活动  /明日方舟活动 /粥活动 /舟日历 ... Arknights, the same
/活动      /日历                            both games
/活动通知预览 [明日方舟]  (superuser)        the notice for the next activity to open, sent to the current chat only

Every few minutes both calendars are checked: an activity whose opening time has passed gets one
notice picture in every group, with its deadline. All times are Asia/Shanghai. Activities that open
at night are announced when the quiet hours end.

Endfield data is the calendar of the wiki index (AKEData's unpacked ActivityTable); Arknights data
is fetched by arknights.py (game tables, plus the activities PRTS Wiki has already recorded).
Each game's pictures follow that game's own look (themes.py), and every activity row shows the
activity's own banner (Endfield: the game's tab picture from AKEData; Arknights: the announcement
banner from PRTS Wiki), downloaded once and kept in the plugin's data directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Callable

import httpx
from nonebot import get_bots, get_driver, logger, on_command, on_regex, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.exception import MatcherException
from nonebot.params import CommandArg, RegexDict
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_alconna")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_skland")
require("plugins.endfield_guide")
require("plugins.endfield_wiki")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage, message_reaction
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_skland.config import GACHA_DATA_PATH
from nonebot_plugin_skland.data_source import ef_gacha_pool_data

from plugins import ef_theme
from plugins import endfield_guide as guide
from plugins import endfield_wiki as wiki
from plugins.endfield_wiki import data as wiki_data

from . import arknights, themes

__plugin_meta__ = PluginMetadata(
    name="Game calendar",
    description="终末地与明日方舟的活动日历，活动开启时在各群发送通知图。",
    usage="/zmd活动  /mrfz活动  /活动",
    type="application",
)

CN = guide.CN
WIDTH = 1000
STATE_FILE = store.get_plugin_data_file("announced.json")
ARKNIGHTS_FILE = store.get_plugin_data_file("arknights.json")
POOL_ART_FILE = store.get_plugin_data_file("endfield_pools.json")  # pool id -> banner URL answers of the official pool page
POOL_ART_AGE = 6 * 3600  # how long an answer (also "no banner yet") is kept
POOL_CONTENT_URL = "https://ef-webview.hypergryph.com/api/content"  # the game's own pool page; public
ART_DIR = store.get_plugin_data_dir() / "img"  # downloaded activity and pool banners
ART_ATTEMPTS = 3
ART_LIMIT = 4 * 1024 * 1024  # bytes; anything larger is not a banner
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
SPRITE_NAME = re.compile(r"[A-Za-z0-9_]+")
SEND_GAP = 6.0  # seconds between groups
CHECK_MINUTES = 5
ARKNIGHTS_REFRESH_MINUTES = 30
LATE_LIMIT = timedelta(hours=24)  # an opening found later than this (downtime, late data) is not announced any more
QUIET_HOURS = (0, 8)  # [from, to) Beijing hour: openings in this window are announced when it ends; None announces at once
UPCOMING_DAYS = 14  # how far ahead the calendar looks
SOON = timedelta(days=3)  # deadlines closer than this are highlighted
WEEKDAYS = "一二三四五六日"
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"
# Endfield activity id prefixes -> a short kind label; ids that match nothing get no label.
ENDFIELD_KINDS = (
    ("activity_high_difficulty", "高难"), ("activity_stamina", "理智"), ("activity_checkin", "签到"), ("activity_char_trial", "试用"),
    ("characterguide", "干员"), ("web_", "网页"), ("activity_map", "探索"), ("activity_limited_formula", "制造"),
    ("activity_submit", "物资"), ("activity_contingency", "合约"), ("activity_seasontower", "回响"),
)
# What members may type before 活动 / 日历.
ENDFIELD_WORDS = ("zmd", "终末地", "终末", "endfield", "ef")
ARKNIGHTS_WORDS = ("mrfz", "明日方舟", "方舟", "arknights", "ark", "粥", "舟")
COMMAND = re.compile(
    rf"^/\s*(?:(?P<endfield>{'|'.join(ENDFIELD_WORDS)})|(?P<arknights>{'|'.join(ARKNIGHTS_WORDS)}))?\s*的?(?:活动日历|活动列表|活动|日历)\s*$",
    re.IGNORECASE,
)

calendar_cmd = on_regex(COMMAND.pattern, flags=re.IGNORECASE, priority=5, block=True)
preview_cmd = on_command("活动通知预览", permission=SUPERUSER, priority=5, block=True)


# ── data ────────────────────────────────────────────────────────────────


def _parse(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN)
    except (TypeError, ValueError):
        return None


def _unique(rows: list[dict]) -> list[dict]:
    """One row per (name, opening): the tables list some activities several times.

    `key` identifies an activity for the notices; `keys` also holds the names and opening times
    another source uses for it.
    """
    result, seen = [], set()
    for row in sorted(rows, key=lambda a: a["start"]):
        stamps = [f"{moment:%Y/%m/%d %H:%M}" for moment in (row["start"], *(row.get("alt_starts") or []))]
        key = f'{row["name"]}|{stamps[0]}'
        if key not in seen:
            seen.add(key)
            result.append({**row, "key": key, "keys": {f"{name}|{stamp}" for name in (row["name"], *(row.get("aliases") or [])) for stamp in stamps}})
    return result


def _endfield() -> dict:
    """{"activities", "pools", "source"} from the wiki index."""
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
                    "name": row["name"], "kind": kind, "start": start, "end": _parse(row.get("close", "")),
                    "art": wiki_data.activity_banner_url(tab) if SPRITE_NAME.fullmatch(tab) else "",
                    "color": color if COLOR.fullmatch(color) else "",
                }
            )
    pool_rows = []
    for row in calendar.get("pools") or []:
        start, end = _parse(row.get("open", "")), _parse(row.get("close", ""))
        if start and end:
            pool_rows.append({"key": row.get("id", ""), "name": row["name"], "kind": "武器" if row.get("kind") == "weapon" else "角色", "up": row.get("up") or [], "start": start, "end": end})
    return {"activities": _unique(rows), "pools": pool_rows, "source": f'AKEData 游戏解包（版本 {index.get("version", "")}）'}


def _arknights() -> dict:
    index = arknights.load(ARKNIGHTS_FILE)
    moment = lambda seconds: datetime.fromtimestamp(seconds, CN)
    rows = [
        {
            "name": a["name"], "kind": a["kind"], "start": moment(a["start"]), "end": moment(a["end"]),
            "aliases": a.get("aliases") or [], "alt_starts": [moment(t) for t in a.get("alt_starts") or []], "art": a.get("image") or "",
        }
        for a in index.get("activities") or []
    ]
    images = index.get("pool_images") or {}
    pool_rows = [
        {"key": p["id"], "name": p["name"], "kind": p["kind"], "up": p["up"], "start": moment(p["start"]), "end": moment(p["end"]), "art": images.get(p["id"], "")}
        for p in index.get("pools") or []
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


def open_now(items: list[dict], now: datetime) -> list[dict]:
    """Limited-time activities in progress, the one ending first on top. Permanent ones have no deadline to show."""
    return sorted((a for a in items if a["start"] <= now and a["end"] and now < a["end"]), key=lambda a: a["end"])


def upcoming(items: list[dict], now: datetime, days: int = UPCOMING_DAYS) -> list[dict]:
    return [a for a in items if now < a["start"] <= now + timedelta(days=days)]


def current_pools(rows: list[dict], now: datetime) -> list[dict]:
    shown = [p for p in rows if now < p["end"] and p["start"] <= now + timedelta(days=UPCOMING_DAYS)]
    return sorted(shown, key=lambda p: (p["start"] > now, p["end"]))


# ── text ────────────────────────────────────────────────────────────────


def _moment(value: datetime) -> str:
    return f"{value:%m/%d} 周{WEEKDAYS[value.weekday()]} {value:%H:%M}"


def _span(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, hours = minutes // 1440, minutes % 1440 // 60
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    return f"{hours} 小时 {minutes % 60} 分" if hours else f"{minutes % 60} 分钟"


def _deadline(end: datetime | None, now: datetime) -> str:
    """Right-hand cell of a row: the deadline, and how long is left."""
    if end is None:
        return '<div class="dl"><b class="perm">常驻开放</b></div>'
    left = end - now
    return f'<div class="dl"><b>{_moment(end)} 截止</b><span class="{"hot" if left < SOON else ""}">还剩 {_span(left)}</span></div>'


def _art_box(src: str, kind: str = "") -> str:
    return f"<div class=\"art{' ' + kind if kind else ''}\" style=\"background-image:url('{escape(src)}')\"></div>" if src else ""


def _row(theme, activity: dict, right: str, art: dict[str, str]) -> str:
    """One activity: kind and name, its banner when there is one, then the deadline cell."""
    src, color = art.get(activity["key"], ""), activity.get("color") or ""
    classes = "row" + (" pic" if src else "") + (" tint" if color else "")
    tint = f' style="--c:{color}"' if color else ""
    return f'<div class="{classes}"{tint}><div class="nm">{theme.tag(activity["kind"])}<b>{escape(activity["name"])}</b></div>{_art_box(src)}{right}</div>'


def _open_rows(theme, items: list[dict], now: datetime, art: dict[str, str]) -> str:
    return "".join(_row(theme, a, _deadline(a["end"], now), art) for a in items)


def _upcoming_rows(theme, items: list[dict], now: datetime, art: dict[str, str]) -> str:
    rows = ""
    for a in items:
        until = f'{_moment(a["end"])} 截止 · 持续 {_span(a["end"] - a["start"])}' if a["end"] else "常驻开放"
        rows += _row(theme, a, f'<div class="dl"><b>{_moment(a["start"])} 开启</b><span>{until}</span></div>', art)
    return rows


def _pool_rows(theme, items: list[dict], now: datetime, art: dict[str, str]) -> str:
    rows = ""
    for p in items:
        up = f'<small>UP：{escape("、".join(p["up"]))}</small>' if p["up"] else ""
        if p["start"] > now:
            right = f'<div class="dl"><b>{_moment(p["start"])} 开启</b><span>{_moment(p["end"])} 截止</span></div>'
        else:
            right = _deadline(p["end"], now)
        name = f'<b>{escape(p["name"])}</b>' if p["name"] != p["kind"] else ""
        src = art.get(p["key"], "")
        rows += f'<div class="row{" pic" if src else ""}"><div class="nm">{theme.tag(p["kind"], True)}{name}{up}</div>{_art_box(src, "pool")}{right}</div>'
    return rows


# ── pages ───────────────────────────────────────────────────────────────

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
    """The opening notice: each new activity as a large plate with its deadline, then what else is running."""
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
    """file:// URI of a banner, downloaded on first use; "" when it cannot be had (the row then shows none)."""
    suffix = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
    path = ART_DIR / (hashlib.sha1(url.encode()).hexdigest()[:20] + suffix)
    if not path.exists():
        ART_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        for _ in range(ART_ATTEMPTS):  # a fresh connection each time: one of the wiki's CDN nodes does not answer this host
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
    """Activity key -> local banner for the activities that have one."""
    wanted = [a for a in items if a.get("art")]
    sources = await asyncio.gather(*(_banner(a["art"]) for a in wanted))
    return {a["key"]: src for a, src in zip(wanted, sources) if src}


def _pool_cache() -> dict:
    try:
        return json.loads(POOL_ART_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


async def _endfield_pool_url(pool_id: str) -> str:
    """Banner of an Endfield pool: the table nonebot-plugin-skland keeps, else the game's own pool page (current pools only)."""
    known = ef_gacha_pool_data.get_pool(pool_id) if getattr(ef_gacha_pool_data, "pool_table", None) else None
    if known and (known.up6_image or known.rotate_image):
        return known.up6_image or known.rotate_image
    cache = _pool_cache()
    answer = cache.get(pool_id)
    if answer and time.time() - answer.get("ts", 0) < POOL_ART_AGE:
        return answer.get("url", "")
    url = ""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(POOL_CONTENT_URL, params={"lang": "zh-cn", "pool_id": pool_id, "server_id": "1"})
        pool = (response.json().get("data") or {}).get("pool") or {}
        url = pool.get("up6_image") or pool.get("rotate_image") or ""
    except (httpx.HTTPError, ValueError):
        return ""  # not remembered: asked again next time
    cache[pool_id] = {"url": url, "ts": time.time()}
    POOL_ART_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = POOL_ART_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(POOL_ART_FILE)
    return url


async def _pool_banners(game: Game, pool_rows: list[dict]) -> dict[str, str]:
    """Pool key -> local banner."""
    if game.key == "endfield":
        urls = [await _endfield_pool_url(p["key"]) for p in pool_rows]  # one at a time: each answer is added to the same cache file
    else:
        urls = [p.get("art", "") for p in pool_rows]
    wanted = [(p, url) for p, url in zip(pool_rows, urls) if url and url.startswith("https://")]
    sources = await asyncio.gather(*(_banner(url) for _, url in wanted))
    return {p["key"]: src for (p, _), src in zip(wanted, sources) if src}


async def calendar_picture(game: Game, now: datetime) -> bytes:
    data = game.load()
    art = await _banners(open_now(data["activities"], now) + upcoming(data["activities"], now))
    art.update(await _pool_banners(game, current_pools(data["pools"], now)))
    return await _picture(calendar_html(game, now, art))


async def notice_picture(game: Game, opened: list[dict], now: datetime) -> bytes:
    items = game.load()["activities"]
    return await _picture(notice_html(game, opened, now, await _banners(opened + open_now(items, now) + upcoming(items, now, 7))))


# ── opening notices ─────────────────────────────────────────────────────


def _load_state() -> dict[str, list[str]]:
    try:
        state = json.loads(STATE_FILE.read_text("utf-8")).get("announced")
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(state: dict[str, list[str]]) -> None:
    STATE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"announced": {game: sorted(keys) for game, keys in state.items()}}, ensure_ascii=False), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(STATE_FILE)


def due(items: list[dict], announced: set[str], now: datetime) -> tuple[list[dict], set[str]]:
    """(activities to announce now, keys that are settled without a notice because they opened too long ago)."""
    started = [a for a in items if a["start"] <= now and not a["keys"] & announced]
    fresh = [a for a in started if now - a["start"] < LATE_LIMIT and (a["end"] is None or now < a["end"])]
    return fresh, {key for a in started if a not in fresh for key in a["keys"]}


def _quiet(now: datetime) -> bool:
    return QUIET_HOURS is not None and QUIET_HOURS[0] <= now.hour < QUIET_HOURS[1]


def _current_bot() -> Bot | None:
    return next((b for b in get_bots().values() if isinstance(b, Bot)), None)


async def _broadcast(image: bytes) -> dict[str, int]:
    """The picture to every group, without @全体成员."""
    stats = {"ok": 0, "fail": 0}
    bot = _current_bot()
    if bot is None:
        return stats
    for group in await bot.get_group_list():
        for attempt in range(2):
            current = _current_bot()  # the connection may have been re-established during a long run
            try:
                if current is None:
                    raise RuntimeError("bot offline")
                await current.send_group_msg(group_id=group["group_id"], message=MessageSegment.image(image))
                stats["ok"] += 1
                break
            except Exception as e:
                if attempt:
                    stats["fail"] += 1
                    logger.warning(f"Activity notice failed for one group: {type(e).__name__}")
                else:
                    await asyncio.sleep(SEND_GAP)
        await asyncio.sleep(SEND_GAP)
    return stats


_check_lock = asyncio.Lock()


async def _check_game(game: Game, state: dict[str, list[str]], now: datetime) -> None:
    items = game.load()["activities"]
    if not items:  # data not synced yet
        return
    if game.key not in state:  # first run for this game: everything already open is old news
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
    if stats["ok"]:  # nothing delivered (bot offline): try again at the next check
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


# ── commands ────────────────────────────────────────────────────────────


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


@calendar_cmd.handle()
async def _(words: dict = RegexDict()) -> None:
    asked = [game for game in GAMES if words.get(game.key)] or list(GAMES)
    ready = [game for game in asked if game.load()["activities"]]
    if not ready:
        await calendar_cmd.finish("活动数据还没有同步好，请稍后再试")
    await _react(REACTION_PROCESSING)
    try:
        now = datetime.now(CN)
        for game in ready:
            await UniMessage.image(raw=await calendar_picture(game, now)).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Game calendar render failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await calendar_cmd.finish("活动日历生成失败，请稍后再试")
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
    moment = max(now, sample[0]["start"])  # as it will look at the opening
    await UniMessage.image(raw=await notice_picture(game, sample, moment)).send()
    await _react(REACTION_DONE)
