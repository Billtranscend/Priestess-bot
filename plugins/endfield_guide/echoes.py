"""Collect bound members' War Echoes (战争回响) best clears for per-group team statistics.

Only the best-record team (operator id, level, potential, elite phase) and the
增辉 flag are kept per stage and difficulty, keyed by QQ. Members who opted out
are never fetched and are dropped from stored data.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
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

WAR_ECHOES_URL = "https://zonai.skland.com/api/v1/game/endfield/card/war-echoes"
INDIE_HARD_URL = "https://zonai.skland.com/api/v1/game/endfield/card/indie-hard"  # 影拓丰碑
DIFFICULTY_FIELDS = (("normalDungeon", "普通"), ("hardDungeon", "困难"), ("cruelDungeon", "残酷"))
REQUEST_GAP = 2.0
ECHO_SCHEMA = 2  # 2: keep the fastest clear when a stage recurs across seasons (1 kept the latest)


class RecordError(RuntimeError):
    """Skland answered the record request with a non-zero code."""


class EchoStore:
    def __init__(self, data_file: Path, optout_file: Path) -> None:
        self.data_file = data_file
        self.optout_file = optout_file
        self.lock = asyncio.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.data_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {"members": {}, "targets": {}}

    def _save(self, data: dict) -> None:
        tmp = self.data_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.data_file)

    def optout(self) -> set[str]:
        # A missing file means nobody opted out; a corrupt one must not silently re-include people.
        if not self.optout_file.exists():
            return set()
        return set(json.loads(self.optout_file.read_text("utf-8")))

    def set_optout(self, qq: str, out: bool) -> None:
        ids = self.optout()
        ids.add(qq) if out else ids.discard(qq)
        tmp = self.optout_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(ids)), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.optout_file)
        if out:
            data = self.load()
            if data["members"].pop(qq, None) is not None:
                self._save(data)

    async def collect(self) -> dict[str, int]:
        """Fetch every bound, non-opted-out member once; returns counters for logging."""
        async with self.lock:
            optout = self.optout()
            data = self.load()
            stats = Counter()
            async with get_session() as session:
                # Plain values: the per-member commit below expires ORM objects, and lazily
                # reloading them outside an await raises MissingGreenlet.
                binds = (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.platform == "QQClient"))).all()
                for done, (qq, bind_id) in enumerate(binds, 1):
                    if done % 20 == 0:
                        logger.info(f"War Echoes collection progress: {done}/{len(binds)} binds, {dict(stats)}")
                    if qq in optout:
                        data["members"].pop(qq, None)
                        continue
                    user = await session.get(SkUser, bind_id)
                    if user is None:  # unbound since the last run: no stale clears on the boards
                        stats["dropped"] += data["members"].pop(qq, None) is not None
                        continue
                    char = (
                        await session.scalars(
                            select(Character).where(Character.id == user.id, Character.isdefault, Character.app_code == "endfield")
                        )
                    ).first()
                    if char is None:
                        stats["dropped"] += data["members"].pop(qq, None) is not None
                        continue
                    # An API error code drops the member at once; other failures (network, login) get one
                    # retry. Members still failing are dropped so stale clears never stay on the boards.
                    for attempt in range(2):
                        try:
                            echoes = await _fetch(WAR_ECHOES_URL, user, char)
                            records, targets = _extract(echoes["warEchoes"])
                            await asyncio.sleep(REQUEST_GAP)
                            monument = await _fetch(INDIE_HARD_URL, user, char)
                            records.update(_extract_monument(monument["indieHard"]))
                            data["members"][qq] = {"ts": time.time(), "records": records}
                            data["targets"].update(targets)
                            stats["ok"] += 1
                            break
                        except Exception as e:
                            if isinstance(e, RecordError) or attempt:
                                stats[type(e).__name__] += 1
                                stats["dropped"] += data["members"].pop(qq, None) is not None
                                break
                            await asyncio.sleep(REQUEST_GAP * 2)
                    # Persist refreshed tokens now: a write left pending across the whole run would hold
                    # SQLite's write lock for minutes and make binding / gacha updates fail.
                    await session.commit()
                    await asyncio.sleep(REQUEST_GAP)
            data["schema"] = ECHO_SCHEMA
            self._save(data)
            return dict(stats)


async def _fetch(base_url: str, user: SkUser, char: Character) -> dict:
    url = f"{base_url}?" + urlencode({"roleId": char.role_id, "serverId": char.channel_master_id, "userId": user.user_id})

    async def call() -> dict:
        cred = CRED(cred=user.cred, token=user.cred_token)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=await SklandAPI.get_sign_header(cred, url, method="get"))
        return response.json()

    payload = await call()
    if payload.get("code") == 10000:  # cred_token expired
        user.cred_token = await SklandLoginAPI.refresh_token(user.cred)
        payload = await call()
    if payload.get("code") == 10002:  # cred expired; needs the stored access token
        if not user.access_token:
            raise LoginException("cred expired")
        new = await SklandLoginAPI.get_cred(await SklandLoginAPI.get_grant_code(user.access_token, 0))
        user.cred, user.cred_token = new.cred, new.token
        payload = await call()
    if payload.get("code"):
        raise RecordError(f"skland record code {payload.get('code')}")
    return payload["data"]


def _faster(new: dict, old: dict | None) -> bool:
    """Fastest clear wins; a record without a time loses; ties go to whoever was set first."""
    if old is None:
        return True
    rank = lambda r: (r["time"] is None, r["time"] or 0, r["at"] or float("inf"))
    return rank(new) < rank(old)


def _extract(echoes: dict) -> tuple[dict, dict]:
    """Fastest cleared team per "stage|difficulty" across all seasons, plus each stage's 增辉 target text.

    A stage recurs in several seasons/rotations, each with its own bestRecord.
    """
    records, targets = {}, {}
    for season in sorted(echoes.get("seasons", []), key=lambda s: int(s.get("id") or 0)):
        for week in season.get("weeks", []):
            for group in week.get("dungeonGroups", []):
                name = group.get("name", "")
                for field, difficulty in DIFFICULTY_FIELDS:
                    dungeon = group.get(field) or {}
                    if dungeon.get("additionalChallengeTarget"):
                        targets[f"{name}|{difficulty}"] = dungeon["additionalChallengeTarget"]
                    best = dungeon.get("bestRecord") or {}
                    chars = best.get("chars") or []
                    if dungeon.get("isPass") and chars:
                        record, key = _record(best, bool(dungeon.get("plusTask"))), f"{name}|{difficulty}"
                        if _faster(record, records.get(key)):
                            records[key] = record
    return records, targets


def _record(best: dict, plus: bool = False) -> dict:
    """passTs is the clear time in whole seconds (only present on top difficulties)."""
    seconds = best.get("passTs")
    return {
        "plus": plus,
        "time": int(seconds) if str(seconds or "").isdigit() and int(seconds) > 0 else None,
        "at": int(best["ts"]) if str(best.get("ts") or "").isdigit() else 0,
        "team": [
            {"id": c.get("charId", ""), "lv": c.get("level", 0), "pot": c.get("potentialLevel", 0), "phase": c.get("evolvePhase", 0)}
            for c in best.get("chars") or []
        ],
    }


def _extract_monument(indie_hard: dict) -> dict:
    """影拓丰碑 best clears keyed "stage|普通" / "stage|苦难"."""
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


def speed_ranking(data: dict, group_members: set[str], stage_key: str) -> list[tuple[str, dict]]:
    """(qq, record) pairs with a clear time, fastest first; ties go to whoever set it earlier."""
    rows = [
        (qq, m["records"][stage_key])
        for qq, m in data.get("members", {}).items()
        if qq in group_members and m.get("records", {}).get(stage_key, {}).get("time")
    ]
    return sorted(rows, key=lambda row: (row[1]["time"], row[1]["at"] or float("inf")))


def usage_rates(data: dict, group_members: set[str], stage_keys: set[str] | None) -> tuple[int, Counter]:
    """How many recorded clears include each operator (each clear counts an operator once)."""
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


def team_stats(data: dict, group_members: set[str], stage_key: str, top: int = 3) -> dict:
    """Most common teams among group members who cleared the stage."""
    clears = [m["records"][stage_key] for qq, m in data.get("members", {}).items() if qq in group_members and stage_key in m.get("records", {})]
    teams: dict[tuple, list] = {}
    for clear in clears:
        teams.setdefault(tuple(sorted(c["id"] for c in clear["team"])), []).append(clear)
    ranked = sorted(teams.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    result = []
    for ids, members in ranked:
        chars = []
        for cid in ids:
            rows = [c for m in members for c in m["team"] if c["id"] == cid]
            chars.append({"id": cid, "lv": round(sum(r["lv"] for r in rows) / len(rows)), "pot": round(sum(r["pot"] for r in rows) / len(rows), 1)})
        result.append({"count": len(members), "plus": sum(1 for m in members if m["plus"]), "chars": chars})
    return {"clears": len(clears), "plus": sum(1 for c in clears if c["plus"]), "teams": result}
