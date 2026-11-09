"""Endfield endgame stage guides: 战争回响 and 影拓丰碑.

/战争回响                 this week's rotation: stages in game order with their covers
/影拓丰碑 [丰碑名]         every series with its cover; one series: its stages in game order with covers
/敌人 <关卡|丰碑> [难度]   mechanics and enemy attributes (/回响敌人: this week's rotation, /丰碑敌人: the current series)
/攻略统计 退出|加入        opt out of / back into the group clear-team statistics
/竞速榜                   menu → /回响竞速 [关卡]  /丰碑竞速 [丰碑|关卡]
/竞速更新                 fetch the sender's own 战争回响 / 影拓丰碑 clears now, so the boards show them at once
                          (owner 2026-11-06: members refresh themselves instead of waiting for the collection);
                          once per REFRESH_COOLDOWN per member, not while the collection runs
/干员统计 [关卡|战争回响|影拓丰碑]  one table of the 6-star operators: share of bound roles (every account, 权限等级 30+)
                          owning each, the owners' average potential, and the share of recorded hard clears using it
                          (owner 2026-11-08: the former /zmd持有率 and /榜单角色出场率 merged, their names kept as aliases)

Stage data comes from the endfield_wiki AKEData index; clears and teams from members'
Skland War Echoes records (collected twice a day). /攻略 (stage card with group teams and
Bilibili videos) was removed 2026-11-08: members read /敌人 instead.
The game's stage order and the series covers also come from Skland (see stages.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape

from nonebot import get_driver, logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import MatcherException
from nonebot.params import Command, CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("plugins.endfield_wiki")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage, message_reaction
from nonebot_plugin_apscheduler import scheduler

from plugins import ef_theme
from plugins import endfield_wiki as wiki
from plugins.endfield_wiki import data as wiki_data
from plugins.endfield_wiki.lookup import normalize, to_pinyin

from . import stages
from .builds import HOLDING_MIN_LEVEL, BuildStore
from .echoes import ECHO_SCHEMA, EchoStore, partners, speed_ranking, usage_rates

__plugin_meta__ = PluginMetadata(
    name="Endfield stage guide",
    description="战争回响 / 影拓丰碑 stage pages, enemies, speed boards and operator stats.",
    usage="/战争回响  /影拓丰碑  /敌人 重伤之围 残酷",
    type="application",
)

DATA_DIR = store.get_plugin_data_dir()
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
echo_store = EchoStore(DATA_DIR / "echoes.json", DATA_DIR / "optout.json")
build_store = BuildStore(DATA_DIR / "builds.json")  # read by endfield_wiki for the build statistics on its cards
covers = stages.Covers(wiki.IMAGE_DIR)
CN = timezone(timedelta(hours=8))
WIDTH = 1100
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"
DIFFICULTY_WORDS = {
    "普通": "普通", "简单": "普通", "困难": "困难", "残酷": "残酷", "增辉": "残酷",
    "苦难": "苦难", "镀层": "苦难", "高难": "苦难",
}
STAGE_NUMBER = re.compile(r"^第?(\d{1,2})关?$")  # "2" / "第2关": a stage of a 影拓丰碑 series by its number
ROTATION_WORDS = ("战争回响", "回响", "本周", "本期")
MONUMENT_WORDS = ("影拓丰碑", "丰碑")
ENEMY_USAGE = (
    "/敌人 领袖敌人名　该敌人的特性和各级默认属性，例：/敌人 祀、/敌人 罗丹\n"
    "/敌人 领袖　领袖敌人目录\n\n"
    "关卡机制和敌人属性\n"
    "/敌人 关卡名 [难度]　例：/敌人 暗曜白霆、/敌人 重伤之围 残酷\n"
    "/敌人 丰碑名　整个丰碑的全部关卡，例：/敌人 幽影刻形\n"
    "/敌人 丰碑名 序号　例：/敌人 幽影刻形 2\n"
    "/回响敌人　本期战争回响的三个关卡\n"
    "/丰碑敌人　当前活动中的丰碑"
)

rotation = on_command("战争回响", rule=strict, priority=5, block=True)
monument_list = on_command("影拓丰碑", rule=strict, priority=5, block=True)
enemy = on_command("敌人", aliases={"敌人属性", "敌人数值", "回响敌人", "丰碑敌人"}, rule=strict, priority=5, block=True)
stats_opt = on_command("攻略统计", aliases={"战绩统计"}, rule=strict, priority=5, block=True)
speed_board = on_command("竞速榜", rule=strict, priority=5, block=True)
echo_speed = on_command("回响竞速", aliases={"战争回响竞速"}, rule=strict, priority=5, block=True)
monument_speed = on_command("丰碑竞速", aliases={"影拓丰碑竞速"}, rule=strict, priority=5, block=True)
operator_stats = on_command(
    "干员统计",
    aliases={"zmd干员统计", "榜单角色出场率", "角色出场率", "zmd持有率", "终末地持有率", "干员持有率", "持有率"},
    rule=strict,
    priority=5,
    block=True,
)
collect_now = on_command("攻略数据更新", rule=strict, permission=SUPERUSER, priority=5, block=True)
speed_refresh = on_command("竞速更新", aliases={"更新竞速"}, rule=strict, priority=5, block=True)
REFRESH_COOLDOWN = 300  # seconds between one member's /竞速更新
REFRESH_LISTED = 8  # faster clears named in the answer
_refreshed: dict[str, float] = {}  # qq -> monotonic time of their last successful /竞速更新 (or of one still running)
_refreshing: set[str] = set()  # members whose /竞速更新 is running


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


def _endgame() -> dict:
    """The stage index, with 影拓丰碑 in the game's order (series cover and stage numbers attached)."""
    loaded = wiki._resolver()
    return stages.ordered(loaded[0].get("endgame", {}) if loaded else {}, echo_store.layout())


def _leaders() -> list[dict]:
    loaded = wiki._resolver()
    return loaded[0].get("leaders", []) if loaded else []


LEADER_LIST_WORDS = {"领袖", "领袖敌人", "目录", "列表"}


def _leader(query: str) -> tuple[str, object] | None:
    """('leader', entry) | ('candidates', [names]) | None. Exact name first, then a name containing the query."""
    leaders, q = _leaders(), normalize(query)
    if not q:
        return None
    named = [(normalize(l["name"]), l) for l in leaders]
    found = [l for n, l in named if n == q] or ([l for n, l in named if q in n] if len(q) >= 2 else [])
    names = list(dict.fromkeys(l["name"] for l in found))
    if len(names) > 1:
        return "candidates", names
    if found:  # one name; two entries of it (白垩界卫) -> the one with its own challenge
        return "leader", max(found, key=lambda l: l["challenge"])
    return None


def _leader_list() -> str:
    lines, with_challenge = [], {l["name"] for l in _leaders() if l["challenge"]}
    for l in sorted(_leaders(), key=lambda l: (not l["challenge"], l["name"])):
        if not l["challenge"] and l["name"] in with_challenge:
            continue  # 白垩界卫 has a second, challenge-less form; /敌人 shows the one with the challenge
        lvs = l["levels"]
        span = f"{lvs[0]['tier']}～{lvs[-1]['tier']}　Lv{lvs[0]['level']}～{lvs[-1]['level']}" if l["challenge"] else "无专属挑战"
        lines.append(f"· {l['name']}　{span}")
    return "领袖敌人目录（发 /敌人 名字 查看特性和各级属性）\n" + "\n".join(lines)


def _version() -> str:
    loaded = wiki._resolver()
    return loaded[0].get("version", "") if loaded else ""


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN)
    except ValueError:
        return None


def _current_week(endgame: dict) -> tuple[dict, dict] | None:
    now = datetime.now(CN)
    for season in endgame.get("tower_seasons", []):
        for week in season["weeks"]:
            start, end = _parse_time(week["open"]), _parse_time(week["close"])
            if start and end and start <= now < end:
                return season, week
    return None


def _stage_groups(endgame: dict) -> list[dict]:
    groups = list(endgame.get("tower", {}).values())
    for series in endgame.get("monument", []):
        groups.extend(series["groups"])
    return groups


def _resolve(endgame: dict, query: str) -> tuple[str, object] | None:
    """('group', group) | ('series', series) | ('candidates', [names]) | None."""
    q = normalize(query)
    if not q:
        return None
    entries = [(normalize(g["name"]), "group", g) for g in _stage_groups(endgame)]
    entries += [(normalize(s["name"]), "series", s) for s in endgame.get("monument", [])]
    exact = [e for e in entries if e[0] == q]
    if exact:
        return exact[0][1], exact[0][2]
    readings = to_pinyin(q)
    by_sound = [e for e in entries if readings & to_pinyin(e[0])]
    if len(by_sound) == 1:
        return by_sound[0][1], by_sound[0][2]
    prefix = [e for e in entries if e[0].startswith(q) or (len(q) >= 2 and q in e[0])]
    if len(prefix) == 1:
        return prefix[0][1], prefix[0][2]
    if 1 < len(prefix) <= 8:
        return "candidates", [e[2]["name"] for e in prefix]
    close = difflib.get_close_matches(q, [e[0] for e in entries], n=2, cutoff=0.6)
    if len(close) == 1:
        match = next(e for e in entries if e[0] == close[0])
        return match[1], match[2]
    return None


def _words(arg: Message) -> tuple[str, str | None, int | None]:
    """(name, difficulty, stage number) from a command's arguments."""
    words = arg.extract_plain_text().split()
    difficulty = next((DIFFICULTY_WORDS[w] for w in words if w in DIFFICULTY_WORDS), None)
    number = next((int(m.group(1)) for w in words if (m := STAGE_NUMBER.match(w))), None)
    return "".join(w for w in words if w not in DIFFICULTY_WORDS and not STAGE_NUMBER.match(w)), difficulty, number


def _numbered(series: dict) -> str:
    return "、".join(f"{g['no']}.{g['name']}" for g in series["groups"])


async def _foe_icons(groups: list[dict]) -> dict[str, str]:
    ids = sorted({foe["icon"] for g in groups for s in g["stages"] for foe in s["enemies"] if foe.get("icon")})
    uris = await asyncio.gather(*(wiki._cached_image(wiki_data.enemy_icon_url(i), f"{i}.png") for i in ids))
    return dict(zip(ids, uris))


def _cover_jobs(series: list[dict], groups: list[dict]) -> list[tuple[str, str, int]]:
    return [stages.poster(s) for s in series] + [stages.stage_cover(g, wiki_data.stage_cover_url(g)) for g in groups]


async def _rich_css() -> None:
    """The game's text colours and term icons for the pages built next (same as on the wiki cards)."""
    with contextlib.suppress(Exception):
        loaded = wiki._resolver()
        if loaded:
            stages.RICH_CSS = await wiki.rich_css(loaded[0])


async def _send_page(matcher, build, fallback: str) -> None:
    """Render and send a menu page; `build` returns its HTML. Falls back to the plain text."""
    await _react(REACTION_PROCESSING)
    try:
        await _rich_css()
        image = await ef_theme.render_page(await build(), stages.WIDTH, height=300, template_path=wiki.DATA_DIR.as_uri())
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Stage menu failed: {type(e).__name__}: {e}")
        await _react(REACTION_FAIL)
        await matcher.finish(fallback)
    await _react(REACTION_DONE)



_partners_cache: dict = {"key": None, "value": {}}


def team_partners() -> dict[str, dict]:
    """Teammates of every operator in members' stored clears (read by endfield_wiki for its cards), recomputed when the clears change."""
    try:
        key = echo_store.data_file.stat().st_mtime
    except OSError:
        return {}
    if _partners_cache["key"] != key:
        _partners_cache.update(key=key, value=partners(_load_echoes(), ENDMIN_IDS))
    return _partners_cache["value"]


def _load_echoes() -> dict:
    """Stored clears with Skland's md5(charId) hashes mapped back to game operator ids."""
    data = echo_store.load()
    ids = list(wiki._resolver()[0]["operators"]) + list(ENDMIN_IDS) + list(ENDMIN_IDS.values())
    by_hash = {hashlib.md5(cid.encode()).hexdigest(): cid for cid in ids}
    for member in data.get("members", {}).values():
        for record in member.get("records", {}).values():
            for char in record["team"]:
                char["id"] = by_hash.get(char["id"], char["id"])
    return data


# ── commands ───────────────────────────────────────────────────────────


def _rotation(endgame: dict) -> tuple[dict, dict, list[dict]] | None:
    """(season, week, stages numbered in the game's order) of the rotation that is open now."""
    current = _current_week(endgame)
    if not current:
        return None
    season, week = current
    return season, week, [{**endgame["tower"][g], "no": n} for n, g in enumerate((g for g in week["groups"] if g in endgame["tower"]), 1)]


def _upcoming(endgame: dict) -> list[tuple[str, str, list[str]]]:
    """The next rotation that has not opened yet: (title, opening time, stage names)."""
    now = datetime.now(CN)
    for season in endgame.get("tower_seasons", []):
        for week in season["weeks"]:
            start = _parse_time(week["open"])
            names = [endgame["tower"][g]["name"] for g in week["groups"] if g in endgame["tower"]]
            if start and start > now and names:
                return [(f"下期 · {week['name'] or '轮换' + str(week['week'])}", f"{start:%m/%d %H:%M}", names)]
    return []


@rotation.handle()
async def _() -> None:
    endgame = _endgame()
    current = _rotation(endgame)
    if not current:
        await rotation.finish("当前没有开放中的战争回响轮换（数据可能尚未更新）")
    season, week, groups = current
    end = _parse_time(week["close"])
    left = end - datetime.now(CN)
    names = [g["name"] for g in groups]
    lines = [f"战争回响 · {season['name']} · {week['name'] or '轮换' + str(week['week'])}",
             f"剩余 {left.days} 天 {left.seconds // 3600} 小时（{end:%m/%d %H:%M} 轮换）", ""]
    lines += [f"{i}. {n}" for i, n in enumerate(names, 1)]
    lines += ["", "机制和敌人属性发 /回响敌人，单关发 /敌人 关卡名，例如 /敌人 " + (names[0] if names else "重伤之围") + " 残酷"]

    async def build() -> str:
        art, icons = await asyncio.gather(covers.get(_cover_jobs([], groups)), _foe_icons(groups))
        return stages.rotation_html(ef_theme, season, week, groups, _upcoming(endgame), art, icons, datetime.now(CN), _version())

    await _send_page(rotation, build, "\n".join(lines))


def _monument_text(endgame: dict) -> str:
    lines = ["影拓丰碑（苦难模式全通即可为该丰碑的蚀刻章镀层；关卡按游戏内顺序）"]
    lines += [f"【{series['name']}】{_numbered(series)}" for series in endgame.get("monument", [])]
    if endgame.get("monument"):
        first = endgame["monument"][0]
        lines.append(f"\n机制和敌人属性发 /敌人 关卡名，例如 /敌人 {first['groups'][0]['name']} 苦难\n整个丰碑发 /敌人 {first['name']}")
    return "\n".join(lines)


@monument_list.handle()
async def _(arg: Message = CommandArg()) -> None:
    endgame = _endgame()
    monument = endgame.get("monument", [])
    query = arg.extract_plain_text().strip().removesuffix("丰碑")
    if not monument:
        await monument_list.finish("影拓丰碑数据尚未同步，请稍后再试")
    if not query:

        async def overview() -> str:
            art = await covers.get(_cover_jobs(monument, []))
            return stages.overview_html(ef_theme, monument, art, time.time(), _version())

        await _send_page(monument_list, overview, _monument_text(endgame))
        return
    found = _resolve(endgame, query)
    if found and found[0] == "group" and found[1]["mode"] == "战争回响":
        await monument_list.finish(f"「{found[1]['name']}」是战争回响关卡，本期轮换发 /战争回响")
    if not found or found[0] == "candidates":
        hint = f"找到多个：{'、'.join(found[1])}" if found else f"没有找到丰碑「{query}」"
        await monument_list.finish(hint + "\n\n" + _monument_text(endgame))
    marked = found[1]["name"] if found[0] == "group" else ""
    series = found[1] if found[0] == "series" else next(s for s in monument if s["name"] == found[1]["series"])

    async def page() -> str:
        art, icons = await asyncio.gather(covers.get(_cover_jobs([series], series["groups"])), _foe_icons(series["groups"]))
        return stages.series_html(ef_theme, series, art, icons, time.time(), _version(), marked)

    await _send_page(monument_list, page, f"影拓丰碑【{series['name']}】（按游戏内顺序）：{_numbered(series)}")


@enemy.handle()
async def _(arg: Message = CommandArg(), command: tuple[str, ...] = Command()) -> None:
    query, difficulty, number = _words(arg)
    endgame = _endgame()
    monument = endgame.get("monument", [])
    query = query or {"回响敌人": "战争回响", "丰碑敌人": "影拓丰碑"}.get(command[0], "")
    if not query:
        await enemy.finish(ENEMY_USAGE)
    if query in LEADER_LIST_WORDS:
        await enemy.finish(_leader_list() if _leaders() else "领袖敌人数据尚未同步，请稍后再试")
    if command[0] == "敌人" and (leader := _leader(query)):
        if leader[0] == "candidates":
            await enemy.finish(f"找到多个领袖敌人：{'、'.join(leader[1])}\n请输入更完整的名字")
        entry = leader[1]

        async def build_leader() -> str:
            icon = await wiki._cached_image(wiki_data.enemy_icon_url(entry["id"]), f"{entry['id']}.png")
            return stages.leader_html(ef_theme, entry, icon, _version())

        await _send_page(enemy, build_leader, "敌人资料图生成失败，请稍后再试")
        return
    series = None
    if query in ROTATION_WORDS:
        current = _rotation(endgame)
        if not current:
            await enemy.finish("当前没有开放中的战争回响轮换（数据可能尚未更新）")
        season, week, groups = current
        title, scope = "机制与敌人 · <em>本期战争回响</em>", f"{season['name']} · {week['name'] or '轮换' + str(week['week'])}"
    elif query in MONUMENT_WORDS:
        series = next((s for s in monument if stages.live(s, time.time())), monument[0] if monument else None)
        if series is None:
            await enemy.finish("影拓丰碑数据尚未同步，请稍后再试")
    else:
        found = _resolve(endgame, query.removesuffix("丰碑") or query)
        if not found:
            await enemy.finish(f"没有找到「{query}」，发 /战争回响 或 /影拓丰碑 查看全部关卡名\n\n{ENEMY_USAGE}")
        if found[0] == "candidates":
            await enemy.finish(f"找到多个关卡：{'、'.join(found[1])}\n请输入更完整的名字")
        if found[0] == "series":
            series = found[1]
        else:
            groups = [found[1]]
            title, scope = f"机制与敌人 · <em>{escape(found[1]['name'])}</em>", found[1].get("series") or "战争回响"
    if series is not None:
        if number and 1 <= number <= len(series["groups"]):
            groups = [series["groups"][number - 1]]
            title, scope = f"机制与敌人 · <em>{escape(groups[0]['name'])}</em>", series["name"]
        else:
            groups = series["groups"]
            title, scope = f"机制与敌人 · <em>{escape(series['name'])}</em>", f"影拓丰碑 · 共 {len(groups)} 关"
    if difficulty and not any(s["difficulty"] == difficulty for g in groups for s in g["stages"]):
        difficulty = None
    chips = f"<span>{escape(scope)}</span><span>{escape(difficulty) if difficulty else '全部难度'}</span>"
    kicker = f"Arknights: Endfield · {groups[0]['mode']}"

    async def build() -> str:
        return stages.enemies_html(ef_theme, kicker, title, chips, groups, difficulty, await _foe_icons(groups), _version())

    await _send_page(enemy, build, "敌人属性图生成失败，请稍后再试")


@stats_opt.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    if action not in ("退出", "加入"):
        await stats_opt.finish("用法：/攻略统计 退出  或  /攻略统计 加入\n（竞速榜、/干员统计 和资料卡上的常用队友会使用已绑定群友的战争回响 / 影拓丰碑最佳记录和干员持有情况）")
    echo_store.set_optout(str(event.user_id), action == "退出")
    await _react(REACTION_DONE)
    await stats_opt.finish("已退出统计，你的记录不会再被收集，也不会出现在竞速榜上" if action == "退出" else "已重新加入通关阵容统计，下次更新后生效", at_sender=True)


@speed_refresh.handle()
async def _(event: MessageEvent) -> None:
    qq = str(event.user_id)
    since = time.monotonic() - _refreshed.get(qq, -REFRESH_COOLDOWN)
    if since < REFRESH_COOLDOWN:
        ago = "刚刚" if since < 10 else f"{int(since)} 秒前" if since < 60 else f"{int(since // 60)} 分钟前"
        if qq in _refreshing:
            await speed_refresh.finish("正在更新你的记录，请稍等", at_sender=True)
        await speed_refresh.finish(f"你 {ago}已更新成功，成绩已在榜上；{int(REFRESH_COOLDOWN // 60)} 分钟内不用重复更新", at_sender=True)
    _refreshing.add(qq)
    _refreshed[qq] = time.monotonic()
    await _react(REACTION_PROCESSING)
    try:
        outcome, before, after = await echo_store.refresh(qq)
    except Exception as e:
        logger.warning(f"Speed refresh failed: {type(e).__name__}")
        outcome, before, after = "failed", {}, {}
    finally:
        _refreshing.discard(qq)
    if outcome == "ok":
        _refreshed[qq] = time.monotonic()  # the cooldown counts from the finished update
    else:
        _refreshed.pop(qq, None)  # nothing was updated: may try again at once
        await _react(REACTION_FAIL)
        await speed_refresh.finish({
            "busy": "全员战绩正在更新中，结束后榜单就会包含你的最新记录，不用再发",
            "optout": "你已退出攻略统计，记录不会被收集。发 /攻略统计 加入 重新加入后再更新",
            "unbound": "还没有绑定森空岛的终末地角色，绑定后才能更新竞速记录",
            "failed": "森空岛暂时没有返回你的战绩，请稍后再试",
        }[outcome], at_sender=True)
    faster = [(key, before[key]["time"], record["time"]) for key, record in after.items() if key in before and record.get("time") is not None and before[key].get("time") is not None and record["time"] < before[key]["time"]]
    fresh = [key for key in after if key not in before]
    lines = [f"竞速记录已更新，共 {len(after)} 条通关记录，榜单已同步"]
    if faster:
        lines.append(f"{len(faster)} 关成绩变快：")
        lines += [f"{key.replace('|', ' ')}  {_fmt_time(old)} → {_fmt_time(new)}" for key, old, new in faster[:REFRESH_LISTED]]
        if len(faster) > REFRESH_LISTED:
            lines.append(f"……另有 {len(faster) - REFRESH_LISTED} 关")
    if fresh:
        lines.append(f"新增 {len(fresh)} 关通关记录")
    if not faster and not fresh:
        lines.append("没有比上次更快的成绩")
    await _react(REACTION_DONE)
    await speed_refresh.finish("\n".join(lines), at_sender=True)


@collect_now.handle()
async def _() -> None:
    await collect_now.send("开始收集战争回响通关记录和干员配装，约需半小时（期间榜单会陆续更新）…")
    stats = await echo_store.collect(build_store)
    await collect_now.finish(f"收集完成：{stats}")


# ── speed leaderboard and operator usage ──────────────────────────────


async def _member_pool(bot: Bot, event: MessageEvent) -> tuple[set[str], dict[str, str]]:
    """Group member QQs (or just the sender in private chat) and their display names."""
    if isinstance(event, GroupMessageEvent):
        members = await bot.get_group_member_list(group_id=event.group_id)
        names = {str(m["user_id"]): m.get("card") or m.get("nickname") or str(m["user_id"]) for m in members}
    else:
        names = {str(event.user_id): event.sender.nickname or str(event.user_id)}
    return set(names) - echo_store.optout(), names


def _fmt_time(seconds: int) -> str:
    return f"{seconds // 60}分{seconds % 60:02d}秒" if seconds >= 60 else f"{seconds}秒"


def _top_difficulty(group: dict) -> str:
    return "残酷" if group["mode"] == "战争回响" else "苦难"


async def _icon(char_id: str) -> str:
    return await wiki._cached_image(wiki_data.char_icon_url(char_id), f"chr_{char_id}.png")


async def _team_icons(team: list[dict], operators: dict) -> str:
    cells = []
    for c in team:
        src = await _icon(c["id"]) if c["id"] in operators else ""
        img = f'<img src="{escape(src)}">' if src else '<i class="ph"></i>'
        cells.append(f'<span class="mini" title="{escape(operators.get(c["id"], {}).get("name", ""))}">{img}<em>{c["lv"]}·{c["pot"]}</em></span>')
    return "".join(cells)


BOARD_CSS = """
body { width: %dpx; padding: 26px; }
td.rk .ef-rank { line-height: 24px; }
table { width: 100%%; border-collapse: collapse; }
td { padding: 8px 10px; border-top: 1px solid var(--ef-line-2); font-size: 18px; vertical-align: middle; }
tr:first-child td { border-top: 0; }
tr.me td { background: rgba(255,225,0,.22); }
tr.me td:first-child { box-shadow: inset 5px 0 0 var(--ef-yellow); }
td.rk { width: 54px; text-align: center; }
td.nm { width: 220px; max-width: 220px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
td.tm { width: 130px; font-family: var(--ef-num); font-weight: 700; font-size: 24px; letter-spacing: .3px; white-space: nowrap; line-height: 1.15; }
td.tm small { display: block; font-size: 13px; color: var(--ef-sub); font-weight: 500; letter-spacing: .5px; }
.mini { display: inline-block; text-align: center; margin-right: 6px; vertical-align: top; }
.mini img, .mini .ph { display: block; width: 46px; height: 46px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); border-bottom: 2px solid var(--ef-ink); }
.mini em { font-style: normal; font-family: var(--ef-num); font-weight: 500; font-size: 13px; color: var(--ef-sub); }
.bar { height: 14px; background: var(--ef-ink); }
tr:first-child .bar { background: var(--ef-yellow); box-shadow: inset 0 0 0 1px var(--ef-ink); }
td.ic { width: 58px; } td.ic img { width: 42px; height: 42px; object-fit: contain; display: block; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
td.pc { width: 100px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 22px; white-space: nowrap; }
td.ct { width: 84px; text-align: right; color: var(--ef-sub); font-size: 15px; white-space: nowrap; }
td.pt { width: 92px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 22px; white-space: nowrap; }
tr.hd td { padding: 6px 10px; font-size: 13px; color: var(--ef-sub); border-top: 0; text-align: right; font-family: inherit; }
tr.hd + tr td { border-top: 1px solid var(--ef-line-2); }
table.stats { table-layout: fixed; }
table.stats td.rk, table.stats td.ic, table.stats td.nm, table.stats td.pc, table.stats td.pt { width: auto; }
table.stats td.nm { max-width: none; }
table.stats td.g { border-left: 1px solid var(--ef-line); }
table.stats td.bw { padding-right: 4px; }
table.stats .bar.own { background: var(--ef-sub); box-shadow: none; }
table.stats td.pt { text-align: center; }
table.stats td.pc small { display: block; font-size: 13px; color: var(--ef-sub); font-weight: 500; font-family: inherit; }
table.stats tr.hd td { text-align: center; font-size: 14px; font-weight: 700; color: var(--ef-ink); padding: 8px 10px; border-bottom: 2px solid var(--ef-ink); }
table.stats tr.hd td.g { border-left: 1px solid var(--ef-line); }
"""

BOARD_WIDTH = 1000
ENDMIN_IDS = {"chr_0003_endminf": "chr_0002_endminm", "chr_9000_endmin": "chr_0002_endminm"}
BOARD_FOOT = "用时与阵容来自森空岛最佳记录，精确到秒；阵容数字为 等级·潜能。数据每天 05:30、17:30 起陆续更新<br>不想上榜可发 /攻略统计 退出"


def _rank(n: int) -> str:
    return f'<span class="ef-rank{f" r{n}" if n <= 3 else ""}">{n}</span>'


async def _board_page(title: str, subtitle: str, body: str, foot: str = BOARD_FOOT, kicker: str = "Endfield · Speed Ranking") -> bytes:
    html = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{BOARD_CSS % BOARD_WIDTH}</style></head><body class="ef">'
        + ef_theme.head(kicker, title, subtitle)
        + body
        + ef_theme.foot(foot)
        + "</body></html>"
    )
    return await ef_theme.render_page(html, BOARD_WIDTH, height=600, template_path=wiki.DATA_DIR.as_uri())


async def _speed_card(data, pool, names, group, difficulty, requester, limit) -> str:
    operators = wiki._resolver()[0]["operators"]
    ranking = speed_ranking(data, pool, f"{group['name']}|{difficulty}")
    number = f"第{group['no']}关 · " if group.get("no") else ""
    header = f'<h3>{number}{escape(group["name"])} · {difficulty}<small class="cjk">{len(ranking)} 人有用时记录</small></h3>'
    if not ranking:
        return f'<div class="ef-sec">{header}<div class="ef-empty">本群还没有这关{difficulty}的用时记录</div></div>'
    shown = ranking[:limit]
    me = next((i for i, (qq, _) in enumerate(ranking, 1) if qq == requester), None)
    if me and me > limit:
        shown = shown + [ranking[me - 1]]
    rows = []
    for qq, rec in shown:
        rank = next(i for i, (q, _) in enumerate(ranking, 1) if q == qq)
        when = datetime.fromtimestamp(rec["at"], CN).strftime("%m/%d") if rec["at"] else ""
        rows.append(
            f'<tr class="{"me" if qq == requester else ""}"><td class="rk">{_rank(rank)}</td><td class="nm">{escape(names.get(qq, qq))}</td>'
            f'<td class="tm">{_fmt_time(rec["time"])}<small>{when}{" · 增辉" if rec.get("plus") else ""}</small></td>'
            f"<td>{await _team_icons(rec['team'], operators)}</td></tr>"
        )
    return f'<div class="ef-sec">{header}<table>{"".join(rows)}</table></div>'


SPEED_MENU = (
    "竞速榜 · 请选择要查询的榜单\n"
    "\n① 回响竞速（战争回响 · 残酷）\n"
    "　/回响竞速 → 本周全部关卡\n"
    "　/回响竞速 关卡名 → 单关完整榜\n"
    "\n② 丰碑竞速（影拓丰碑 · 苦难）\n"
    "　/丰碑竞速 → 选择要查的丰碑\n"
    "\n关卡名记不清？本周关卡发 /战争回响，丰碑关卡发 /影拓丰碑"
)


def _parse_board_args(arg: Message) -> tuple[str, str | None]:
    words = arg.extract_plain_text().split()
    difficulty = next((DIFFICULTY_WORDS[w] for w in words if w in DIFFICULTY_WORDS), None)
    return "".join(w for w in words if w not in DIFFICULTY_WORDS), difficulty


def _monument_menu(endgame: dict) -> str:
    lines = ["丰碑竞速 · 请选择要查的丰碑", ""]
    for series in endgame.get("monument", []):
        lines.append(f"【{series['name']}】{_numbered(series)}")
        lines.append(f"　→ /丰碑竞速 {series['name']}")
    lines += ["", "只看单关完整榜：/丰碑竞速 关卡名"]
    return "\n".join(lines)


async def _send_speed(bot: Bot, event: MessageEvent, matcher, groups: list[dict], difficulty: str | None, title: str, subtitle: str) -> None:
    await _react(REACTION_PROCESSING)
    try:
        pool, names = await _member_pool(bot, event)
        data = _load_echoes()
        requester = str(event.user_id)
        limit = 20 if len(groups) == 1 else 5
        cards = []
        for g in groups:
            diff = difficulty if difficulty and any(s["difficulty"] == difficulty for s in g["stages"]) else _top_difficulty(g)
            cards.append(await _speed_card(data, pool, names, g, diff, requester, limit))
        image = await _board_page(title, subtitle, "".join(cards))
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Speed board failed: {type(e).__name__}: {e}")
        await _react(REACTION_FAIL)
        await matcher.finish("竞速榜生成失败，请稍后再试")
    await _react(REACTION_DONE)


def _single_title(group: dict) -> tuple[str, str]:
    kind = "回响竞速" if group["mode"] == "战争回响" else "丰碑竞速"
    series = f"【{escape(group['series'])}】" if group.get("series") else ""
    return f"{kind} · <em>{escape(group['name'])}</em>", f"{series}本群群友最快通关用时排名"


@speed_board.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    query, difficulty = _parse_board_args(arg)
    if not query:
        await speed_board.finish(SPEED_MENU)
    if query in ("回响", "战争回响", "回响竞速"):
        await speed_board.finish("请发 /回响竞速（本周全部关卡）或 /回响竞速 关卡名")
    if query in ("丰碑", "影拓丰碑", "丰碑竞速"):
        await speed_board.finish(_monument_menu(_endgame()))
    query = query.removesuffix("丰碑") or query
    found = _resolve(_endgame(), query)
    if found and found[0] == "group":
        await _send_speed(bot, event, speed_board, [found[1]], difficulty, *_single_title(found[1]))
    elif found and found[0] == "series":
        await _send_monument_series(bot, event, speed_board, found[1], difficulty)
    else:
        hint = f"找到多个关卡：{'、'.join(found[1])}" if found else f"没有找到「{query}」"
        await speed_board.finish(hint + "\n\n" + SPEED_MENU)


@echo_speed.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    query, difficulty = _parse_board_args(arg)
    endgame = _endgame()
    if not query:
        current = _current_week(endgame)
        if not current:
            await echo_speed.finish("当前没有开放中的战争回响轮换，请发 /回响竞速 关卡名")
        groups = [endgame["tower"][g] for g in current[1]["groups"] if g in endgame["tower"]]
        await _send_speed(
            bot, event, echo_speed, groups, difficulty,
            "回响竞速 · <em>本周战争回响</em>", "本群群友最快通关用时（每关前 5 名，单关完整榜发 /回响竞速 关卡名）",
        )
        return
    found = _resolve(endgame, query)
    if found and found[0] == "group" and found[1]["mode"] == "战争回响":
        await _send_speed(bot, event, echo_speed, [found[1]], difficulty, *_single_title(found[1]))
    elif found and found[0] in ("group", "series"):
        await echo_speed.finish(f"「{found[1]['name']}」是影拓丰碑，请发 /丰碑竞速 {found[1]['name']}")
    else:
        hint = f"找到多个关卡：{'、'.join(found[1])}" if found else f"没有找到战争回响关卡「{query}」"
        await echo_speed.finish(hint + "\n本周关卡发 /战争回响；用法：/回响竞速 关卡名")


async def _send_monument_series(bot: Bot, event: MessageEvent, matcher, series: dict, difficulty: str | None) -> None:
    await _send_speed(
        bot, event, matcher, series["groups"], difficulty,
        f"丰碑竞速 · <em>{escape(series['name'])}</em>", f"本群群友最快通关用时（每关前 5 名，单关完整榜发 /丰碑竞速 关卡名）",
    )


@monument_speed.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    query, difficulty = _parse_board_args(arg)
    endgame = _endgame()
    query = query.removesuffix("丰碑")
    if not query:
        await monument_speed.finish(_monument_menu(endgame))
    found = _resolve(endgame, query)
    if found and found[0] == "series":
        await _send_monument_series(bot, event, monument_speed, found[1], difficulty)
    elif found and found[0] == "group" and found[1]["mode"] != "战争回响":
        await _send_speed(bot, event, monument_speed, [found[1]], difficulty, *_single_title(found[1]))
    elif found and found[0] == "group":
        await monument_speed.finish(f"「{found[1]['name']}」是战争回响关卡，请发 /回响竞速 {found[1]['name']}")
    else:
        hint = f"找到多个：{'、'.join(found[1])}" if found else f"没有找到丰碑「{query}」"
        await monument_speed.finish(hint + "\n\n" + _monument_menu(endgame))


STATS_RARITY = 6  # the owner's choice: lower rarities say little
STATS_LEFT_OUT = {"管理员"}  # everyone starts with the Endministrator: it says nothing about pulls
STATS_USAGE = "用法：/干员统计 [关卡名 | 战争回响 | 影拓丰碑]"
STATS_FOOT = (
    f"持有率 = 持有该干员的角色数 ÷ 已统计角色数；绑定的所有森空岛账号和角色都计入，一人多号算多个，权限等级 {HOLDING_MIN_LEVEL} 级起；平均潜能只算持有者，范围 0–5<br>"
    "出场率 = 带该干员的通关记录数 ÷ 通关记录总数，每条记录为一位玩家在一关的最快编队；可加 关卡名 / 战争回响 / 影拓丰碑 只看这部分关卡；出场率的条以第一名为满格<br>"
    "只代表绑定了机器人的玩家；持有率每天 03:00 更新，出场率每天 05:30、17:30 起陆续更新；不想参与可发送 /攻略统计 退出"
)


def _usage_scope(endgame: dict, query: str) -> tuple[set[str], str] | None:
    """(stage keys, label) for the clears the usage column counts; None when the query names nothing."""
    if not query:
        return {f"{g['name']}|{_top_difficulty(g)}" for g in _stage_groups(endgame)}, "全部关卡（战争回响残酷、影拓丰碑苦难）"
    if query in ("战争回响", "本周"):
        current = _current_week(endgame)
        return ({f"{endgame['tower'][g]['name']}|残酷" for g in current[1]["groups"]} if current else set()), "本周战争回响（残酷）"
    if query == "影拓丰碑":
        return {f"{g['name']}|苦难" for s in endgame.get("monument", []) for g in s["groups"]}, "影拓丰碑（苦难）"
    found = _resolve(endgame, query)
    if not found or found[0] != "group":
        return None
    g = found[1]
    return {f"{g['name']}|{_top_difficulty(g)}"}, f"{g['name']}（{_top_difficulty(g)}）"


async def _stats_body(roles: int, held: dict[str, tuple[int, float]], clears: int, used: Counter, scope: str, players: int) -> str:
    """6-star operators, most used first, then most owned. One nobody owns or uses is left out: it may not be released yet."""
    by_name: dict[str, str] = {}
    for char_id, operator in sorted(wiki._resolver()[0]["operators"].items()):
        if operator["rarity"] == STATS_RARITY and operator["name"] not in STATS_LEFT_OUT:
            by_name.setdefault(operator["name"], char_id)  # alternative ids of one operator share one row
    rows = []
    for name, char_id in by_name.items():
        owners, potential = held.get(name, (0, 0.0))
        uses = used.get(name, 0)
        if owners or uses:
            rows.append((name, char_id, owners, potential, uses))
    rows.sort(key=lambda row: (-row[4], -row[2], -row[3], row[0]))
    top = max((row[4] for row in rows), default=0)  # usage bars run to the most used operator: a clear has four, so no one gets near 100%
    cols = '<colgroup><col style="width:6%"><col style="width:7%"><col style="width:14%"><col style="width:21%"><col style="width:10%"><col style="width:10%"><col style="width:20%"><col style="width:12%"></colgroup>'
    cells = ['<tr class="hd"><td colspan="3">干员</td><td colspan="2" class="g">持有率</td><td class="g">平均潜能</td><td colspan="2" class="g">出场率</td></tr>']
    for i, (name, char_id, owners, potential, uses) in enumerate(rows, 1):
        src = await _icon(char_id)
        own = owners / roles * 100 if roles else 0.0
        use = uses / clears * 100 if clears else 0.0
        cells.append(
            f'<tr><td class="rk">{_rank(i)}</td><td class="ic">{f"<img src={chr(34)}{escape(src)}{chr(34)}>" if src else ""}</td><td class="nm">{escape(name)}</td>'
            f'<td class="bw g"><div class="bar own" style="width:{max(1, own):.0f}%"></div></td><td class="pc">{f"{own:.1f}%" if roles else "—"}</td>'
            f'<td class="pt g">{f"{potential:.2f}" if owners else "—"}</td>'
            f'<td class="bw g"><div class="bar" style="width:{max(1, uses / top * 100) if top else 1:.0f}%"></div></td>'
            f'<td class="pc">{f"{use:.1f}%" if clears else "—"}<small>{f"{uses} 次" if clears else ""}</small></td></tr>'
        )
    note = f"持有率 {roles} 个角色 · 出场率 {escape(scope)} {players} 位玩家 {clears} 条记录"
    return f'<div class="ef-sec"><h3>{STATS_RARITY}★ 干员<small class="cjk">{note}</small></h3><table class="stats">{cols}{"".join(cells)}</table></div>'


@operator_stats.handle()
async def _(arg: Message = CommandArg()) -> None:
    endgame = _endgame()
    scope = _usage_scope(endgame, arg.extract_plain_text().strip())
    if scope is None:
        await operator_stats.finish(f"没有找到「{arg.extract_plain_text().strip()}」\n{STATS_USAGE}")
    keys, label = scope
    await _react(REACTION_PROCESSING)
    try:
        optout = echo_store.optout()
        roles, held = build_store.holdings(optout)
        data = _load_echoes()
        pool = set(data.get("members", {})) - optout  # every bound player, not just this group
        clears, raw = usage_rates(data, pool, keys)
        players = sum(1 for qq, m in data.get("members", {}).items() if qq in pool and any(k in keys for k in m.get("records", {})))
        operators = wiki._resolver()[0]["operators"]
        used = Counter()
        for cid, n in raw.items():
            used[operators.get(ENDMIN_IDS.get(cid, cid), {}).get("name", cid)] += n
        if not roles and not clears:
            await _react(REACTION_DONE)
            await operator_stats.finish("干员统计数据还在收集，请稍后再试")
        body = await _stats_body(roles, held, clears, used, label, players)
        image = await _board_page("终末地 · <em>6★ 干员统计</em>", "全部绑定玩家（不分群）的持有率、持有者平均潜能和高难关卡出场率", body, STATS_FOOT, "Endfield · Operator Stats")
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Operator stats failed: {type(e).__name__}: {e}")
        await _react(REACTION_FAIL)
        await operator_stats.finish("干员统计生成失败，请稍后再试")
    await _react(REACTION_DONE)


# ── scheduled collection ───────────────────────────────────────────────


@scheduler.scheduled_job("cron", hour="5,17", minute=30, timezone="Asia/Shanghai", id="endfield_guide_collect")
async def _collect() -> None:
    """Clears twice a day; operators' equipment has its own night run (here only when there is none yet, or it is outdated)."""
    started = time.time()
    with_builds = not build_store.current()
    try:
        stats = await echo_store.collect(build_store if with_builds else None)
        logger.info(f"War Echoes clears collected in {time.time() - started:.0f}s: {stats}")
    except Exception as e:
        logger.warning(f"War Echoes collection failed: {type(e).__name__}")
    await _warm_covers()


@scheduler.scheduled_job("cron", hour=3, minute=0, timezone="Asia/Shanghai", id="endfield_guide_builds")
async def _collect_builds() -> None:
    """Operators' equipment once a night, apart from the clears so the 05:30 boards are not held up by it."""
    started = time.time()
    try:
        stats = await echo_store.collect(build_store, clears=False)
        logger.info(f"Operator builds collected in {time.time() - started:.0f}s: {stats}")
    except Exception as e:
        logger.warning(f"Operator builds collection failed: {type(e).__name__}")


async def _warm_covers() -> None:
    """Fetch covers that are not cached yet, one at a time (a stage cover is a 20 MB download)."""
    endgame = _endgame()
    fetched = 0
    for url, name, width in _cover_jobs(endgame.get("monument", []), _stage_groups(endgame)):
        if task := covers.start(url, name, width):
            try:
                await task
                fetched += 1
            except Exception as e:
                logger.warning(f"Stage cover unavailable: {type(e).__name__}")
    if fetched:
        logger.info(f"Stage covers fetched: {fetched}")


_background: set[asyncio.Task] = set()


@get_driver().on_startup
async def _first_collect() -> None:
    """Collect shortly after startup when there is no data yet or it was extracted by older rules.

    Existing (older) data keeps serving the boards until the new collection finishes.
    Otherwise only the stage covers that are still missing are fetched.
    """
    current = echo_store.data_file.exists() and echo_store.load().get("schema") == ECHO_SCHEMA and build_store.current()

    async def run() -> None:
        await asyncio.sleep(120)
        await (_warm_covers() if current else _collect())  # a collection ends with the cover warm-up itself

    task = asyncio.get_running_loop().create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)
