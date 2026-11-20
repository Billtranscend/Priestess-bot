"""采集已绑定群友的战争回响和影拓丰碑最佳通关记录，供竞速榜、/干员统计 的出场率和资料卡上的常用队友使用。

每关每个难度只保存最佳记录的编队（干员 id、等级、潜能）和增辉标记，
按 QQ 存放。退出统计的群友不会被拉取，已存数据里也会删除。
采集时还可以顺带读取名片，为 builds.py 保存干员配装和持有名单。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlencode

import httpx
from nonebot import logger
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.exception import LoginException
from nonebot_plugin_skland.model import Character, SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_user.models import Bind
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from plugins.skland_roles import default_role, member_roles, owner_id_of

from .builds import CARD_URL, BuildStore, extract, role_roster
from plugins.shared import write_json

WAR_ECHOES_URL = "https://zonai.skland.com/api/v1/game/endfield/card/war-echoes"
INDIE_HARD_URL = "https://zonai.skland.com/api/v1/game/endfield/card/indie-hard"  # 影拓丰碑
DIFFICULTY_FIELDS = (("normalDungeon", "普通"), ("hardDungeon", "困难"), ("cruelDungeon", "残酷"))
REQUEST_GAP = 2.0  # 同一个 worker 两次请求之间的间隔
WORKERS = 5  # 同时拉取的群友数；逐个拉取约 10 秒一人（392 人 67 分钟），3 个 worker 420 人约 21 分钟且没有多出错误
SAVE_SECONDS = 300  # 采集中途隔多久保存一次通关记录：整份约 20 MB，每写一次要卡主线程约 0.3 秒
COMMIT_TRIES = 4  # 保存 token 时数据库被别的写操作锁住：最多试几次
SSL_CONTEXT = httpx.create_ssl_context()  # 所有请求共用：每个客户端现建一份证书配置要在主线程卡约 25 毫秒
ECHO_SCHEMA = 2  # 2：同一关在多个赛季重复出现时保留最快的通关（1 保留的是最新的）


class RecordError(RuntimeError):
    """森空岛对记录请求返回了非零 code。"""


class Unreadable(RuntimeError):
    """回答读不了（HTTP 错误、不是预期的格式）：和网络失败一样，再试一次，而不是当成森空岛拒绝。"""


REFRESH_WAIT = 60  # /竞速更新 排在别人的个人更新后面时最多等多久（一次只要几秒）


def people(binds: list[tuple[str, int]]) -> dict[str, int]:
    """每个人一条：（号码 -> bind_id）。一个人绑着几个号（占位编号和后来认出的 QQ 号）时只留一个：
    官方机器人现在认的那个，没有说法时真正的 QQ 号，再不然最小的那个。否则统计里同一个人会算两次。"""
    identity = sys.modules.get("plugins.official_bridge.identity")  # 只在配置了官方机器人的地方才有
    recognised = getattr(identity, "recognised", None)

    def rank(number: str) -> tuple:
        known = bool(recognised(number)) if recognised else False
        return (not known, not (number.isdigit() and int(number) < PLACEHOLDER_FROM), int(number) if number.isdigit() else 0)

    numbers: dict[int, list[str]] = {}
    for number, bind_id in binds:
        numbers.setdefault(bind_id, []).append(str(number))
    return {min(found, key=rank): bind_id for bind_id, found in numbers.items()}


PLACEHOLDER_FROM = 9_000_000_000  # 官方机器人给没认出 QQ 号的成员的占位编号从这里开始


class EchoStore:
    def __init__(self, data_file: Path, optout_file: Path, layout_file: Path | None = None) -> None:
        self.data_file = data_file
        self.optout_file = optout_file
        self.layout_file = layout_file or data_file.with_name("layout.json")
        self.lock = asyncio.Lock()  # 通关记录（echoes.json）：全员采集和个人更新一次只能有一个在写
        self.builds_lock = asyncio.Lock()  # 配装（只读名片的那一轮不碰通关记录，所以不占上面那把锁）
        self.collecting = False  # 正在全员采集通关记录（不只是某个人在更新自己）

    def layout(self) -> dict:
        """森空岛对所有玩家都一样的内容：按游戏内顺序排列的丰碑和关卡，以及它们的图片。"""
        try:
            return json.loads(self.layout_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save_layout(self, layout: dict) -> None:
        if not layout.get("monument") or layout == self.layout():
            return
        write_json(self.layout_file, layout, ensure_ascii=False)

    def load(self) -> dict:
        try:
            return json.loads(self.data_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {"members": {}}

    def current(self) -> bool:
        """存储的通关记录是否按当前规则提取。要把整个文件解析一遍，别放在启动时。"""
        return self.data_file.exists() and self.load().get("schema") == ECHO_SCHEMA

    def _save(self, data: dict) -> None:
        """保存时按当下的退出名单再筛一遍：采集途中有人退出，他的记录也不会被这一轮写回去。"""
        for qq in self.optout():
            data["members"].pop(qq, None)
        write_json(self.data_file, data, ensure_ascii=False)

    def optout(self) -> set[str]:
        # 文件不存在表示没人退出；文件损坏时不能悄悄把人重新算进来。
        if not self.optout_file.exists():
            return set()
        return set(json.loads(self.optout_file.read_text("utf-8")))

    def set_optout(self, qq: str, out: bool) -> None:
        ids = self.optout()
        ids.add(qq) if out else ids.discard(qq)
        write_json(self.optout_file, sorted(ids))
        if out:
            data = self.load()
            if data["members"].pop(qq, None) is not None:
                self._save(data)

    async def refresh(self, qq: str) -> tuple[str, dict, dict]:
        """立即拉取一位群友的通关记录（/竞速更新），方式与采集相同：(结果, 之前的记录, 之后的记录)。

        结果："ok"；采集占着存储时为 "busy"；"optout"；"unbound"（没有 QQ 绑定或没有默认的终末地角色）；
        "failed"（森空岛拒绝或没有响应）。只有 "ok" 才写入存储，所以失败时保留该群友之前的记录；
        这和采集不同，采集中失败的群友会被删除。
        """
        if self.collecting:
            return "busy", {}, {}
        try:  # 别人正在更新自己：等他几秒；等这么久还没轮到，就是全员采集刚好开始了
            await asyncio.wait_for(self.lock.acquire(), REFRESH_WAIT)
        except asyncio.TimeoutError:
            return "busy", {}, {}
        try:
            return await self._refresh(qq)
        finally:
            self.lock.release()

    async def _refresh(self, qq: str) -> tuple[str, dict, dict]:
        if qq in self.optout():
            return "optout", {}, {}
        data, stats = self.load(), Counter()
        before = dict((data["members"].get(qq) or {}).get("records") or {})
        async with get_session(expire_on_commit=False, autoflush=False) as session:
            bind_id = await owner_id_of(session, qq)
            if bind_id is not None:  # 记在这个人统一用的那个号下面（他可能绑着几个号）
                same = (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.platform == "QQClient", Bind.bind_id == bind_id))).all()
                qq = next(iter(people([(str(n), b) for n, b in same])), qq)
                before = dict((data["members"].get(qq) or {}).get("records") or {})
            if bind_id is None or await default_role(session, bind_id) is None:
                return "unbound", before, {}
            await self._member(session, qq, bind_id, data, stats, set(), None, {}, True)
        if not stats["ok"]:
            return "failed", before, {}
        data["schema"] = ECHO_SCHEMA
        self._save(data)
        return "ok", before, dict(data["members"][qq]["records"])

    async def collect(self, builds: BuildStore | None = None, clears: bool = True) -> dict[str, int]:
        """把每位已绑定且未退出的群友拉取一遍；返回用于日志的计数。

        传入 `builds` 时还会读取每位群友的名片，把干员配装存进去。
        `clears=False` 只读名片（群友保留上一轮的通关记录）。
        """
        async with contextlib.AsyncExitStack() as held:
            if builds is not None:
                await held.enter_async_context(self.builds_lock)
            if clears:  # 只读名片的那一轮不写通关记录：不挡 /竞速更新
                await held.enter_async_context(self.lock)
                self.collecting = True
                held.callback(setattr, self, "collecting", False)
            optout = self.optout()
            data = self.load()
            stats = Counter()
            build_members = builds.load().get("members") or {} if builds else {}
            try:
                await self._collect_into(data, stats, optout, builds, build_members, clears)
            finally:  # 失败后也要保存：已拉取的群友留新记录，其余的留旧记录
                if clears:
                    data["schema"] = ECHO_SCHEMA
                    self._save(data)
                if builds is not None:  # 上面删掉的群友（解绑、退出、失败）也从配装统计中移除
                    builds.save({qq: member for qq, member in build_members.items() if qq in data["members"]})
            return dict(stats)

    async def _collect_into(self, data: dict, stats: Counter, optout: set[str], builds: BuildStore | None, build_members: dict, clears: bool) -> None:
        async with get_session() as session:
            every = [(str(n), b) for n, b in (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.platform == "QQClient"))).all()]
        binds = list(people(every).items())
        left = {b for n, b in every if n in optout}  # 用哪个号退出的都算这个人退出了
        optout = optout | {qq for qq, b in binds if b in left}
        if clears:  # 已经没有绑定、或者换了个号记的人：旧的那条不再算（配装统计跟着 data 一起清）
            for gone in set(data["members"]) - {qq for qq, _ in binds}:
                del data["members"][gone]
                stats["unbound_dropped"] += 1
        queue: asyncio.Queue = asyncio.Queue()
        for bind in binds:
            queue.put_nowait(bind)
        done, saved = 0, time.monotonic()

        async def worker() -> None:
            # 每个 worker 一个 session：AsyncSession 不能被并发任务共用。
            nonlocal done, saved
            async with get_session(expire_on_commit=False, autoflush=False) as session:
                while not queue.empty():
                    qq, bind_id = queue.get_nowait()
                    await self._member(session, qq, bind_id, data, stats, optout, builds, build_members, clears)
                    done += 1
                    if done % 20 == 0:
                        logger.info(f"War Echoes collection progress: {done}/{len(binds)} binds, {dict(stats)}")
                    if clears and time.monotonic() - saved >= SAVE_SECONDS:  # 榜单读的是这个文件：先把目前新拿到的显示出来
                        saved = time.monotonic()
                        data["schema"] = ECHO_SCHEMA
                        self._save(data)

        try:
            async with asyncio.TaskGroup() as group:  # 一个 worker 失败会在最终保存前停掉其他 worker
                for _ in range(WORKERS):
                    group.create_task(worker())
        except ExceptionGroup as failed:  # 调用方记录原因的类型，和以前逐个拉取时一样
            raise failed.exceptions[0] from None

    async def _member(self, session, qq: str, bind_id: int, data: dict, stats: Counter, optout: set[str], builds: BuildStore | None, build_members: dict, clears: bool) -> None:
        if qq in optout:
            data["members"].pop(qq, None)
            return
        # 每位群友一条：其默认的终末地角色（上次之后解绑了，或没有这样的角色：
        # 榜单上就不留过时的通关记录）。
        selected = await default_role(session, bind_id)
        if selected is None:
            stats["dropped"] += data["members"].pop(qq, None) is not None
            return
        user, char = selected
        # API 返回错误 code 时立即删掉该群友；其他失败（网络、登录）重试一次。
        # 仍然失败的群友也删掉，免得过时的通关记录留在榜单上。
        for attempt in range(2 if clears else 0):
            try:
                echoes = await _fetch(WAR_ECHOES_URL, user, char)
                records = _extract(echoes["warEchoes"])
                await asyncio.sleep(REQUEST_GAP)
                monument = await _fetch(INDIE_HARD_URL, user, char)
                records.update(_extract_monument(monument["indieHard"]))
                if not stats["ok"]:  # 对每位群友都一样：每轮取一份就够了
                    self.save_layout(extract_layout(monument["indieHard"]))
                data["members"][qq] = {"ts": time.time(), "records": records}
                stats["ok"] += 1
                break
            except Exception as e:
                if isinstance(e, RecordError) or attempt:
                    stats[type(e).__name__] += 1
                    stats["dropped"] += data["members"].pop(qq, None) is not None
                    break
                await asyncio.sleep(REQUEST_GAP * 2)
        if builds is not None and qq in data["members"]:
            try:  # 失败时保留该群友之前的快照
                if clears:
                    await asyncio.sleep(REQUEST_GAP)
                card = await _fetch(CARD_URL, user, char)
                detail = card.get("detail") or {}
                rosters = await self._other_rosters(session, bind_id, char, (build_members.get(qq) or {}).get("rosters") or {}, stats)
                rosters[str(char.id)] = role_roster(detail)
                build_members[qq] = {"ts": time.time(), "chars": extract(detail), "rosters": rosters}
                stats["builds"] += 1
            except Exception as e:
                stats[f"builds_{type(e).__name__}"] += 1
        # 立刻保存刷新过的 token：如果一个写操作在整轮采集中一直挂着，就会占住 SQLite 的写锁
        # 好几分钟，导致绑定 / 抽卡记录更新失败。
        await _commit(session, stats)
        await asyncio.sleep(REQUEST_GAP)


    @staticmethod
    async def _other_rosters(session, bind_id: int, default: Character, previous: dict, stats: Counter) -> dict:
        """群友其他角色（任意账号）的持有名单；拉取失败的角色保留之前的名单，已解绑的角色去掉。"""
        rosters = {}
        for user, char in await member_roles(session, bind_id):
            if char.id == default.id:
                continue
            key = str(char.id)
            await asyncio.sleep(REQUEST_GAP)
            try:
                rosters[key] = role_roster((await _fetch(CARD_URL, user, char)).get("detail") or {})
                stats["other_roles"] += 1
            except Exception as e:
                stats[f"other_roles_{type(e).__name__}"] += 1
                if key in previous:
                    rosters[key] = previous[key]
        return rosters


async def _commit(session, stats: Counter) -> None:
    """保存刷新过的 token；数据库被别的写操作锁住时先等，等不到就放弃（下次会再刷新 token）。

    曾经在 404 人中的第 300 人处一次 commit 被锁，结束了那轮采集，已拉取的数据全部丢失。
    """
    for attempt in range(1, COMMIT_TRIES + 1):
        try:
            await session.commit()
            return
        except OperationalError:
            await session.rollback()
            if attempt < COMMIT_TRIES:  # 最后一次失败后直接放弃
                await asyncio.sleep(3 * attempt)
    stats["commit_locked"] += 1


async def _fetch(base_url: str, user: SkUser, char: Character) -> dict:
    url = f"{base_url}?" + urlencode({"roleId": char.role_id, "serverId": char.channel_master_id, "userId": user.skland_user_id})

    async def call() -> dict:
        cred = CRED(cred=user.cred, token=user.cred_token)
        async with httpx.AsyncClient(timeout=20, verify=SSL_CONTEXT) as client:
            response = await client.get(url, headers=await SklandAPI.get_sign_header(cred, url, method="get"))
        if response.status_code >= 500 or response.status_code == 429:  # 限流、服务器出错：不是对这个人的回答
            raise Unreadable(f"http {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise Unreadable("not json") from None
        if not isinstance(payload, dict):
            raise Unreadable("not an object")
        return payload

    payload = await call()
    if payload.get("code") == 10000:  # cred_token 已过期：用 cred 重新刷新
        user.cred_token = await SklandLoginAPI.refresh_token(user.cred)
        payload = await call()
    if payload.get("code") == 10002:  # cred 过期；需要已保存的 access token
        if not user.access_token:
            raise LoginException("cred expired")
        new = await SklandLoginAPI.get_cred(await SklandLoginAPI.get_grant_code(user.access_token, 0))
        user.cred, user.cred_token = new.cred, new.token
        payload = await call()
    if payload.get("code"):
        raise RecordError(f"skland record code {payload.get('code')}")
    if not isinstance(payload.get("data"), dict):
        raise Unreadable("no data")
    return payload["data"]


def _faster(new: dict, old: dict | None) -> bool:
    """最快的通关胜出；没有用时的记录排在后面；并列时先创下的胜出。"""
    if old is None:
        return True
    rank = lambda r: (r["time"] is None, r["time"] or 0, r["at"] or float("inf"))
    return rank(new) < rank(old)


def _extract(echoes: dict) -> dict:
    """所有赛季中每个 "关卡|难度" 用时最短的通关编队。


    同一关会在多个赛季/轮换中出现，各有自己的 bestRecord。
    """
    records = {}
    for season in sorted(echoes.get("seasons", []), key=lambda s: int(s.get("id") or 0)):
        for week in season.get("weeks", []):
            for group in week.get("dungeonGroups", []):
                name = group.get("name", "")
                for field, difficulty in DIFFICULTY_FIELDS:
                    dungeon = group.get(field) or {}
                    best = dungeon.get("bestRecord") or {}
                    chars = best.get("chars") or []
                    if dungeon.get("isPass") and chars:
                        record, key = _record(best, bool(dungeon.get("plusTask"))), f"{name}|{difficulty}"
                        if _faster(record, records.get(key)):
                            records[key] = record
    return records


def _record(best: dict, plus: bool = False) -> dict:
    """passTs 是以整秒计的通关用时（只有最高难度才有）。"""
    seconds = best.get("passTs")
    return {
        "plus": plus,
        "time": int(seconds) if str(seconds or "").isdigit() and int(seconds) > 0 else None,
        "at": int(best["ts"]) if str(best.get("ts") or "").isdigit() else 0,
        "team": [
            {"id": c.get("charId", ""), "lv": c.get("level", 0), "pot": c.get("potentialLevel", 0)}
            for c in best.get("chars") or []
        ],
    }


def _extract_monument(indie_hard: dict) -> dict:
    """影拓丰碑的最佳通关记录，键为 "关卡|普通" / "关卡|苦难"。"""
    records = {}
    for series in indie_hard.get("indieHardGroups", []):
        for group in series.get("dungeonGroups", []):
            for field, difficulty in (("normalDungeon", "普通"), ("hardDungeon", "苦难")):
                dungeon = group.get(field) or {}
                best = dungeon.get("bestRecord") or {}
                if dungeon.get("isPass") and best.get("chars"):
                    name = dungeon.get("name", "").split("·")[0].strip()
                    record, key = _record(best), f"{name}|{difficulty}"
                    if _faster(record, records.get(key)):
                        records[key] = record
    return records


def extract_layout(indie_hard: dict) -> dict:
    """按游戏里的顺序排列的丰碑和关卡，以及森空岛用的封面图。

    游戏数据表里既没有影拓丰碑的关卡顺序，也没有丰碑封面。
    """

    def stamp(value) -> int:
        return int(value) if str(value or "").isdigit() else 0

    monument = [
        {
            "name": series.get("name", ""),
            "pic": series.get("pic", ""),
            "stages": [
                (group.get("normalDungeon") or group.get("hardDungeon") or {}).get("name", "").split("·")[0].strip()
                for group in series.get("dungeonGroups", [])
            ],
            "activity": series.get("activityName", ""),
            "open": stamp(series.get("activityStartTs")),
            "close": stamp(series.get("activityEndTs")),
        }
        for series in indie_hard.get("indieHardGroups", [])
    ]
    return {"monument": monument}


def speed_ranking(data: Mapping, group_members: set[str], stage_key: str) -> list[tuple[str, dict]]:
    """有通关用时的 (qq, 记录) 对，最快的在前；并列时先创下的在前。"""
    rows = [
        (qq, m["records"][stage_key])
        for qq, m in data.get("members", {}).items()
        if qq in group_members and m.get("records", {}).get(stage_key, {}).get("time")
    ]
    return sorted(rows, key=lambda row: (row[1]["time"], row[1]["at"] or float("inf")))


def usage_rates(data: Mapping, group_members: set[str], stage_keys: set[str] | None) -> tuple[int, Counter]:
    """每个干员出现在多少条通关记录里（每条记录中一个干员只算一次）。"""
    total, counts = 0, Counter()
    for qq, member in data.get("members", {}).items():
        if qq not in group_members:
            continue
        for key, record in member.get("records", {}).items():
            if stage_keys is not None and key not in stage_keys:
                continue
            total += 1
            counts.update({c["id"] for c in record["team"] if c["id"]})
    return total, counts


def partners(data: Mapping, same: dict[str, str] | None = None, mates_top: int = 6, teams_top: int = 3) -> dict[str, dict]:
    """根据群友已存的通关记录，统计每个干员：有多少群友带它通关、和谁组队，以及最常见的完整阵容。
    每位群友对每个队友、每种阵容只算一次，这样一个人的大量通关不会压过其他人。
    `same` 把同一干员的不同 id 映射为一个（管理员）。"""
    same = same or {}
    users, mates, lineups = Counter(), {}, {}
    for member in data.get("members", {}).values():
        with_whom: dict[str, set[str]] = {}
        teams: dict[str, set[tuple[str, ...]]] = {}
        for record in member.get("records", {}).values():
            ids = sorted({same.get(c["id"], c["id"]) for c in record["team"] if c.get("id")})
            if len(ids) < 2:
                continue
            for cid in ids:
                with_whom.setdefault(cid, set()).update(x for x in ids if x != cid)
                teams.setdefault(cid, set()).add(tuple(ids))
        for cid, others in with_whom.items():
            users[cid] += 1
            mates.setdefault(cid, Counter()).update(others)
            lineups.setdefault(cid, Counter()).update(t for t in teams[cid] if len(t) == 4)
    return {
        cid: {"n": n, "mates": mates[cid].most_common(mates_top), "teams": [[list(t), c] for t, c in lineups[cid].most_common(teams_top)]}
        for cid, n in users.items()
    }
