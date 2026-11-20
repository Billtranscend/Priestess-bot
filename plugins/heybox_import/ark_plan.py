"""把小黑盒（Heybox）明日方舟“抽卡分析”的汇总，变成官方 API 已经给不出的抽卡记录行。

小黑盒按卡池保存汇总：每个卡池的抽数（`cost`）、每个 6 星的精确时间和出货时的保底计数（`diff`），
以及每段里出的 5 星和它们当时的保底计数（`sr_info`）。没有 3、4 星的逐条记录。这里重建抽卡记录，
使每个卡池的抽数、6 星、5 星和它们的位置都准确：

  - 6 星、5 星带真实干员和编号（编号取自小黑盒的头像地址），6 星的时间精确到秒；
  - 其余每一抽都是占位行（PLACEHOLDER_NAME）；最后一个 6 星之后出的 5 星小黑盒没有记录，也是占位行；
  - 6 星和它前面的最多 9 抽算作同一次十连，用 6 星的时间；其余的抽按十连一批，
    均匀落在相邻两个已知时间之间，并且都在卡池开放期内。

保底计数：每个限定池自己计，小黑盒标为独立计算（`is_separated`）的活动卡池（如跨年欢庆）也是；
标准寻访（小黑盒的 permanent、act）共用一个，中坚寻访（hardcore）共用一个，
所以 `diff` 会跨卡池累计。共用的那两组，小黑盒在 `bottom_list` 里给出当前的保底计数，据此算出
小黑盒开始统计之前已经垫了多少抽，再检查每个 6 星都落在它自己的卡池里。

小黑盒没记下卡池的 6 星，会单独出现在卡池列表里（没有 detail，也没有抽数）。它所在卡池的抽数是
`head_info` 里这一组的总抽数减去已知卡池的抽数：这些抽照样排进保底序列，只是写不进去。

排不出一条链的共用组（几个卡池同时开放、轮流抽，或者小黑盒漏记了一些抽）只能估算：每个卡池单独排，
抽数、6 星、5 星和 6 星的时间仍然准确，6 星前的抽数照小黑盒显示的保底计数填（卡池里放不下时压进去），
5 星与 6 星的距离不变；这些卡池记在 `Plan.estimated` 里，回复里会说明。

小黑盒的卡池 id 是“开放时间_类型”，不是游戏的卡池 id：按名称和开放时间对应到游戏的卡池表，
所有 6 星的时间都必须落在那个卡池的开放期内。

只补机器人还没有官方记录的卡池，官方行从不改动。官方接口只给最近一段时间的抽卡，所以跨过那个界限的
卡池，机器人只有它的后半段：这样的卡池只补前面查不到的那段（`_head`）。两边都有的时段里，6 星必须一致，
否则整个导入作废。重建的行 `pos` 为负（官方行是十连里的 0 到 9），随时间递增。

纯函数：没有 I/O，不碰数据库。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

from .plan import Official, Plan, PoolResult, _spread

PLACEHOLDER_NAME = "小黑盒导入"
PLACEHOLDER_RARITY = 3  # 官方的稀有度从 0 算起：3 是四星（3、4 星分不出来；页面只画五星和六星）
FIVE, SIX = 4, 5
UNRECORDED = "小黑盒没有记下这个卡池"  # 小黑盒列出了 6 星，却不知道是在哪个卡池抽的（回复里合成一句说明）
GENERIC_POOL_NAME = "适合多种场合的强力干员"  # 游戏表里标准寻访、中坚寻访等几百个卡池都叫这个
SHARED = {"permanent": "standard", "act": "standard", "hardcore": "classic"}  # 小黑盒的卡池类型 -> 共用保底的组；限定池各自一组
PITY_SLOT = {"standard": 1, "classic": 2}  # bottom_list 的顺序与页面的统计块一致：限定、标准、中坚
GROUP_TOTAL = {"standard": "act_info", "classic": "normal_info"}  # head_info 里这一组的总抽数
MATCH_WINDOW = 2 * 86400  # 按名称对应卡池时，开放时间最多差多少秒
TIME_WINDOW = 6 * 3600  # 只按开放时间对应时最多差多少秒：小黑盒记的开始时间偶尔和游戏表差几分钟（见过 15:50 对 16:00）
KIND_PREFIXES = {  # 同一时间开放好几个卡池时，按小黑盒的卡池类型挑
    "limited": ("LIMITED", "LINKAGE"),
    "permanent": ("NORM", "DOUBLE", "SINGLE", "SPECIAL"),
    "act": ("NORM", "DOUBLE", "SINGLE", "SPECIAL"),
    "hardcore": ("CLASSIC", "FESCLASSIC"),
}
CHAR_ID = re.compile(r"(char_\d+_[a-z0-9]+)")


@dataclass
class GamePool:
    """游戏卡池表（gacha_table 的 gachaPoolClient）里的一个卡池。"""

    pool_id: str
    name: str
    open: int
    close: int


@dataclass
class _Pool:
    game: GamePool | None  # 对不上游戏卡池时为 None：只占位置，不写行
    name: str
    start: int  # 小黑盒给的开放时间，game 为 None 时用来排顺序
    cost: int
    entries: list[dict]  # 6 星，最早的在前
    problem: str = ""  # 不写这个卡池的原因
    first: int = 0  # 这个卡池在本组抽卡序列里占的位置：(first, last]
    last: int = 0


def _bare(name: str) -> str:
    return re.sub(r"(·复刻|（复刻）)$", "", re.sub(r"^【[^】]*】", "", name or "")).strip()


def _match(detail: dict, pools: list[GamePool]) -> GamePool | None:
    """小黑盒的卡池对应游戏的哪个卡池：先按名称（取开放时间最近的），通用名称的卡池按开放时间（取最近的）。"""
    start = int(detail.get("start") or 0)
    named = [p for p in pools if p.name != GENERIC_POOL_NAME and _bare(p.name) == _bare(detail.get("name", ""))]
    if named:
        best = min(named, key=lambda p: abs(p.open - start))
        return best if abs(best.open - start) <= MATCH_WINDOW else None
    near = sorted((p for p in pools if abs(p.open - start) <= TIME_WINDOW), key=lambda p: abs(p.open - start))
    if len(near) > 1:
        near = [p for p in near if p.pool_id.startswith(KIND_PREFIXES.get(detail.get("gacha_type"), ()))]
    if len(near) > 1 and abs(near[0].open - start) == abs(near[1].open - start):
        return None  # 同时开放的同类卡池，分不出是哪一个
    return near[0] if near else None


def _char_id(item: dict) -> str:
    found = CHAR_ID.search(str(item.get("img") or ""))
    return found.group(1) if found else ""


def _six_stars(pool: dict) -> list[dict]:
    """6 星，最早的在前（小黑盒最新的在前；同一次十连里的几个 6 星时间相同，保持原来的先后）。"""
    entries = [e for e in pool.get("list") or [] if e.get("name") and int(e.get("diff") or 0) > 0]
    return sorted(reversed(entries), key=lambda e: int(e["ts"]))


def _pool_name(game: GamePool, heybox_name: str) -> str:
    return _bare(heybox_name) if game.name == GENERIC_POOL_NAME else _bare(game.name)


def _place(group: list[_Pool], carried: int) -> dict[int, dict] | None:
    """一组共用保底的卡池按开放顺序排成一条抽卡序列，返回 位置 -> 那一抽（6 星、5 星）；对不上时为 None。

    `carried`：小黑盒开始统计之前已经垫了的抽数，算在第一个 6 星的 diff 里。
    """
    position, total, known = -carried, 0, {}
    for pool in group:
        pool.first, pool.last = total, total + pool.cost
        total = pool.last
    for pool in group:
        for entry in pool.entries:
            start = position  # 这一段保底开始之前的位置
            position += int(entry["diff"])
            if not pool.first < position <= pool.last:
                return None
            known[position] = {"name": entry["name"], "char_id": _char_id(entry), "rarity": SIX, "is_new": bool(entry.get("is_new")), "ts": int(entry["ts"])}
            for five in entry.get("sr_info") or []:
                spot = start + int(five.get("diff") or 0)
                if 0 < spot < position and spot not in known:
                    known[spot] = {"name": five.get("name") or PLACEHOLDER_NAME, "char_id": _char_id(five), "rarity": FIVE, "is_new": bool(five.get("is_new"))}
    return known if position <= total else None


def _place_each(group: list[_Pool]) -> dict[int, dict] | None:
    """共用保底却排不出一条链时，每个卡池单独排；6 星前的抽数照小黑盒的保底计数，放不下时压进卡池。"""
    total, known = 0, {}
    for pool in group:
        pool.first, pool.last = total, total + pool.cost
        total = pool.last
        if len(pool.entries) > pool.cost:
            return None
        spot = pool.first
        for j, entry in enumerate(pool.entries):
            diff = int(entry["diff"])
            spot = max(spot + 1, min(spot + diff, pool.last - (len(pool.entries) - 1 - j)))
            known[spot] = {"name": entry["name"], "char_id": _char_id(entry), "rarity": SIX, "is_new": bool(entry.get("is_new")), "ts": int(entry["ts"])}
            for five in entry.get("sr_info") or []:
                where = spot - diff + int(five.get("diff") or 0)  # 与 6 星的距离不变
                if pool.first < where < spot and where not in known:
                    known[where] = {"name": five.get("name") or PLACEHOLDER_NAME, "char_id": _char_id(five), "rarity": FIVE, "is_new": bool(five.get("is_new"))}
    return known


def _stamps(pool: _Pool, known: dict[int, dict]) -> list[int]:
    """卡池里每一抽的时间（按序列顺序）：6 星用自己的时间，其余按十连分批落在已知时间之间。"""
    positions = list(range(pool.first + 1, pool.last + 1))
    anchors = [(p, known[p]["ts"]) for p in positions if known.get(p, {}).get("rarity") == SIX]
    stamps: dict[int, int] = {}
    previous_spot, previous_ts = pool.first, pool.game.open
    for spot, ts in anchors:
        if ts == previous_ts and previous_spot > pool.first:  # 同一次十连里的第二个 6 星
            stamps.update({p: ts for p in range(previous_spot + 1, spot + 1)})
        else:
            batch_start = max(previous_spot + 1, spot - 9)
            stamps.update({p: ts for p in range(batch_start, spot + 1)})
            before = list(range(previous_spot + 1, batch_start))
            stamps.update(zip(before, _spread(len(before), previous_ts, ts)))
        previous_spot, previous_ts = spot, ts
    after = list(range(previous_spot + 1, pool.last + 1))
    stamps.update(zip(after, _spread(len(after), previous_ts, None)))
    return [min(max(stamps[p], pool.game.open), pool.game.close) for p in positions]


def _agrees(pools: list[_Pool], official: list[Official]) -> bool:
    """两边都有数据的时段里，6 星（干员、秒）一致。"""
    by_pool: dict[str, list[Official]] = {}
    for row in official:
        by_pool.setdefault(row.pool_id, []).append(row)
    for pool in pools:
        ours = by_pool.get(pool.game.pool_id) if pool.game else None
        if not ours or not pool.entries:
            continue
        since, until = min(o.ts for o in ours), max(int(e["ts"]) for e in pool.entries)
        mine = Counter((o.name, o.ts) for o in ours if o.rarity == SIX and since <= o.ts <= until)
        theirs = Counter((e["name"], int(e["ts"])) for e in pool.entries if since <= int(e["ts"]) <= until)
        if mine != theirs:
            return False
    return True


def _head(pool: _Pool, known: dict[int, dict], official: list[Official]) -> _Pool | None:
    """官方记录只有后半段的卡池，返回前面查不到的那段（要补的抽）；补不了时为 None。

    官方接口只给最近一段时间的抽卡，所以角色最早的官方记录之前开放的卡池，开头那些抽已经查不到了。
    要补，卡池必须在最早的官方记录之前开放、在最新的官方记录之前结束（期间的抽官方都给过），
    并且小黑盒的 6 星恰好以最早的官方记录为界：前面那段里的都更早，后面那段里的都不更早。
    """
    ours = sum(1 for row in official if row.pool_id == pool.game.pool_id)
    earliest, latest = min(row.ts for row in official), max(row.ts for row in official)
    cut = pool.last - ours
    if cut <= pool.first or not pool.game.open < earliest or not pool.game.close < latest:
        return None
    sixes = [(spot, pull["ts"]) for spot, pull in known.items() if pool.first < spot <= pool.last and pull["rarity"] == SIX]
    if any((spot <= cut) != (ts < earliest) for spot, ts in sixes):
        return None
    return replace(pool, last=cut, game=replace(pool.game, close=min(pool.game.close, earliest - 1)))


def _add_strays(members: list[_Pool], entries: list[dict], head: dict, key: str) -> bool:
    """把没记下卡池的 6 星当作一个抽数已知、写不进去的卡池，排进这一组；做不到时返回 False。

    抽数 = 这一组的总抽数 - 已知卡池的抽数。几个这样的 6 星必须夹在同样两个已知卡池之间，才能当作一个卡池。
    """
    entries = sorted(entries, key=lambda e: int(e["ts"]))
    cost = int((head.get(GROUP_TOTAL[key]) or {}).get("total") or 0) - sum(pool.cost for pool in members)
    starts = sorted(pool.game.open if pool.game else pool.start for pool in members)
    if cost < 0 or len({sum(1 for s in starts if s <= int(e["ts"])) for e in entries}) > 1:
        return False
    name = "、".join(dict.fromkeys(e["name"] for e in entries)) + "所在的卡池"
    members.append(_Pool(None, name, int(entries[0]["ts"]), cost, entries, UNRECORDED))
    return True


def build_ark_plan(result: dict, uid: str, official: list[Official], game_pools: list[GamePool]) -> Plan:
    """根据小黑盒明日方舟的汇总 `result` 和我们的官方记录，生成角色 `uid` 要插入的行。"""
    plan = Plan()
    record = result.get("gacha_record") or {}
    if not result.get("user_info") or not record.get("total_pools"):
        plan.error = (
            "这个小黑盒 ID 没有明日方舟「抽卡分析」的数据。请确认填的是小黑盒 ID 而不是游戏 UID，"
            "并且已经在小黑盒 App 的明日方舟「抽卡分析」里登录游戏账号、点过更新"
        )
        return plan
    if str(result["user_info"].get("uid")) != str(uid):
        plan.error = "这个小黑盒账号绑定的明日方舟 UID 与你在机器人绑定的不一致，不能导入别人的记录"
        return plan

    plan.official_pulls = len(official)
    plan.heybox_pulls = sum(int(pool.get("cost") or 0) for pool in record["total_pools"])
    covered = {row.pool_id for row in official}
    groups: dict[str, list[_Pool]] = {}
    strays: dict[str, list[dict]] = {}  # 小黑盒没记下卡池的 6 星，按共用保底的组
    for heybox in record["total_pools"]:
        if not heybox.get("detail") and heybox.get("name") and int(heybox.get("diff") or 0) > 0:
            if heybox.get("gacha_type") in SHARED:
                strays.setdefault(SHARED[heybox["gacha_type"]], []).append(heybox)
            else:
                plan.pools.append(PoolResult("", f"{heybox['name']}所在的卡池", skipped=UNRECORDED))
            continue
        detail = heybox.get("detail") or {}
        kind, name = detail.get("gacha_type"), _bare(detail.get("name", ""))
        alone = kind == "limited" or bool(detail.get("is_separated"))  # 保底只在这个卡池里计（跨年欢庆的类型小黑盒有时标限定、有时空着）
        if not alone and kind not in SHARED:
            plan.pools.append(PoolResult(str(detail.get("pool_id") or ""), name, skipped="暂不支持的卡池类型"))
            continue
        game, entries = _match(detail, game_pools), _six_stars(heybox)
        problem = ""
        if game is None:
            problem = "对不上游戏里的卡池"
        elif any(not game.open <= int(e["ts"]) <= game.close for e in entries):
            problem = "六星的时间不在这个卡池的开放期内"
        pool = _Pool(None if problem else game, _pool_name(game, name) if game else name, int(detail.get("start") or 0), int(heybox.get("cost") or 0), entries, problem)
        groups.setdefault(f"alone:{detail.get('pool_id')}" if alone else SHARED[kind], []).append(pool)

    broken = set()  # 没记下卡池的 6 星排不进去的组：只能每个卡池单独估算
    for key, entries in strays.items():
        if not _add_strays(groups.setdefault(key, []), entries, result.get("head_info") or {}, key):
            broken.add(key)
            plan.pools += [PoolResult("", f"{e['name']}所在的卡池", skipped=UNRECORDED) for e in entries]

    staged: list[tuple[list[_Pool], dict[int, dict], bool]] = []
    for key, members in groups.items():
        members.sort(key=lambda p: p.game.open if p.game else p.start)
        diffs = sum(int(e["diff"]) for pool in members for e in pool.entries)
        total = sum(pool.cost for pool in members)
        if key in PITY_SLOT:
            slots = record.get("bottom_list") or []
            pity = slots[PITY_SLOT[key]].get("value") if len(slots) > PITY_SLOT[key] else None
            carried = diffs + int(pity) - total if pity is not None and key not in broken else -1
        else:
            carried = 0  # 限定池：保底从这个卡池开始计
        known = _place(members, carried) if carried >= 0 else None
        estimated = known is None and key in PITY_SLOT
        if estimated:  # 排不出一条链：只能每个卡池单独估算
            known = _place_each(members)
        if known is None:
            for pool in members:
                plan.pools.append(PoolResult(pool.game.pool_id if pool.game else "", pool.name, skipped=pool.problem or "小黑盒数据自身对不上（六星的保底计数与卡池抽数不符）"))
            continue
        staged.append((members, known, estimated))

    if not _agrees([pool for members, _, _ in staged for pool in members], official):
        plan.error = "小黑盒的记录和机器人已有的官方记录对不上，已拒绝导入"
        plan.pools = []
        return plan

    flat = []
    for members, known, estimated in staged:
        for pool in members:
            if pool.game is None:
                plan.pools.append(PoolResult("", pool.name, skipped=pool.problem))
                continue
            outcome = PoolResult(pool.game.pool_id, pool.name)
            part = pool
            if pool.game.pool_id in covered:
                part = _head(pool, known, official)
                if part is None:
                    if sum(1 for row in official if row.pool_id == pool.game.pool_id) < pool.cost:
                        outcome.skipped = "机器人已有这个卡池的一部分官方记录，这个卡池没有补"
                        plan.pools.append(outcome)
                    continue
                outcome.partial = True
            for offset, ts in enumerate(_stamps(part, known)):
                spot = part.first + 1 + offset
                pull = known.get(spot) or {"name": PLACEHOLDER_NAME, "char_id": "", "rarity": PLACEHOLDER_RARITY, "is_new": False}
                flat.append({**pull, "ts": ts, "pool_id": pool.game.pool_id, "pool_name": pool.name, "_order": (ts, pool.game.open, spot)})
            outcome.pulls = part.last - part.first
            outcome.six_stars = sum(1 for spot in range(part.first + 1, part.last + 1) if known.get(spot, {}).get("rarity") == SIX)
            plan.pools.append(outcome)
            if estimated:
                plan.estimated.append(pool.name)

    # 为负，并随时间严格递增；同一批的行保持连续。
    flat.sort(key=lambda row: row.pop("_order"))
    for i, row in enumerate(flat):
        row["pos"] = i - len(flat)
    plan.rows = flat
    return plan
