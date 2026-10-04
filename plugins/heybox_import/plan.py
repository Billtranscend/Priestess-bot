"""Turn a 小黑盒 (Heybox) Endfield "抽卡分析" overview into gacha rows the official API can no longer give.

Heybox keeps an aggregate per pool: total pulls, the current pity, every 6-star with its exact
time and the number of pulls it took (`diff`), and the 5-stars pulled in between (name + count).
It does not keep each 4-star. For a period our database has no official rows for, the pulls are
rebuilt so that counts, 6-stars and pity are exact:

  - the 6-star row carries the real name and second;
  - the 5-stars of the segment carry real names, at an unknown position inside the segment;
  - every other pull is a placeholder row (PLACEHOLDER_NAME, 4-star).

Rebuilt rows get a negative `pos` (official seqIds are positive), so they can never collide with
official rows and can be told apart or removed at any time. Official rows are never modified:
a pool is only extended backwards, and only when the overlap between the two sources agrees.

Pure functions: no I/O, no database.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

PLACEHOLDER_NAME = "小黑盒导入"
PLACEHOLDER_RARITY = 4
SUPPORTED_PREFIXES = ("special", "joint", "wepon", "weapon")  # plus the exact ids below
SUPPORTED_IDS = ("standard", "beginner")


@dataclass
class Official:
    """One official row already in the database."""

    pool_id: str
    name: str
    rarity: int
    ts: int
    pos: int
    is_free: bool


@dataclass
class PoolResult:
    pool_id: str
    pool_name: str
    pulls: int = 0
    six_stars: int = 0
    skipped: str = ""


@dataclass
class Plan:
    rows: list[dict] = field(default_factory=list)
    pools: list[PoolResult] = field(default_factory=list)
    error: str = ""  # set when nothing may be imported at all
    heybox_pulls: int = 0  # paid pulls Heybox knows about, for the user-facing explanation
    official_pulls: int = 0

    @property
    def imported(self) -> list[PoolResult]:
        return [p for p in self.pools if p.pulls]

    @property
    def skipped(self) -> list[PoolResult]:
        return [p for p in self.pools if p.skipped]


def _is_gift(entry: dict) -> bool:
    """Heybox lists milestone gifts (赠送潜能 / 赠送武器) next to the pulls; they are not pulls."""
    return bool(entry.get("is_free_potential") or entry.get("is_free_weapon_or_box"))


def _six_stars(entries: list[dict]) -> list[dict]:
    """Real 6-star pulls, oldest first, with second / index-in-batch split out of the id."""
    result = []
    for entry in entries:
        if not entry.get("name") or _is_gift(entry):
            continue
        parts = str(entry.get("id", "")).split("_")
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        result.append({**entry, "ts": int(entry["timestamp"]) // 1000, "index": index})
    return sorted(result, key=lambda e: (int(e["timestamp"]), e["index"]))


def _owned_by(entries: list[dict], uid: str) -> bool:
    return all(str(e.get("id", "")).startswith(f"{uid}_") for e in entries if e.get("name") or e.get("is_free"))


def _spread(count: int, after: int | None, before: int | None) -> list[int]:
    """Timestamps for `count` placeholder pulls in batches of ten, strictly between two known times."""
    groups = -(-count // 10)
    stamps = []
    for g in range(groups):
        if after is not None and before is not None:
            ts = after + max(1, (before - after) * (g + 1) // (groups + 1))
            ts = min(ts, before - 1) if before - after > 1 else after
        elif before is not None:
            ts = before - 60 * (groups - g)
        else:
            ts = (after or 0) + 60 * (g + 1)
        size = count - 10 * (groups - 1) if g == 0 else 10  # the short batch is the oldest one
        stamps += [ts] * size
    return stamps


def _segment(events: list, entry: dict, fillers: int, prev: dict | None, weapon: bool) -> None:
    """Append the pulls leading to one 6-star (or `fillers` trailing pulls when entry is None)."""
    five = [name for sr in (entry or {}).get("sr_list") or [] for name in [sr.get("name")] * int(sr.get("count") or 0) if name]
    five = five[:fillers]
    labels = [(name, 5) for name in five] + [(PLACEHOLDER_NAME, PLACEHOLDER_RARITY)] * (fillers - len(five))
    stamps: list[int] = []
    prev_ts = prev["ts"] if prev else None
    carry = (9 - prev["index"]) if prev and (weapon or prev["index"] > 0) else 0  # pulls left in the previous ten-pull
    if entry is not None and prev is not None and entry["ts"] == prev_ts:
        stamps = [prev_ts] * fillers  # two 6-stars in the same ten-pull
    else:
        tail = min(carry, fillers)
        head = min(entry["index"], fillers - tail) if entry is not None else 0
        middle = fillers - tail - head
        stamps = [prev_ts] * tail + _spread(middle, prev_ts, entry["ts"] if entry is not None else None) + ([entry["ts"]] * head if entry else [])
    for (name, rarity), ts in zip(labels, stamps):
        events.append({"ts": ts, "name": name, "rarity": rarity, "is_free": False})
    if entry is not None:
        events.append({"ts": entry["ts"], "name": entry["name"], "rarity": 6, "is_free": False})


def _free_segment(entry: dict) -> list[dict]:
    """The free ten-pull: ten rows at one second, with its 6-star (if any) at position `diff`."""
    ts = int(entry["timestamp"]) // 1000
    events = [{"ts": ts, "name": PLACEHOLDER_NAME, "rarity": PLACEHOLDER_RARITY, "is_free": True} for _ in range(10)]
    if entry.get("name"):
        slot = max(1, min(10, int(entry.get("diff") or 10))) - 1
        events[slot] = {"ts": ts, "name": entry["name"], "rarity": 6, "is_free": True}
    return events


def _pool_events(pool: dict, official: list[Official], weapon: bool) -> tuple[list[dict], str]:
    """Pulls of one pool that predate our official rows, oldest first; or a reason to skip it."""
    entries = _six_stars(pool.get("records") or [])
    pity = int(pool.get("current_pity") or 0)
    if sum(int(e.get("diff") or 0) for e in entries) + pity != int(pool.get("total_count") or 0):
        return [], "小黑盒数据自身对不上（六星间隔与总抽数不符）"

    paid = sorted((o for o in official if not o.is_free), key=lambda o: (o.ts, o.pos))
    events: list[dict] = []
    if not paid:
        prev = None
        for entry in entries:
            _segment(events, entry, int(entry["diff"]) - 1, prev, weapon)
            prev = entry
        if prev is not None:
            _segment(events, None, pity, prev, weapon)
        elif pity:  # no 6-star at all: the pool's opening time is the only anchor
            start = int((pool.get("pool_info") or {}).get("start_time") or 0)
            if start <= 0:
                return [], "这个卡池没有出过六星，小黑盒也没有给出卡池时间，无法确定抽卡时间"
            events += [
                {"ts": ts, "name": PLACEHOLDER_NAME, "rarity": PLACEHOLDER_RARITY, "is_free": False}
                for ts in _spread(pity, start // 1000 if start > 10**11 else start, None)
            ]
    else:
        first_ts = paid[0].ts
        if not any(e["ts"] < first_ts for e in entries) and int(pool.get("total_count") or 0) <= len(paid):
            return [], ""  # Heybox holds nothing older than our official rows for this pool
        ours = Counter((o.name, o.ts) for o in paid if o.rarity == 6)
        theirs = Counter((e["name"], e["ts"]) for e in entries if e["ts"] >= first_ts)
        if theirs - ours:
            return [], "与机器人已有的官方记录对不上"
        if ours - theirs:
            return [], "小黑盒的数据比机器人已有的旧，请先在小黑盒里更新后再导入"
        older = [e for e in entries if e["ts"] < first_ts]
        overlap = [e for e in entries if e["ts"] >= first_ts]
        if overlap:
            target = overlap[0]
            row = next(o for o in paid if o.rarity == 6 and (o.name, o.ts) == (target["name"], target["ts"]))
            have = sum(1 for o in paid if (o.ts, o.pos) <= (row.ts, row.pos))
            missing = int(target["diff"]) - have
        else:
            missing = pity - len(paid)
        if missing < 0:
            return [], "小黑盒的数据比机器人已有的旧，请先在小黑盒里更新后再导入"
        prev = None
        for entry in older:
            _segment(events, entry, int(entry["diff"]) - 1, prev, weapon)
            prev = entry
        if missing:
            carry = (9 - prev["index"]) if prev and (weapon or prev["index"] > 0) else 0
            tail = min(carry, missing)
            prev_ts = prev["ts"] if prev else None  # no older 6-star: only the pulls before our first row are missing
            stamps = [prev_ts] * tail + _spread(missing - tail, prev_ts, first_ts)
            events += [{"ts": ts, "name": PLACEHOLDER_NAME, "rarity": PLACEHOLDER_RARITY, "is_free": False} for ts in stamps]

    has_free = any(o.is_free for o in official)
    first_official = min((o.ts for o in official), default=None)
    for entry in pool.get("free_records") or []:
        if entry.get("is_free") and not _is_gift(entry) and not has_free:
            if first_official is None or int(entry["timestamp"]) // 1000 < first_official:
                events += _free_segment(entry)
    return events, ""


def build_plan(result: dict, uid: str, official: list[Official]) -> Plan:
    """Rows to insert for the role `uid`, given Heybox's overview `result` and our official rows."""
    plan = Plan()
    if not result.get("is_bind") or not result.get("user_info"):
        plan.error = (
            "这个小黑盒 ID 没有终末地「抽卡分析」的数据。请确认填的是小黑盒 ID 而不是游戏 UID，"
            "并且已经在小黑盒 App 的终末地「抽卡分析」里登录游戏账号、点过更新"
        )
        return plan
    if str(result["user_info"].get("uid")) != str(uid):
        plan.error = "这个小黑盒账号绑定的终末地 UID 与你在机器人绑定的不一致，不能导入别人的记录"
        return plan

    plan.official_pulls = sum(1 for row in official if not row.is_free)
    plan.heybox_pulls = sum(int(pool.get("total_count") or 0) for category in result.get("gacha_record") or [] for pool in category.get("pool_records") or [])
    by_pool: dict[str, list[Official]] = {}
    for row in official:
        by_pool.setdefault(row.pool_id, []).append(row)

    staged: list[tuple[str, str, bool, list[dict]]] = []
    for category in result.get("gacha_record") or []:
        weapon = category.get("gacha_type") == "weapon"
        for pool in category.get("pool_records") or []:
            pool_id = str(pool.get("pool_id") or "")
            outcome = PoolResult(pool_id=pool_id, pool_name=str(pool.get("pool_name") or pool_id))
            everything = (pool.get("records") or []) + (pool.get("free_records") or [])
            if not _owned_by(everything, str(uid)):
                plan.error = "小黑盒返回的记录里出现了其他 UID，已拒绝导入"
                plan.rows, plan.pools = [], []
                return plan
            if not (pool_id.lower().startswith(SUPPORTED_PREFIXES) or pool_id in SUPPORTED_IDS):
                outcome.skipped = "暂不支持的卡池类型"
            else:
                events, reason = _pool_events(pool, by_pool.get(pool_id, []), weapon)
                outcome.skipped = reason
                if events:
                    outcome.pulls = len(events)
                    outcome.six_stars = sum(1 for e in events if e["rarity"] == 6)
                    staged.append((pool_id, outcome.pool_name, weapon, events))
            if outcome.pulls or outcome.skipped:
                plan.pools.append(outcome)

    # Negative, strictly increasing in time; rows of one batch stay consecutive (ten-pull detection).
    flat = [
        {**event, "pool_id": pool_id, "pool_name": pool_name, "item_type": "weapon" if weapon else "char", "_order": (event["ts"], pool_id, i)}
        for pool_id, pool_name, weapon, events in staged
        for i, event in enumerate(events)
    ]
    flat.sort(key=lambda r: r.pop("_order"))
    for i, row in enumerate(flat):
        row["pos"] = i - len(flat)
    plan.rows = flat
    return plan
