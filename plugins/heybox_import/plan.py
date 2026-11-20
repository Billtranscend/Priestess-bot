"""把小黑盒（Heybox）终末地“抽卡分析”的概览，变成官方 API 已经给不出的抽卡记录行。

小黑盒按卡池保存汇总：总抽数、当前保底、每个 6 星及其精确时间和用了多少抽（`diff`），以及其间
抽到的 5 星（名称 + 数量）。它不保存每一个 4 星。对于我们数据库里没有官方记录的时段，
这里重建抽卡记录，使抽数、6 星和保底都准确：

  - 6 星那一行带真实名称，时间精确到秒；
  - 该段里的 5 星带真实名称，但在段内的位置未知；
  - 其余每一抽都是占位行（PLACEHOLDER_NAME，4 星）。

重建的行 `pos` 为负（官方 seqId 为正），所以绝不会和官方行冲突，随时都能区分出来。官方行从不修改：
卡池只会向前（更早的方向）延伸，而且只在两边数据重叠部分一致时才延伸。

小黑盒还会报告它在每种卡池类型里见过的最新官方 seqId。比这更新的官方行是它快照之后的抽卡：
这些行不参与比较，所以同一份快照无论何时重建计划，结果都一样（见 __init__.py 的 `reconcile`）。

纯函数：没有 I/O，不碰数据库。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

PLACEHOLDER_NAME = "小黑盒导入"
PLACEHOLDER_RARITY = 4
SUPPORTED_PREFIXES = ("special", "joint", "wepon", "weapon")  # 外加下面列出的确切 id
SUPPORTED_IDS = ("standard", "beginner")
SEQ_KEYS = (  # 卡池 id 前缀 -> 小黑盒 gacha_type_latest_seq_id 里的键（武器池共用 "weapon"）
    ("special", "E_CharacterGachaPoolType_Special"),
    ("joint", "E_CharacterGachaPoolType_Joint"),
    ("standard", "E_CharacterGachaPoolType_Standard"),
    ("beginner", "E_CharacterGachaPoolType_Beginner"),
)


@dataclass
class Official:
    """数据库里已有的一行官方记录。"""

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
    partial: bool = False  # 只补了官方记录之前的那段（官方记录只有这个卡池的后半段）


@dataclass
class Plan:
    rows: list[dict] = field(default_factory=list)
    pools: list[PoolResult] = field(default_factory=list)
    error: str = ""  # 完全不能导入时设置
    heybox_pulls: int = 0  # 小黑盒知道的付费抽数，用于给用户的说明
    official_pulls: int = 0
    estimated: list[str] = field(default_factory=list)  # 抽数只能估算的卡池名称（明日方舟共用保底、又排不出一条链时）

    @property
    def imported(self) -> list[PoolResult]:
        return [p for p in self.pools if p.pulls]

    @property
    def skipped(self) -> list[PoolResult]:
        return [p for p in self.pools if p.skipped]


def _is_gift(entry: dict) -> bool:
    """小黑盒把里程碑赠送（赠送潜能 / 赠送武器）和抽卡列在一起；它们不是抽卡。"""
    return bool(entry.get("is_free_potential") or entry.get("is_free_weapon_or_box"))


def _six_stars(entries: list[dict]) -> list[dict]:
    """真实的 6 星抽卡，最早的在前，从 id 里拆出秒数和在本批中的序号。"""
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
    """为 `count` 个占位抽卡生成时间戳，按十连分批，严格落在两个已知时间之间。"""
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
        size = count - 10 * (groups - 1) if g == 0 else 10  # 不满十个的那批是最早的
        stamps += [ts] * size
    return stamps


def _segment(events: list, entry: dict, fillers: int, prev: dict | None, weapon: bool) -> None:
    """追加通向某个 6 星的那些抽（entry 为 None 时追加 `fillers` 个尾部抽卡）。"""
    five = [name for sr in (entry or {}).get("sr_list") or [] for name in [sr.get("name")] * int(sr.get("count") or 0) if name]
    five = five[:fillers]
    labels = [(name, 5) for name in five] + [(PLACEHOLDER_NAME, PLACEHOLDER_RARITY)] * (fillers - len(five))
    stamps: list[int] = []
    prev_ts = prev["ts"] if prev else None
    carry = (9 - prev["index"]) if prev and (weapon or prev["index"] > 0) else 0  # 上一次十连里剩下的抽
    if entry is not None and prev is not None and entry["ts"] == prev_ts:
        stamps = [prev_ts] * fillers  # 同一次十连里出了两个 6 星
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
    """免费十连：同一秒的十行，其中的 6 星（如果有）在第 `diff` 位。"""
    ts = int(entry["timestamp"]) // 1000
    events = [{"ts": ts, "name": PLACEHOLDER_NAME, "rarity": PLACEHOLDER_RARITY, "is_free": True} for _ in range(10)]
    if entry.get("name"):
        slot = max(1, min(10, int(entry.get("diff") or 10))) - 1
        events[slot] = {"ts": ts, "name": entry["name"], "rarity": 6, "is_free": True}
    return events


def _opening(pool: dict, window: tuple[int, int] | None, account_start: int) -> int:
    """卡池开放的时间（unix 秒），没人知道时为 0。

    小黑盒自己的 start_time 通常是 0，有时还和结束时间对调，所以只有落在游戏数据表给出的
    时间窗内才相信它；否则用时间窗的开始时间。常驻卡池没有时间窗：用小黑盒开始统计该账号的时间代替。
    """
    start = int((pool.get("pool_info") or {}).get("start_time") or 0)
    start = start // 1000 if start > 10**11 else start
    if window:
        return start if window[0] <= start < window[1] else window[0]
    return start if start > 0 else account_start


def _snapshot_seq(result: dict, pool_id: str, weapon: bool) -> int | None:
    """小黑盒在这个卡池类型里拿到的最新官方 seqId（如果它给了的话）。"""
    key = "weapon" if weapon else next((key for prefix, key in SEQ_KEYS if pool_id.lower().startswith(prefix)), "")
    value = (result.get("gacha_type_latest_seq_id") or {}).get(key)
    return int(value) if value else None


def _pool_events(
    pool: dict, official: list[Official], weapon: bool, window: tuple[int, int] | None = None, account_start: int = 0
) -> tuple[list[dict], str]:
    """某个卡池里早于我们官方记录的抽卡，最早的在前；或者跳过它的原因。"""
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
        elif pity:  # 一个 6 星都没有：卡池开放时间是唯一的锚点
            start = _opening(pool, window, account_start)
            if start <= 0:
                return [], "这个卡池没有出过六星，也查不到卡池开放时间，无法确定抽卡时间"
            events += [{"ts": ts, "name": PLACEHOLDER_NAME, "rarity": PLACEHOLDER_RARITY, "is_free": False} for ts in _spread(pity, start, None)]
    else:
        first_ts = paid[0].ts
        if not any(e["ts"] < first_ts for e in entries) and int(pool.get("total_count") or 0) <= len(paid):
            return [], ""  # 这个卡池里小黑盒没有比我们的官方记录更早的东西
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
            prev_ts = prev["ts"] if prev else None  # 没有更早的 6 星：缺的只是我们第一行之前的那些抽
            stamps = [prev_ts] * tail + _spread(missing - tail, prev_ts, first_ts)
            events += [{"ts": ts, "name": PLACEHOLDER_NAME, "rarity": PLACEHOLDER_RARITY, "is_free": False} for ts in stamps]

    has_free = any(o.is_free for o in official)
    first_official = min((o.ts for o in official), default=None)
    for entry in pool.get("free_records") or []:
        if entry.get("is_free") and not _is_gift(entry) and not has_free:
            if first_official is None or int(entry["timestamp"]) // 1000 < first_official:
                events += _free_segment(entry)
    return events, ""


def build_plan(result: dict, uid: str, official: list[Official], pool_windows: dict[str, tuple[int, int]] | None = None) -> Plan:
    """根据小黑盒的概览 `result` 和我们的官方记录，生成角色 `uid` 要插入的行。

    `pool_windows` 把卡池 id 映射到 (开放, 关闭) 的 unix 秒，来自游戏数据表；
    玩家在某个卡池从没出过 6 星时，靠它来安放该池的抽卡。
    """
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

    account_start = int((result.get("statistic_info") or {}).get("count_time") or 0) // 1000
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
            if pool_id.lower().startswith("rerun"):  # 重构寻访（1.6）：足够新，官方记录是完整的
                outcome.skipped = "重构寻访，官方记录已完整同步，无需导入"
            elif not (pool_id.lower().startswith(SUPPORTED_PREFIXES) or pool_id in SUPPORTED_IDS):
                outcome.skipped = "暂不支持的卡池类型"
            else:
                seq = _snapshot_seq(result, pool_id, weapon)
                known = [row for row in by_pool.get(pool_id, []) if seq is None or row.pos <= seq]
                events, reason = _pool_events(pool, known, weapon, (pool_windows or {}).get(pool_id), account_start)
                outcome.skipped = reason
                if events:
                    outcome.pulls = len(events)
                    outcome.six_stars = sum(1 for e in events if e["rarity"] == 6)
                    staged.append((pool_id, outcome.pool_name, weapon, events))
            if outcome.pulls or outcome.skipped:
                plan.pools.append(outcome)

    # 为负，并随时间严格递增；同一批的行保持连续（用于识别十连）。
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
