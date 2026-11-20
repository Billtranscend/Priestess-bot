"""日历用的明日方舟活动和卡池。

游戏数据表来自 yuanyan3060/ArknightsGameResource（nonebot-plugin-skland 本来就从这个仓库下载
明日方舟数据）：活动用 activity_table.json，卡池用 gacha_table.json。卡池的 UP 干员只在
PRTS 保存的卡池详情（weedy.prts.wiki）里，这部分可有可无。表里的时间是 unix 秒。

数据表要等客户端为活动更新后才有这个活动，通常是开放当天。PRTS Wiki 在活动一公布就以结构化数据
（活动页上的 Semantic MediaWiki 属性）收录，所以会调它的 API 查询正在进行和即将到来的活动，
把数据表里还没有的补上。只用 API；wiki 的页面在人机验证后面，不去抓取。

磁盘上保存一份小索引：仓库的版本文件变化时重建游戏部分，wiki 部分大约每小时更新一次。
和游戏部分一起生成的还有 /本周任务 用的每周内容（`weekly`）：周常任务（mission_table）、
剿灭作战轮换（campaign_table，关卡名取自 stage_table）和保全派驻赛季（climb_tower_table）。

持续一年的模式（集成战略、生息演算、卫戍协议·规程……）不显示，只在新开时显示：开放前一周和开放后
NEW_MODE 时间内。只凭持续时间识别，
所以以后再有这类模式也不用手动处理。
有些活动在 wiki 上没有自己的条目，也就没有横幅（幸运墙、特别招募……）。这些活动由
data/game_calendar/arknights_banners.json {"<游戏活动 id>": "<PRTS 文件名>"} 手动指定公告图，
这些图和 wiki 的图一起查询。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
from plugins.shared import write_json

RAW = "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource"
VERSION_URL = f"{RAW}/refs/heads/main/version"
ACTIVITY_URL = f"{RAW}/main/gamedata/excel/activity_table.json"
GACHA_URL = f"{RAW}/main/gamedata/excel/gacha_table.json"
POOL_DETAIL_URL = "https://weedy.prts.wiki/gacha_table.json"
WEEKLY_URLS = {name: f"{RAW}/main/gamedata/excel/{name}.json" for name in ("mission_table", "campaign_table", "climb_tower_table", "stage_table")}
WIKI_API = "https://prts.wiki/api.php"
WIKI_QUERY = "[[分类:有活动信息的页面]][[活动结束时间::>{since}]]|?名称|?名称缩短|?分类|?活动开始时间|?活动结束时间|?标题图文件名|sort=活动开始时间|order=asc|limit=50"
WIKI_POOL_QUERY = "[[分类:国服寻访]][[寻访关闭时间cn::>{since}]]|?寻访名cn|?寻访开启时间cn|?寻访关闭时间cn|sort=寻访开启时间cn|order=asc|limit=30"
BANNER_WIDTH = 960  # 向 wiki 请求的横幅缩略图宽度（像素）（原图 1560 x 500，卡池横幅 1650 x 900）
TITLES_PER_QUERY = 50  # MediaWiki 一次 query 最多查这么多个标题（普通账号的上限）
WIKI_INTERVAL = 3600
WIKI_ATTEMPTS = 4  # wiki 的 CDN 有些节点这台机器连不上；新建连接通常会落到别的节点
WIKI_TIMEOUT = httpx.Timeout(30, connect=8)
SAME_MOMENT = 300  # 秒：两个数据源给同一活动的时间通常相同
SAME_DAY = 12 * 3600  # ……维护日除外：数据表写 07:00，公告（和 wiki）写 12:00
# wiki 分类关键词 -> 类型标签。
WIKI_KINDS = (
    ("支线故事", "活动"), ("别传", "活动"), ("故事集", "故事集"), ("登录", "签到"), ("签到", "签到"), ("矢量突破", "矢量突破"),
    ("引航者", "引航者"), ("卫戍协议", "卫戍协议"), ("争锋频道", "争锋频道"), ("多维合作", "多人"), ("插曲", "主线"),
)
USER_AGENT = "QQBot-GameCalendar/1.0 (non-commercial QQ group bot; activity calendar)"
KEEP_ENDED = 30 * 86400  # 结束超过这么久的活动不保留
MAX_AGE = 12 * 3600  # 至少隔这么久重建一次：卡池详情的出现与游戏版本无关
PERMANENT = 180 * 86400  # 开放时间比这更长的卡池（归航寻访……）不算限时
NEW_MODE = 7 * 86400  # 持续一年的模式在开放后保留这么久（日历在开放前后一周内显示它）
GENERIC_POOL_NAME = "适合多种场合的强力干员"
# 活动类型前缀 -> 类型标签。
KINDS = (
    ("CHECKIN", "签到"), ("LOGIN", "签到"), ("BLESS", "签到"), ("PRAY", "签到"), ("UNIQUE", "签到"),
    ("MINISTORY", "故事集"), ("TYPE_MAINSS", "主线"), ("TYPE_ACT", "活动"), ("VEC_BREAK", "矢量突破"),
    ("BOSS_RUSH", "引航者"), ("AUTOCHESS", "卫戍协议"), ("ENEMY_DUEL", "争锋频道"), ("MULTIPLAY", "多人"),
    ("ROGUE", "集成战略"), ("SANDBOX", "生息演算"), ("SWITCH", "合作"),
)
# 卡池 id 前缀 -> 卡池类型；什么都没匹配上的 id 跳过（新手池和回归池）。
POOL_KINDS = (
    ("FESCLASSIC", "中坚甄选"), ("CLASSIC", "中坚寻访"), ("LIMITED", "限定寻访"), ("LINKAGE", "联动寻访"),
    ("SPECIAL", "定向甄选"), ("DOUBLE", "标准寻访"), ("SINGLE", "标准寻访"), ("NORM", "标准寻访"),
)


def _kind(activity_type: str, replicate: bool, activity_id: str = "") -> str:
    label = next((label for prefix, label in KINDS if activity_type.startswith(prefix)), "")
    if not label and "autochess" in activity_id:  # 卫戍协议的行类型是 DEFAULT
        label = "卫戍协议"
    return f"{label}·复刻" if replicate and label == "活动" else label


def _pool_note(row: dict, names: dict[str, str]) -> str:
    """没有固定 UP 的卡池在 dynMeta 里列出的 6 星干员（2026 年感谢庆典卡池，规则类型 12）。
    否则为空：这样的卡池不显示（中坚甄选和未公布的 UP 都不显示）。"""
    listed = [names.get(char_id, "") for char_id in (row.get("dynMeta") or {}).get("charIdList") or []]
    listed = [name for name in listed if name]
    return f"6★：{'、'.join(listed)}" if listed else ""


def build(activity_table: dict, gacha_table: dict, pool_details: dict | None, names: dict[str, str], now: float) -> tuple[list[dict], list[dict]]:
    """从游戏数据表得到 (活动, 卡池)：正在进行、即将开始或最近结束的。"""
    activities = [
        {"id": row["id"], "name": row["name"], "kind": _kind(row.get("type", ""), bool(row.get("isReplicate")), row["id"]), "start": row["startTime"], "end": row["endTime"]}
        for row in (activity_table.get("basicInfo") or {}).values()
        if row.get("name") and row.get("startTime") and row.get("endTime", 0) > now - KEEP_ENDED
        and (row["endTime"] - row["startTime"] <= PERMANENT or row["startTime"] >= now - NEW_MODE)
    ]
    up = {}
    for pool in (pool_details or {}).get("gachaPoolClient") or []:
        groups = (((pool.get("gachaPoolDetail") or {}).get("detailInfo") or {}).get("upCharInfo") or {}).get("perCharList") or []
        six = [names.get(char_id, "") for group in groups if group.get("rarityRank") == 5 for char_id in group.get("charIdList") or []]
        up[pool.get("gachaPoolId")] = [name for name in six if name]
    pools = []
    for row in gacha_table.get("gachaPoolClient") or []:
        kind = next((label for prefix, label in POOL_KINDS if str(row.get("gachaPoolId", "")).startswith(prefix)), "")
        start, end = row.get("openTime") or 0, row.get("endTime") or 0
        if not kind or not start or end <= now - KEEP_ENDED or end - start > PERMANENT:
            continue
        name = row.get("gachaPoolName") or ""
        pools.append({"id": row["gachaPoolId"], "name": kind if not name or name == GENERIC_POOL_NAME else name, "kind": kind, "up": up.get(row["gachaPoolId"], []),
                      "note": _pool_note(row, names), "start": start, "end": end})
    return sorted(activities, key=lambda a: a["start"]), sorted(pools, key=lambda p: p["start"])


def parse_wiki(answer: dict, now: float | None = None) -> list[dict]:
    """从 Semantic MediaWiki 的 `ask` 结果中取出活动；printout 里的时间戳是 unix 秒。"""
    activities = []
    for row in ((answer.get("query") or {}).get("results") or {}).values():
        printouts = row.get("printouts") or {}
        try:
            name = printouts["名称"][0]
            start, end = int(printouts["活动开始时间"][0]["timestamp"]), int(printouts["活动结束时间"][0]["timestamp"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        categories = "|".join(c.get("fulltext", "") for c in printouts.get("分类") or [])
        kind = next((label for keyword, label in WIKI_KINDS if keyword in categories), "")
        banner = (printouts.get("标题图文件名") or [""])[0]
        banner = banner if isinstance(banner, str) else banner.get("fulltext", "")
        # 集成战略 / 生息演算 / 卫戍协议·规程 持续一年：属于模式，只在新开时显示
        if end - start <= PERMANENT or (now is not None and start >= now - NEW_MODE):
            activities.append({"name": name, "short": (printouts.get("名称缩短") or [name])[0], "kind": kind, "start": start, "end": end, "banner_file": banner, "image": ""})
    return sorted(activities, key=lambda a: a["start"])


def banner_titles(activities: list[dict]) -> list[str]:
    return sorted({"文件:" + a["banner_file"].removeprefix("文件:") for a in activities if a.get("banner_file")})


def apply_banners(activities: list[dict], answer: dict) -> None:
    """用 `imageinfo` 结果里的缩略图 URL 填好每个活动的 `image`；这次结果里没有的不动（分批查询时留着上一批的）。"""
    urls = {}
    normalized = {n.get("to"): n.get("from") for n in (answer.get("query") or {}).get("normalized") or []}
    for page in (answer.get("query") or {}).get("pages") or []:
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url") or ""
        for title in (page.get("title"), normalized.get(page.get("title"))):
            if title and url:
                urls[title] = url
    for activity in activities:
        if activity.get("banner_file") and (url := urls.get("文件:" + activity["banner_file"].removeprefix("文件:"))):
            activity["image"] = url


async def fill_banners(client: httpx.AsyncClient, entries: list[dict]) -> None:
    """活动、卡池和手动指定的横幅一起查 `imageinfo`：通常一次请求，标题超过 API 上限时才分批。"""
    titles = banner_titles(entries)
    for start in range(0, len(titles), TITLES_PER_QUERY):
        response = await client.get(
            WIKI_API,
            params={"action": "query", "format": "json", "formatversion": 2, "titles": "|".join(titles[start:start + TITLES_PER_QUERY]),
                    "prop": "imageinfo", "iiprop": "url", "iiurlwidth": BANNER_WIDTH},
        )
        response.raise_for_status()
        apply_banners(entries, response.json())


def parse_wiki_pools(answer: dict) -> list[dict]:
    """从 `ask` 结果中取卡池横幅：每条结果就是横幅文件本身，附带卡池名和时间。"""
    pools = []
    for title, row in ((answer.get("query") or {}).get("results") or {}).items():
        printouts = row.get("printouts") or {}
        try:
            start, end = int(printouts["寻访开启时间cn"][0]["timestamp"]), int(printouts["寻访关闭时间cn"][0]["timestamp"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        pools.append({"name": (printouts.get("寻访名cn") or [""])[0], "start": start, "end": end, "banner_file": title, "image": ""})
    return pools


def pool_images(game_pools: list[dict], wiki_pools: list[dict]) -> dict[str, str]:
    """游戏卡池 id -> 横幅 URL。按开放时间配对；同时开放的几个卡池再按类型或名字区分。"""
    images = {}
    for pool in game_pools:
        together = [w for w in wiki_pools if abs(w["start"] - pool["start"]) <= SAME_MOMENT and w.get("image")]
        keyword = pool["kind"].removesuffix("寻访")
        match = next((w for w in together if keyword in w["name"] or w["name"] == pool["name"]), None) or (together[0] if len(together) == 1 else None)
        if match:
            images[pool["id"]] = match["image"]
    return images


def _plain(name: str) -> str:
    """用于比较的名字：只留文字，这样 感谢庆典2026限时签到活动（wiki）也能对上 感谢庆典限时签到（数据表）。"""
    return "".join(ch for ch in name if ch.isalnum() and not ch.isdigit()).lower()


def merge(game: list[dict], wiki: list[dict], extra_images: dict[str, str] | None = None) -> list[dict]:
    """游戏数据表里的活动，加上只有 wiki 收录的已公布活动。

    同一活动靠名字加同一天开放来识别，或者开放和结束时间完全相同。
    两边都有时以 wiki 的开放时间为准：那是公告的时间，而数据表里写的是维护日的数据时间。
    另一个数据源的名字和开放时间保存在 `aliases` / `alt_starts` 里，
    这样以其中一个名字通知过的活动，不会再以另一个名字重复通知。
    """
    merged = [{**activity, "aliases": [], "alt_starts": [], "image": ""} for activity in game]
    for entry in wiki:
        names = {_plain(entry["name"]), _plain(entry["short"])}
        named = lambda a: any(n and (n in _plain(a["name"]) or _plain(a["name"]) in n) for n in names)
        match = next((a for a in merged if abs(a["start"] - entry["start"]) <= SAME_DAY and named(a)), None)
        match = match or next(
            (a for a in merged if abs(a["start"] - entry["start"]) <= SAME_MOMENT and abs(a["end"] - entry["end"]) <= SAME_MOMENT and not a["aliases"]), None
        )
        if match:
            match["aliases"] += [entry["name"], entry["short"]]
            match["image"] = entry.get("image", "")
            if abs(match["start"] - entry["start"]) > SAME_MOMENT:
                match["alt_starts"].append(match["start"])
                match["start"] = entry["start"]
        else:
            merged.append(
                {"id": "", "name": entry["name"], "kind": entry["kind"], "start": entry["start"], "end": entry["end"], "aliases": [entry["short"]], "alt_starts": [], "image": entry.get("image", "")}
            )
    for activity in merged:  # 为 wiki 上没有条目的活动手动指定的横幅
        if not activity["image"] and activity.get("id") in (extra_images or {}):
            activity["image"] = extra_images[activity["id"]]
    return sorted(merged, key=lambda a: a["start"])


def build_weekly(missions: dict, campaigns: dict, towers: dict, stages: dict) -> dict:
    """按类型分组的周常任务（每类一行，含档位和分数）、剿灭作战轮换和保全派驻赛季。"""
    kinds: dict[str, dict] = {}
    for row in sorted((m for m in missions.get("missions", {}).values() if m.get("type") == "WEEKLY"), key=lambda m: m.get("sortId", 0)):
        text = row.get("description", "")
        kind = kinds.setdefault(re.sub(r"\d+", "{}", text, count=1), {"counts": [], "points": 0})
        if number := re.search(r"\d+", text):
            kind["counts"].append(int(number.group()))
        kind["points"] += row.get("periodicalPoint", 0)
    lines = []
    for template, kind in kinds.items():
        counts = sorted(kind["counts"])
        amount = f"{counts[0]}–{counts[-1]}" if len(counts) > 1 else str(counts[0]) if counts else ""
        lines.append({"desc": template.format(amount) if "{}" in template else template, "points": kind["points"]})
    now = time.time()
    rewards = [r for r in missions.get("weeklyRewards", {}).values() if r.get("beginTime", 0) <= now <= r.get("endTime", 0)]
    rotations = [
        {"stage": (stages.get(r.get("stageId", "")) or {}).get("name", ""), "open": r.get("startTs", 0), "close": r.get("endTs", 0)}
        for r in campaigns.get("campaignRotateStageOpenTimes", [])
    ]
    seasons = [{"name": s.get("name", ""), "open": s.get("startTs", 0), "close": s.get("endTs", 0)} for s in towers.get("seasonInfos", {}).values()]
    return {
        "missions": lines,
        "mission_count": sum(len(k["counts"]) or 1 for k in kinds.values()),
        "points": sum(line["points"] for line in lines),
        "goal": sum(r.get("periodicalPointCost", 0) for r in rewards),
        "annihilation": rotations,
        "sss": seasons,
    }


def load(cache_file: Path) -> dict:
    try:
        return json.loads(cache_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _operator_names(character_table: Path) -> dict[str, str]:
    """从 nonebot-plugin-skland 本地保存的表得到 干员 id -> 名字；表不存在时为空。"""
    try:
        return {char_id: row.get("name", "") for char_id, row in json.loads(character_table.read_text("utf-8")).items()}
    except (OSError, ValueError):
        return {}


def _hand_banners(cache_file: Path) -> dict[str, str]:
    """游戏活动 id -> PRTS 文件名，取自索引旁边的 arknights_banners.json。"""
    try:
        data = json.loads((cache_file.parent / "arknights_banners.json").read_text("utf-8"))
        return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, ValueError, AttributeError):
        return {}


async def refresh(cache_file: Path, character_table: Path) -> str:
    """把索引更新到最新。返回做了什么（什么都没做时为 "unchanged"）。"""
    cached = load(cache_file)
    now, done = time.time(), []
    hand = _hand_banners(cache_file)
    if hand != (cached.get("wiki") or {}).get("hand_banners", {}):
        cached.setdefault("wiki", {})["fetched"] = 0  # 列表有变化就立即查询
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20), follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(VERSION_URL)
        response.raise_for_status()
        version = response.text.strip()
        if cached.get("version") != version or "game_activities" not in cached or now - cached.get("fetched", 0) >= MAX_AGE:
            tables = []
            for url in (ACTIVITY_URL, GACHA_URL):
                response = await client.get(url)
                response.raise_for_status()
                tables.append(response.json())
            try:
                response = await client.get(POOL_DETAIL_URL)
                response.raise_for_status()
                details = response.json()
            except (httpx.HTTPError, ValueError):
                details = None  # 卡池照样列出，只是没有 UP 干员
            activities, pools = build(tables[0], tables[1], details, _operator_names(character_table), now)
            cached.update(version=version, fetched=now, game_activities=activities, pools=pools)
            done.append(f"game data {version}: {len(activities)} activities, {len(pools)} pools")
        if (cached.get("weekly") or {}).get("version") != version:  # stage_table 约 20 MB：每个游戏版本只下一次
            try:
                tables = {}
                for name, url in WEEKLY_URLS.items():
                    response = await client.get(url)
                    response.raise_for_status()
                    tables[name] = response.json()
                weekly = build_weekly(tables["mission_table"], tables["campaign_table"], tables["climb_tower_table"], tables["stage_table"].get("stages", {}))
                cached["weekly"] = {"version": version, **weekly}
                done.append(f"weekly: {len(weekly['missions'])} mission kinds")
            except (httpx.HTTPError, ValueError) as e:  # 保留旧的每周部分；下次再请求
                done.append(f"weekly unavailable ({type(e).__name__})")
        if now - (cached.get("wiki") or {}).get("fetched", 0) >= WIKI_INTERVAL:
            since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))
            failure = ""
            for _ in range(WIKI_ATTEMPTS):
                try:
                    async with httpx.AsyncClient(timeout=WIKI_TIMEOUT, headers={"User-Agent": USER_AGENT}) as wiki_client:
                        response = await wiki_client.get(WIKI_API, params={"action": "ask", "query": WIKI_QUERY.format(since=since), "format": "json"})
                        response.raise_for_status()
                        found = parse_wiki(response.json(), now)
                        response = await wiki_client.get(WIKI_API, params={"action": "ask", "query": WIKI_POOL_QUERY.format(since=since), "format": "json"})
                        response.raise_for_status()
                        found_pools = parse_wiki_pools(response.json())
                        extra = [{"id": key, "banner_file": name, "image": ""} for key, name in hand.items()]
                        await fill_banners(wiki_client, [*found, *found_pools, *extra])
                    cached["wiki"] = {"fetched": now, "activities": found, "pools": found_pools, "hand_banners": hand,
                                      "extra_images": {e["id"]: e["image"] for e in extra if e["image"]}}
                    done.append(f"wiki: {len(cached['wiki']['activities'])} activities")
                    break
                except (httpx.HTTPError, ValueError) as e:
                    failure = type(e).__name__
            else:  # 继续用上一次的结果；下次刷新时再请求
                done.append(f"wiki unavailable ({failure})")
    if not done:
        return "unchanged"
    cached["activities"] = merge(cached.get("game_activities") or [], (cached.get("wiki") or {}).get("activities") or [], (cached.get("wiki") or {}).get("extra_images"))
    cached["pool_images"] = pool_images(cached.get("pools") or [], (cached.get("wiki") or {}).get("pools") or [])
    cache_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(cache_file, cached, ensure_ascii=False)
    return "; ".join(done)
