"""Endfield endgame stage guides: 战争回响 and 影拓丰碑.

/攻略 <关卡名> [难度]     stage card: mechanics, enemies, 增辉 target, group clear teams, Bilibili videos
/战争回响                 this week's rotation: stages in game order with their covers
/影拓丰碑 [丰碑名]         every series with its cover; one series: its stages in game order with covers
/敌人 <关卡|丰碑> [难度]   mechanics and enemy attributes, no teams or videos (/回响敌人: this week's rotation, /丰碑敌人: the current series)
/攻略统计 退出|加入        opt out of / back into the group clear-team statistics
/竞速榜                   menu → /回响竞速 [关卡]  /丰碑竞速 [丰碑|关卡]

Stage data comes from the endfield_wiki AKEData index; clear teams from members'
Skland War Echoes records (collected twice a day); videos from Bilibili search.
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
from .bili import BiliSearch
from .builds import BuildStore
from .echoes import ECHO_SCHEMA, EchoStore, speed_ranking, team_stats, usage_rates

__plugin_meta__ = PluginMetadata(
    name="Endfield stage guide",
    description="战争回响 / 影拓丰碑 stage guides with group clear teams and Bilibili videos.",
    usage="/攻略 重伤之围 残酷  /战争回响  /影拓丰碑",
    type="application",
)

DATA_DIR = store.get_plugin_data_dir()
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
bili = BiliSearch(DATA_DIR / "videos.json")
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
BOOST = {"残酷": ("残酷", "增辉"), "苦难": ("苦难", "镀层"), "困难": ("困难",), "普通": ()}
STAGE_NUMBER = re.compile(r"^第?(\d{1,2})关?$")  # "2" / "第2关": a stage of a 影拓丰碑 series by its number
ROTATION_WORDS = ("战争回响", "回响", "本周", "本期")
MONUMENT_WORDS = ("影拓丰碑", "丰碑")
ENEMY_USAGE = (
    "只看关卡机制和敌人属性（不含通关阵容和视频）\n"
    "/敌人 关卡名 [难度]　例：/敌人 暗曜白霆、/敌人 重伤之围 残酷\n"
    "/敌人 丰碑名　整个丰碑的全部关卡，例：/敌人 幽影刻形\n"
    "/敌人 丰碑名 序号　例：/敌人 幽影刻形 2\n"
    "/回响敌人　本期战争回响的三个关卡\n"
    "/丰碑敌人　当前活动中的丰碑"
)

guide = on_command("攻略", rule=strict, priority=5, block=True)
rotation = on_command("战争回响", rule=strict, priority=5, block=True)
monument_list = on_command("影拓丰碑", rule=strict, priority=5, block=True)
enemy = on_command("敌人", aliases={"敌人属性", "敌人数值", "回响敌人", "丰碑敌人"}, rule=strict, priority=5, block=True)
stats_opt = on_command("攻略统计", aliases={"战绩统计"}, rule=strict, priority=5, block=True)
speed_board = on_command("竞速榜", rule=strict, priority=5, block=True)
echo_speed = on_command("回响竞速", aliases={"战争回响竞速"}, rule=strict, priority=5, block=True)
monument_speed = on_command("丰碑竞速", aliases={"影拓丰碑竞速"}, rule=strict, priority=5, block=True)
usage_board = on_command("榜单角色出场率", aliases={"角色出场率"}, rule=strict, priority=5, block=True)
collect_now = on_command("攻略数据更新", rule=strict, permission=SUPERUSER, priority=5, block=True)


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


def _endgame() -> dict:
    """The stage index, with 影拓丰碑 in the game's order (series cover and stage numbers attached)."""
    loaded = wiki._resolver()
    return stages.ordered(loaded[0].get("endgame", {}) if loaded else {}, echo_store.layout())


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


def _last_seen(endgame: dict, group_id: str) -> str:
    seen = ""
    for season in endgame.get("tower_seasons", []):
        for week in season["weeks"]:
            start = _parse_time(week["open"])
            if group_id in week["groups"] and start and start <= datetime.now(CN):
                seen = f"{season['name']}·{week['name'] or '轮换' + str(week['week'])}"
    return seen


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


async def _send_page(matcher, build, fallback: str) -> None:
    """Render and send a menu page; `build` returns its HTML. Falls back to the plain text."""
    await _react(REACTION_PROCESSING)
    try:
        image = await ef_theme.render_page(await build(), stages.WIDTH, height=300, template_path=wiki.DATA_DIR.as_uri())
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Stage menu failed: {type(e).__name__}: {e}")
        await _react(REACTION_FAIL)
        await matcher.finish(fallback)
    await _react(REACTION_DONE)


def _video_text(videos) -> str:
    return "\n".join(f"{i}. {v.title[:40]}\nhttps://www.bilibili.com/video/{v.bvid}" for i, v in enumerate(videos, 1))


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
    lines += ["", "发送 /攻略 关卡名 查看攻略，例如 /攻略 " + (names[0] if names else "重伤之围") + " 残酷", "只看机制和敌人属性发 /回响敌人"]

    async def build() -> str:
        art, icons = await asyncio.gather(covers.get(_cover_jobs([], groups)), _foe_icons(groups))
        return stages.rotation_html(ef_theme, season, week, groups, _upcoming(endgame), art, icons, datetime.now(CN), _version())

    await _send_page(rotation, build, "\n".join(lines))


def _monument_text(endgame: dict) -> str:
    lines = ["影拓丰碑（苦难模式全通即可为该丰碑的蚀刻章镀层；关卡按游戏内顺序）"]
    lines += [f"【{series['name']}】{_numbered(series)}" for series in endgame.get("monument", [])]
    if endgame.get("monument"):
        first = endgame["monument"][0]
        lines.append(f"\n发送 /攻略 关卡名 苦难 查看攻略，例如 /攻略 {first['groups'][0]['name']} 苦难\n只看机制和敌人属性发 /敌人 {first['name']}")
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
        await stats_opt.finish("用法：/攻略统计 退出  或  /攻略统计 加入\n（攻略图的通关阵容、竞速榜和角色出场率会使用已绑定群友的战争回响 / 影拓丰碑最佳记录）")
    echo_store.set_optout(str(event.user_id), action == "退出")
    await _react(REACTION_DONE)
    await stats_opt.finish("已退出统计，你的记录不会再被收集，也不会出现在竞速榜上" if action == "退出" else "已重新加入通关阵容统计，下次更新后生效", at_sender=True)


@collect_now.handle()
async def _() -> None:
    await collect_now.send("开始收集战争回响通关记录和干员配装，约需半小时…")
    stats = await echo_store.collect(build_store)
    await collect_now.finish(f"收集完成：{stats}")


@guide.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    query, difficulty, number = _words(arg)
    endgame = _endgame()
    if not query:
        await guide.finish("用法：/攻略 关卡名 [难度]\n例如 /攻略 重伤之围 残酷、/攻略 暗曜白霆 苦难、/攻略 幽影刻形 2\n本周关卡发 /战争回响，影拓丰碑关卡发 /影拓丰碑")
    found = _resolve(endgame, query)
    if not found:
        await guide.finish(f"没有找到「{query}」，发 /战争回响 或 /影拓丰碑 查看全部关卡名")
    kind, target = found
    if kind == "candidates":
        await guide.finish(f"找到多个关卡：{'、'.join(target)}\n请输入更完整的名字")
    if kind == "series" and number and 1 <= number <= len(target["groups"]):
        kind, target = "group", target["groups"][number - 1]
    if kind == "series":
        await guide.finish(f"影拓丰碑【{target['name']}】包含：{_numbered(target)}\n请发 /攻略 关卡名 苦难 或 /攻略 {target['name']} 序号 查看单关攻略")

    group = target
    is_tower = group["mode"] == "战争回响"
    difficulty = difficulty if difficulty and any(s["difficulty"] == difficulty for s in group["stages"]) else ("残酷" if is_tower else "苦难")
    stage = next(s for s in group["stages"] if s["difficulty"] == difficulty)

    await _react(REACTION_PROCESSING)
    try:
        videos, stale = await bili.videos(group["name"], BOOST.get(difficulty, ()))
        pool, _ = await _member_pool(bot, event)
        data = _load_echoes()
        clears = team_stats(data, pool, f"{group['name']}|{difficulty}")
        target_text = data.get("targets", {}).get(f"{group['name']}|{difficulty}", "")
        html = await _render(endgame, group, stage, difficulty, clears, target_text, videos, stale, isinstance(event, GroupMessageEvent))
        image = await ef_theme.render_page(html, WIDTH, template_path=wiki.DATA_DIR.as_uri())
        await UniMessage.image(raw=image).send()
        if videos:
            await guide.send("推荐视频（点击链接观看）\n" + _video_text(videos))
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Stage guide failed: {type(e).__name__}: {e}")
        await _react(REACTION_FAIL)
        await guide.finish("攻略生成失败，请稍后再试")
    await _react(REACTION_DONE)


# ── rendering ──────────────────────────────────────────────────────────


async def _enemy_table(enemies: list[dict]) -> str:
    if not enemies or "hp" not in enemies[0]:  # index built before enemy stats were added
        items = "".join(f'<li>{escape(e["name"])}<small>Lv{e["level"]}</small></li>' for e in enemies)
        return _box("敌人", f"<ul>{items}</ul>")
    same_def = len({e.get("def") for e in enemies}) == 1
    poise = all("poise" in e for e in enemies)  # index built before the bonus lines were added
    icons = await asyncio.gather(
        *(wiki._cached_image(wiki_data.enemy_icon_url(e["icon"]), f"{e['icon']}.png") if e.get("icon") else asyncio.sleep(0, "") for e in enemies)
    )
    rows = []
    for e, icon in zip(enemies, icons):
        thumb = f'<img src="{escape(icon)}">' if icon else '<i class="ph"></i>'
        notes = stages.notes_html(e)
        rows.append(
            f'<tr><td class="ei"{" rowspan=2" if notes else ""}>{thumb}</td><td class="fe">{escape(e["name"])}{" *" if e.get("plain") else ""}</td>'
            f'<td class="lv">Lv{e["level"]}</td><td class="num">{e.get("hp", 0):,}</td>'
            f'<td class="num">{e.get("atk", 0):,}</td>' + ("" if same_def else f'<td class="num">{e.get("def", 0):,}</td>')
            + (f'<td class="num">{e["poise"]:,}</td>' if poise else "")
            + f'<td class="rs">{stages.res_html(e)}</td></tr>'
            + (f'<tr class="bn"><td colspan="{(4 if same_def else 5) + poise + 1}">{notes}</td></tr>' if notes else "")
        )
    head = (
        "<th></th><th>敌人</th><th>等级</th><th class=\"num\">生命值</th><th class=\"num\">攻击力</th>"
        + ("" if same_def else "<th class=\"num\">防御力</th>")
        + ("<th class=\"num\">失衡值上限</th>" if poise else "")
        + "<th>抗性</th>"
    )
    note = f"防御力均为 {enemies[0].get('def', 0)}；" if same_def else ""
    plain = "；带 * 的敌人不在关卡的固定刷怪配置中（多为战斗中召唤），按其自身属性显示" if any(e.get("plain") for e in enemies) else ""
    return _box(
        "敌人数值",
        f'<table class="foe"><tr>{head}</tr>{"".join(rows)}</table>'
        f'<div class="note">{note}数值为敌人在本关卡内的最终属性，已包含每行下方列出的出生加成（该敌人自带）和关卡加成，未计入上方的特殊增益；抗性越高，受到该类伤害越少{plain}</div>',
        f"{len(enemies)} 种",
    )


GUIDE_CSS = """
body { width: %dpx; padding: 24px; }
.ef-head h1 { font-size: 46px; }
.ef-head p { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; color: #b9b9b2; }
.ef-head p span { font-size: 15px; padding: 3px 12px; border: 1px solid #55554f; color: #b9b9b2; }
.ef-head p span i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 17px; letter-spacing: .3px; }
.ef-head p span.on { background: var(--ef-yellow); border-color: var(--ef-yellow); color: var(--ef-ink); font-weight: 700; }
.ef-head p span.live { border-color: var(--ef-yellow); color: var(--ef-yellow); }
.ef-head p span.past { border: 0; padding-left: 4px; color: #8d8d86; }
.bd { padding: 12px 16px 14px; }
.desc { font-size: 16px; line-height: 1.8; }
.dim { color: var(--ef-faint); }
.up { font-family: var(--ef-num); font-weight: 600; font-size: 17px; padding: 0 4px; margin: 0 1px; background: var(--ef-ink); color: var(--ef-yellow); }
.down { font-weight: 700; color: var(--ef-red); }
.key { font-weight: 700; color: var(--ef-ink); box-shadow: inset 0 -.45em 0 var(--ef-yellow); }
.term { font-weight: 700; color: var(--ef-ink); border-bottom: 1px dashed var(--ef-ink); }
.info { color: var(--ef-sub); }
ul { list-style: none; display: flex; flex-wrap: wrap; gap: 8px; }
li { background: #fff; border: 1px solid var(--ef-line); padding: 4px 10px; font-size: 15px; }
li small { color: var(--ef-sub); margin-left: 6px; font-family: var(--ef-num); font-size: 15px; }
.note { font-size: 13px; color: var(--ef-sub); margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--ef-line-2); }
table { width: 100%%; border-collapse: collapse; font-size: 15px; }
th { text-align: left; padding: 4px 8px 6px; font-size: 13px; font-weight: 400; color: var(--ef-sub); border-bottom: 1px solid var(--ef-ink); white-space: nowrap; }
td { text-align: left; padding: 7px 8px; border-top: 1px solid var(--ef-line-2); white-space: nowrap; vertical-align: middle; }
tr:nth-child(2) td { border-top: 0; }
th.n, td.n, th.num, td.num { text-align: right; }
td.n, td.num, td.lv { font-family: var(--ef-num); font-weight: 600; font-size: 18px; letter-spacing: .3px; }
table.foe { font-size: 16px; } table.foe td.fe { font-weight: 700; }
table.foe td.ei { width: 66px; padding: 5px 8px 5px 0; }
table.foe td.ei img, table.foe td.ei .ph { display: block; width: 54px; height: 54px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
table.foe td.rs { white-space: normal; color: var(--ef-sub); font-size: 14px; }
table.foe td.rs b { font-family: var(--ef-num); font-weight: 700; font-size: 17px; color: var(--ef-ink); }
table.vid td.vt { white-space: normal; width: 60%%; line-height: 1.5; } table.vid td.au { color: var(--ef-sub); }
.team { display: flex; align-items: center; gap: 16px; padding: 12px 0; border-top: 1px solid var(--ef-line-2); }
.team:first-child { border-top: 0; padding-top: 4px; }
.tn { width: 190px; flex: none; font-weight: 900; font-size: 18px; }
.tn i { font-style: normal; display: inline-block; min-width: 26px; height: 26px; line-height: 26px; text-align: center; margin-left: 4px;
  font-family: var(--ef-num); font-weight: 700; font-size: 18px; background: var(--ef-ink); color: #f4f4f0; }
.team.t1 .tn i { background: var(--ef-yellow); color: var(--ef-ink); box-shadow: 0 0 0 1px var(--ef-ink); }
.team.t2 .tn i { background: var(--ef-silver); color: var(--ef-ink); box-shadow: 0 0 0 1px var(--ef-ink); }
.team.t3 .tn i { background: var(--ef-copper); color: #fff; box-shadow: 0 0 0 1px var(--ef-ink); }
.tn small { display: block; margin-top: 6px; font-size: 13px; color: var(--ef-sub); font-weight: 400; line-height: 1.5; }
.ops { display: flex; gap: 12px; }
.op { width: 120px; text-align: center; }
.op img, .op .ph { width: 74px; height: 74px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); border-bottom: 3px solid var(--ef-ink); margin: 0 auto; display: block; }
.op b { display: block; font-size: 15px; margin-top: 5px; }
.op small { font-family: var(--ef-num); font-weight: 500; font-size: 14px; color: var(--ef-sub); }
"""


def _box(title: str, body: str, small: str = "") -> str:
    """One light section panel (ef_theme .ef-sec) with padded content."""
    note = f'<small class="cjk">{small}</small>' if small else ""
    return f'<div class="ef-sec"><h3>{title}{note}</h3><div class="bd">{body}</div></div>'


async def _render(endgame, group, stage, difficulty, clears, target_text, videos, stale, in_group) -> str:
    index = wiki._resolver()[0]
    operators = index["operators"]
    is_tower = group["mode"] == "战争回响"
    tabs = "".join(f'<span class="{"on" if s["difficulty"] == difficulty else ""}">{s["difficulty"]} · <i>Lv{s["recommend_lv"]}</i></span>' for s in group["stages"])

    status = ""
    if is_tower:
        current = _current_week(endgame)
        if current and group["id"] in current[1]["groups"]:
            end = _parse_time(current[1]["close"])
            status = f'<span class="live">本周开放中 · <i>{end:%m/%d %H:%M}</i> 轮换</span>'
        elif seen := _last_seen(endgame, group["id"]):
            status = f'<span class="past">上次出现：{escape(seen)}</span>'
    else:
        number = f" · 第{group['no']}关" if group.get("no") else ""
        status = f'<span class="past">系列：{escape(group["series"])}{number}</span>'

    enemy_block = await _enemy_table(stage["enemies"])
    extra = ""
    if stage["special_buff"]:
        extra += _box("特殊增益", f'<div class="desc">{stage["special_buff"]}</div>')
    if target_text:
        extra += _box("增辉条件", f'<div class="desc">{escape(target_text)}</div>')
    elif is_tower and difficulty == "残酷":
        extra += _box("增辉条件", '<div class="desc dim">残酷难度完成附加目标（多为限时通关）即可增辉</div>')
    if not is_tower and difficulty == "苦难":
        extra += _box("镀层", '<div class="desc dim">通关该系列苦难模式的全部关卡后，即可为该系列蚀刻章镀层</div>')

    team_block = _box("本群通关阵容", '<div class="desc dim">暂无群友的通关记录（绑定森空岛后，数据每天 05:30、17:30 更新）</div>')
    if clears and clears["teams"]:
        rows = []
        for i, team in enumerate(clears["teams"], 1):
            cells = []
            for c in team["chars"]:
                op = operators.get(c["id"], {})
                icon = await wiki._cached_image(wiki_data.char_icon_url(c["id"]), f"chr_{c['id']}.png") if op else ""
                img = f'<img src="{escape(icon)}">' if icon else '<div class="ph"></div>'
                cells.append(f'<div class="op">{img}<b>{escape(op.get("name", c["id"]))}</b><small>Lv{c["lv"]} · 潜能{c["pot"]:g}</small></div>')
            plus = f" · 其中 {team['plus']} 人增辉" if difficulty == "残酷" else ""
            rows.append(f'<div class="team t{i}"><div class="tn">阵容 <i>{i}</i><small>{team["count"]} 人使用{plus}</small></div><div class="ops">{"".join(cells)}</div></div>')
        scope = "本群" if in_group else "你"
        team_block = _box(
            f"{scope}通关阵容",
            f'{"".join(rows)}<div class="note">等级 / 潜能为使用该阵容群友的平均值；数据每天 05:30、17:30 更新，不含武器与装备</div>',
            f'{clears["clears"]} 人通关{difficulty}' + (f'，{clears["plus"]} 人增辉' if difficulty == "残酷" else ""),
        )

    if videos:
        video_rows = "".join(
            f'<tr><td class="vt">{escape(v.title)}</td><td class="au">{escape(v.author)}</td><td class="n">{v.play:,}</td><td class="n">{escape(v.duration)}</td>'
            f'<td class="n">{datetime.fromtimestamp(v.pubdate, CN):%m/%d}</td></tr>'
            for v in videos
        )
        stale_note = "（B站暂时无法访问，显示的是上次结果）" if stale else ""
        video_block = _box(
            "推荐视频",
            f'<table class="vid"><tr><th>标题</th><th>UP主</th><th class="n">播放</th><th class="n">时长</th><th class="n">发布</th></tr>{video_rows}</table>',
            f"按热度与发布时间排序{stale_note}，链接见下一条消息",
        )
    else:
        video_block = _box("推荐视频", '<div class="desc dim">暂时没有找到标题含该关卡名的视频</div>')

    mechanics = _box(
        "关卡机制",
        '<div class="desc">' + (stage["feature"] or '<span class="dim">无特殊机制说明</span>') + "</div>",
        f'{escape(difficulty)} · 推荐等级 {stage["recommend_lv"]}',
    )
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{GUIDE_CSS % WIDTH}{stages.NOTES_CSS}</style></head><body class="ef">'
        + ef_theme.head(f'Arknights: Endfield · {escape(group["mode"])}', escape(group["name"]), f"{tabs}{status}")
        + f"{mechanics}{enemy_block}{extra}{team_block}{video_block}"
        + ef_theme.foot(
            f'关卡数据 AKEData · 阵容 森空岛战绩 · 视频 哔哩哔哩<br>数据版本 <span class="ef-num">{escape(index.get("version", ""))}</span>',
            notes=stages.ENEMY_NOTES,
            title="数值怎么读",
        )
        + "</body></html>"
    )


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
"""

BOARD_WIDTH = 1000
ENDMIN_IDS = {"chr_0003_endminf": "chr_0002_endminm", "chr_9000_endmin": "chr_0002_endminm"}
BOARD_FOOT = "用时与阵容来自森空岛最佳记录，精确到秒；阵容数字为 等级·潜能。数据每天 05:30、17:30 更新<br>不想上榜可发 /攻略统计 退出"


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


@usage_board.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    query = arg.extract_plain_text().strip()
    endgame = _endgame()
    tops = {f"{g['name']}|{_top_difficulty(g)}" for g in _stage_groups(endgame)}
    if not query:
        keys, scope = tops, "全部关卡（战争回响残酷、影拓丰碑苦难）"
    elif query in ("战争回响", "本周"):
        current = _current_week(endgame)
        keys = {f"{endgame['tower'][g]['name']}|残酷" for g in current[1]["groups"]} if current else set()
        scope = "本周战争回响（残酷）"
    elif query == "影拓丰碑":
        keys = {f"{g['name']}|苦难" for s in endgame.get("monument", []) for g in s["groups"]}
        scope = "影拓丰碑（苦难）"
    else:
        found = _resolve(endgame, query)
        if not found or found[0] != "group":
            await usage_board.finish(f"没有找到「{query}」\n用法：/榜单角色出场率 [关卡名 | 战争回响 | 影拓丰碑]")
        g = found[1]
        keys, scope = {f"{g['name']}|{_top_difficulty(g)}"}, f"{g['name']}（{_top_difficulty(g)}）"

    await _react(REACTION_PROCESSING)
    try:
        data = _load_echoes()
        pool = set(data.get("members", {})) - echo_store.optout()  # every bound player, not just this group
        total, raw_counts = usage_rates(data, pool, keys)
        players = {qq for qq, m in data.get("members", {}).items() if any(keys is None or k in keys for k in m.get("records", {}))}
        counts = Counter()
        for cid, n in raw_counts.items():  # male/female Endministrator share one entry
            counts[ENDMIN_IDS.get(cid, cid)] += n
        operators = wiki._resolver()[0]["operators"]
        if not total:
            body = '<div class="ef-sec"><h3>出场率</h3><div class="ef-empty">还没有相关的通关记录</div></div>'
        else:
            top = counts.most_common(40)
            peak = top[0][1]
            rows = []
            for i, (cid, n) in enumerate(top, 1):
                src = await _icon(cid) if cid in operators else ""
                rows.append(
                    f'<tr><td class="rk">{_rank(i)}</td><td class="ic">{f"<img src={chr(34)}{escape(src)}{chr(34)}>" if src else ""}</td>'
                    f'<td class="nm">{escape(operators.get(cid, {}).get("name", cid))}</td>'
                    f'<td><div class="bar" style="width:{max(2, n / peak * 100):.0f}%"></div></td>'
                    f'<td class="pc">{n / total * 100:.1f}%</td><td class="ct">{n} 次</td></tr>'
                )
            body = f'<div class="ef-sec"><h3>{escape(scope)}<small class="cjk">{len(pool & players)} 位玩家 · 共 {total} 条通关记录</small></h3><table>{"".join(rows)}</table></div>'
        foot = "出场率 = 带该干员的通关记录数 ÷ 通关记录总数（每条记录为一位玩家在一关的最快编队）<br>数据每天 05:30、17:30 更新；不想参与统计可发 /攻略统计 退出"
        image = await _board_page("终末地 · <em>榜单角色出场率</em>", "全部绑定玩家（不分群）高难关卡最快编队中各干员的使用比例", body, foot, "Endfield · Usage Rate")
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Usage board failed: {type(e).__name__}: {e}")
        await _react(REACTION_FAIL)
        await usage_board.finish("出场率生成失败，请稍后再试")
    await _react(REACTION_DONE)


# ── scheduled collection ───────────────────────────────────────────────


@scheduler.scheduled_job("cron", hour="5,17", minute=30, timezone="Asia/Shanghai", id="endfield_guide_collect")
async def _collect() -> None:
    """Clears twice a day; operators' equipment only with the morning run (or when there is none yet, or it is outdated)."""
    started = time.time()
    with_builds = datetime.now(timezone(timedelta(hours=8))).hour < 12 or not build_store.current()
    try:
        stats = await echo_store.collect(build_store if with_builds else None)
        logger.info(f"War Echoes clears collected in {time.time() - started:.0f}s: {stats}")
    except Exception as e:
        logger.warning(f"War Echoes collection failed: {type(e).__name__}")
    await _warm_covers()


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
