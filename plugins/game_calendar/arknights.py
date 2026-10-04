"""Arknights activities and gacha pools for the calendar.

The game tables come from yuanyan3060/ArknightsGameResource (the repository nonebot-plugin-skland
already downloads its Arknights data from): activity_table.json for the activities and
gacha_table.json for the pools. The UP operators of a pool are only in PRTS's copy of the pool
details (weedy.prts.wiki), which is optional. Times in the tables are unix seconds.

The tables only know an activity once the client has been updated for it, usually on the opening
day. PRTS Wiki records activities as soon as they are announced, as structured data (Semantic
MediaWiki properties on the activity pages), so its API is asked for what is running or coming and
anything the tables do not have yet is added. Only the API is used; the wiki's pages sit behind a
bot check and are not fetched.

A small index is kept on disk: the game part is rebuilt when the repository's version file changes,
the wiki part about once an hour.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

RAW = "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource"
VERSION_URL = f"{RAW}/refs/heads/main/version"
ACTIVITY_URL = f"{RAW}/main/gamedata/excel/activity_table.json"
GACHA_URL = f"{RAW}/main/gamedata/excel/gacha_table.json"
POOL_DETAIL_URL = "https://weedy.prts.wiki/gacha_table.json"
WIKI_API = "https://prts.wiki/api.php"
WIKI_QUERY = "[[分类:有活动信息的页面]][[活动结束时间::>{since}]]|?名称|?名称缩短|?分类|?活动开始时间|?活动结束时间|sort=活动开始时间|order=asc|limit=50"
WIKI_INTERVAL = 3600
WIKI_ATTEMPTS = 4  # the wiki's CDN has nodes this host cannot reach; a new connection usually lands on another one
WIKI_TIMEOUT = httpx.Timeout(30, connect=8)
SAME_MOMENT = 300  # seconds: the two sources usually give the same activity the same times
SAME_DAY = 12 * 3600  # ... except on maintenance days: the tables say 07:00, the announcement (and the wiki) 12:00
# Wiki category keywords -> kind label.
WIKI_KINDS = (
    ("支线故事", "活动"), ("别传", "活动"), ("故事集", "故事集"), ("登录", "签到"), ("签到", "签到"), ("矢量突破", "矢量突破"),
    ("引航者", "引航者"), ("卫戍协议", "卫戍协议"), ("争锋频道", "争锋频道"), ("多维合作", "多人"), ("插曲", "主线"),
)
USER_AGENT = "QQBot-GameCalendar/1.0 (non-commercial QQ group bot; activity calendar)"
KEEP_ENDED = 30 * 86400  # activities that ended longer ago are not kept
MAX_AGE = 12 * 3600  # rebuild at least this often: pool details appear independently of the game version
PERMANENT = 180 * 86400  # pools running longer than this (归航寻访 ...) are not time-limited
GENERIC_POOL_NAME = "适合多种场合的强力干员"
# Activity type prefixes -> kind label.
KINDS = (
    ("CHECKIN", "签到"), ("LOGIN", "签到"), ("BLESS", "签到"), ("PRAY", "签到"), ("UNIQUE", "签到"),
    ("MINISTORY", "故事集"), ("TYPE_MAINSS", "主线"), ("TYPE_ACT", "活动"), ("VEC_BREAK", "矢量突破"),
    ("BOSS_RUSH", "引航者"), ("AUTOCHESS", "卫戍协议"), ("ENEMY_DUEL", "争锋频道"), ("MULTIPLAY", "多人"),
    ("ROGUE", "集成战略"), ("SANDBOX", "生息演算"), ("SWITCH", "合作"),
)
# Pool id prefixes -> pool kind; ids that match nothing are skipped (newbie and returner pools).
POOL_KINDS = (
    ("FESCLASSIC", "中坚甄选"), ("CLASSIC", "中坚寻访"), ("LIMITED", "限定寻访"), ("LINKAGE", "联动寻访"),
    ("SPECIAL", "定向甄选"), ("DOUBLE", "标准寻访"), ("SINGLE", "标准寻访"), ("NORM", "标准寻访"),
)


def _kind(activity_type: str, replicate: bool) -> str:
    label = next((label for prefix, label in KINDS if activity_type.startswith(prefix)), "")
    return f"{label}·复刻" if replicate and label == "活动" else label


def build(activity_table: dict, gacha_table: dict, pool_details: dict | None, names: dict[str, str], now: float) -> tuple[list[dict], list[dict]]:
    """(activities, pools) from the game tables: what is running, about to start, or ended recently."""
    activities = [
        {"id": row["id"], "name": row["name"], "kind": _kind(row.get("type", ""), bool(row.get("isReplicate"))), "start": row["startTime"], "end": row["endTime"]}
        for row in (activity_table.get("basicInfo") or {}).values()
        if row.get("name") and row.get("startTime") and row.get("endTime", 0) > now - KEEP_ENDED
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
        pools.append({"id": row["gachaPoolId"], "name": kind if not name or name == GENERIC_POOL_NAME else name, "kind": kind, "up": up.get(row["gachaPoolId"], []), "start": start, "end": end})
    return sorted(activities, key=lambda a: a["start"]), sorted(pools, key=lambda p: p["start"])


def parse_wiki(answer: dict) -> list[dict]:
    """Activities from a Semantic MediaWiki `ask` answer; the printout timestamps are unix seconds."""
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
        if end - start <= PERMANENT:  # 集成战略 / 生息演算 run for a year: modes, not limited-time activities
            activities.append({"name": name, "short": (printouts.get("名称缩短") or [name])[0], "kind": kind, "start": start, "end": end})
    return sorted(activities, key=lambda a: a["start"])


def _plain(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum()).lower()


def merge(game: list[dict], wiki: list[dict]) -> list[dict]:
    """Game activities plus the announced ones only the wiki has.

    The same activity is recognised by its name and an opening on the same day, or by identical
    opening and closing times. Where both sources have it, the wiki's opening time wins: it is the
    announced one, while the tables carry the data time of a maintenance day. The other source's
    names and opening time are kept as `aliases` / `alt_starts`, so an activity announced under one
    of them is not announced again under the other.
    """
    merged = [{**activity, "aliases": [], "alt_starts": []} for activity in game]
    for entry in wiki:
        names = {_plain(entry["name"]), _plain(entry["short"])}
        named = lambda a: any(n and (n in _plain(a["name"]) or _plain(a["name"]) in n) for n in names)
        match = next((a for a in merged if abs(a["start"] - entry["start"]) <= SAME_DAY and named(a)), None)
        match = match or next(
            (a for a in merged if abs(a["start"] - entry["start"]) <= SAME_MOMENT and abs(a["end"] - entry["end"]) <= SAME_MOMENT and not a["aliases"]), None
        )
        if match:
            match["aliases"] += [entry["name"], entry["short"]]
            if abs(match["start"] - entry["start"]) > SAME_MOMENT:
                match["alt_starts"].append(match["start"])
                match["start"] = entry["start"]
        else:
            merged.append({"id": "", "name": entry["name"], "kind": entry["kind"], "start": entry["start"], "end": entry["end"], "aliases": [entry["short"]], "alt_starts": []})
    return sorted(merged, key=lambda a: a["start"])


def load(cache_file: Path) -> dict:
    try:
        return json.loads(cache_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _operator_names(character_table: Path) -> dict[str, str]:
    """Operator id -> name from the table nonebot-plugin-skland keeps locally; empty when it is missing."""
    try:
        return {char_id: row.get("name", "") for char_id, row in json.loads(character_table.read_text("utf-8")).items()}
    except (OSError, ValueError):
        return {}


async def refresh(cache_file: Path, character_table: Path) -> str:
    """Bring the index up to date. Returns what happened ("unchanged" when nothing did)."""
    cached = load(cache_file)
    now, done = time.time(), []
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
                details = None  # the pools are still listed, without their UP operators
            activities, pools = build(tables[0], tables[1], details, _operator_names(character_table), now)
            cached.update(version=version, fetched=now, game_activities=activities, pools=pools)
            done.append(f"game data {version}: {len(activities)} activities, {len(pools)} pools")
        if now - (cached.get("wiki") or {}).get("fetched", 0) >= WIKI_INTERVAL:
            since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))
            failure = ""
            for _ in range(WIKI_ATTEMPTS):
                try:
                    async with httpx.AsyncClient(timeout=WIKI_TIMEOUT, headers={"User-Agent": USER_AGENT}) as wiki_client:
                        response = await wiki_client.get(WIKI_API, params={"action": "ask", "query": WIKI_QUERY.format(since=since), "format": "json"})
                    response.raise_for_status()
                    cached["wiki"] = {"fetched": now, "activities": parse_wiki(response.json())}
                    done.append(f"wiki: {len(cached['wiki']['activities'])} activities")
                    break
                except (httpx.HTTPError, ValueError) as e:
                    failure = type(e).__name__
            else:  # the previous answer keeps serving; asked again at the next refresh
                done.append(f"wiki unavailable ({failure})")
    if not done:
        return "unchanged"
    cached["activities"] = merge(cached.get("game_activities") or [], (cached.get("wiki") or {}).get("activities") or [])
    cache_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(cached, ensure_ascii=False), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(cache_file)
    return "; ".join(done)
