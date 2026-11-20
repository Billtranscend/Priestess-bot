"""本项目的终末地 UP 统计：辉光庆典和重构寻访也算限定池。

nonebot-plugin-skland 只有当 6★ 是某个限定寻访卡池（"special_..."）当期的 UP 干员时才算 UP，
也只有这类卡池计入 UP 数据。辉光庆典（"joint_..."）把阵容里每个 6★ 都标成 UP，常驻的也算，
却不计入数据；重构寻访（"rerun_chr_..."）则完全不算。这里三种都算限定池：
  限定寻访   只有当期 UP 干员算 UP，和上游一致（阵容里更早的限定干员算歪）；
  辉光庆典、  抽到的每个限定干员都算 UP：即卡池表里某个限定寻访或重构寻访卡池的 UP 干员
  重构寻访   （卡池太新、表里还没有时，按游戏自己的卡池数据）。所以在辉光庆典里
             抽到洁尔佩塔算 UP，抽到艾尔黛拉不算。

6★ 的 80 抽保底在一个限定寻访卡池和下一个之间继承，重构寻访卡池之间也一样，但两类之间
不共享（已存的记录对两点都能证实：任一类内部没有超过 80 抽不出 6★ 的，而跨两类的有）；
辉光庆典有自己独立的保底。所以“最后一个 UP 之后的抽数”在限定寻访卡池之间累计、在重构寻访
卡池之间累计、辉光庆典按每个卡池单独计。
这是普通模块（不是插件）：供 skland_ef_theme（/zmd抽卡记录）和 skland_gacha_rank（/zmd欧非榜）使用。
"""

from __future__ import annotations

from nonebot_plugin_skland.data_source import ef_gacha_pool_data

LIMITED_PREFIXES = ("special", "joint", "rerun")
UP_SOURCES = ("special", "rerun")  # UP 干员是限定干员的卡池类型


def is_limited(pool) -> bool:
    """可能出限定干员的角色卡池。"""
    return pool.pool_type != "weapon" and pool.pool_id.lower().startswith(LIMITED_PREFIXES)


def split(grouped) -> tuple[list, list]:
    """（限定寻访的卡池，辉光庆典和重构寻访的卡池），取自上游的分组。"""
    return list(grouped.special_pools), [p for p in grouped.joint_pools + grouped.rerun_pools if is_limited(p)]


def limited_ids(pools=()) -> set[str]:
    ids = {
        char.id
        for table in ef_gacha_pool_data.pool_table.values()
        if table.pool_gacha_type == "char" and table.pool_type in UP_SOURCES and table.up6_name
        for char in table.all
        if char.rarity == 6 and char.name == table.up6_name
    }
    ids.update(i for p in pools if p.pool_id.lower().startswith(UP_SOURCES) for i in p.up_six_chars)
    return ids


def mark(pools) -> None:
    """设定限定池的 UP 干员：限定寻访取当期 UP；其他两类取它能出或已出的每个限定 6★。"""
    pools = [p for p in pools if is_limited(p)]
    for pool in pools:  # 先看卡池表，和上游一样：还没人抽之前就知道它的 UP
        if table := ef_gacha_pool_data.get_pool(pool.pool_id):
            pool.up_six_chars = table.up_six_char_ids
    limited = limited_ids(pools)
    for pool in pools:
        if pool.pool_id.lower().startswith("special"):  # 只算当期 UP
            continue
        table = ef_gacha_pool_data.get_pool(pool.pool_id)
        lineup = [c.id for c in table.all if c.rarity == 6] if table else []
        pulled = [pull.item_id for group in reversed(pool.records) for pull in group.pulls if pull.rarity == 6]
        ups = [i for i in pool.up_six_chars if i in limited]
        ups += [i for i in dict.fromkeys(lineup + pulled) if i in limited and i not in ups]
        pool.up_six_chars = ups


def up_count(pools) -> int:
    """抽到的 UP 干员个数，含免费十连里出的（用于显示个数）。"""
    return sum(p.total_six_stars - (p.total_six_stars if p.total_six_spook is None else p.total_six_spook) for p in pools)  # 不知道 UP（None）就一个也不算


def is_rerun(pool) -> bool:
    return pool.pool_id.lower().startswith("rerun")


def _since(pools, stop) -> int:
    """在共享保底的卡池之间，`stop` 认可的最后一次付费抽之后的付费抽数。"""
    pulls = sorted(
        ((group.gacha_ts, pull.seq_id, pull, pool) for pool in pools for group in pool.records for pull in group.pulls),
        key=lambda entry: (entry[0], entry[1]),
    )
    count = 0
    for _, _, pull, pool in reversed(pulls):
        if pull.is_free:
            continue
        if stop(pull, pool):
            break
        count += 1
    return count


def pity(pools) -> int:
    """在共享保底的卡池之间（同一类卡池），上一个付费 6★ 之后的付费抽数。"""
    return _since(pools, lambda pull, pool: pull.rarity == 6)


def paid_up_count(pools) -> int:
    """付费抽出的 UP 干员个数；免费十连里出的不算，它们没有花抽数。"""
    return sum(1 for p in pools for group in p.records for pull in group.pulls
               if pull.rarity == 6 and not pull.is_free and pull.item_id in p.up_six_chars)


def up_avg(special, others) -> float | None:
    """平均每个付费 UP 的付费抽数，不含最后一个 UP 之后的抽数（那些抽还没出 UP）；没有付费 UP 时为 None。"""
    count = paid_up_count(special + others)
    if not count:
        return None
    paid = sum(p.paid_pulls for p in special + others)
    rerun = [p for p in others if is_rerun(p)]
    joint = [p for p in others if not is_rerun(p)]
    is_up = lambda pull, pool: pull.rarity == 6 and pull.item_id in pool.up_six_chars  # noqa: E731
    tail = _since(special, is_up) + _since(rerun, is_up) + sum(p.up_pity_count for p in joint)
    return (paid - tail) / count


# ── 武器池：沿用 0.7.2 的算法（旧武器池里没出 UP 的付费抽也算成本），不用 0.7.4 只算付费 UP 的新口径 ──


def weapon_up_avg(pools) -> float:
    """付费武器抽数减去最新那次 UP 之后的抽数，除以抽到的 UP 武器数。"""
    count = up_count(pools)
    if not count:
        return 0.0
    tail = _since(pools, lambda pull, pool: pull.rarity == 6 and pull.item_id in pool.up_six_chars)
    return (sum(p.paid_pulls for p in pools) - tail) / count


def weapon_pity(pools) -> int:
    """最新有记录的武器池的保底水位（武器池之间不继承）。"""
    latest = max(pools, key=lambda p: max((r.gacha_ts for r in p.records), default=0), default=None)
    return latest.pity_count if latest else 0

