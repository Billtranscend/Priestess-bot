"""Endfield UP counting for this project: every limited operator is an UP, in every limited pool.

nonebot-plugin-skland counts a 6★ as UP only when it is the current UP operator of a 限定寻访 pool
("special_..."), and only those pools enter the UP figures. 辉光庆典 ("joint_...") marks every 6★ of its
line-up as UP, the standard ones too, but is left out of the figures; 重构寻访 ("rerun_chr_...") is left
out entirely. Here the limited pools are all three kinds, and a 6★ pulled in one of them is UP when it
is a limited operator: the UP operator of some 限定寻访 or 重构寻访 pool of the pool table (or, for a pool
too new for the table, of the game's own pool data). So 提弗洛斯 pulled in 万物更新 is UP instead of a
loss, 洁尔佩塔 pulled in 辉光庆典 is UP, and 艾尔黛拉 pulled there is not.

The 6★ pity of the 限定寻访 pools is shared between them, so the pulls after the last UP are counted
across them; 辉光庆典 and 重构寻访 keep their own pity, so theirs are counted per pool.
A plain module (no plugin): used by skland_ef_theme (/zmd抽卡记录) and skland_gacha_rank (/zmd欧非榜).
"""

from __future__ import annotations

from nonebot_plugin_skland.data_source import ef_gacha_pool_data

LIMITED_PREFIXES = ("special", "joint", "rerun")
UP_SOURCES = ("special", "rerun")  # pool types whose UP operator is a limited operator


def is_limited(pool) -> bool:
    """A character pool that can give limited operators."""
    return pool.pool_type != "weapon" and pool.pool_id.lower().startswith(LIMITED_PREFIXES)


def split(grouped) -> tuple[list, list]:
    """(限定寻访 pools, 辉光庆典 and 重构寻访 pools) of upstream's grouping, which files 重构寻访 under 常驻池."""
    others = [p for p in grouped.joint_pools + grouped.standard_pools if is_limited(p)]
    return list(grouped.special_pools), others


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
    """Set the UP operators of the limited pools: the limited 6★ each can give or gave, the current UP first."""
    pools = [p for p in pools if is_limited(p)]
    for pool in pools:  # the pool table first, as upstream: its UP is known even before anyone pulled it
        if table := ef_gacha_pool_data.get_pool(pool.pool_id):
            pool.up_six_chars = table.up_six_char_ids
    limited = limited_ids(pools)
    for pool in pools:
        table = ef_gacha_pool_data.get_pool(pool.pool_id)
        lineup = [c.id for c in table.all if c.rarity == 6] if table else []
        pulled = [pull.item_id for group in reversed(pool.records) for pull in group.pulls if pull.rarity == 6]
        ups = [i for i in pool.up_six_chars if i in limited]
        ups += [i for i in dict.fromkeys(lineup + pulled) if i in limited and i not in ups]
        pool.up_six_chars = ups


def up_count(pools) -> int:
    """UP operators pulled, free pulls included (as upstream counts them)."""
    return sum(p.total_six_stars - p.total_six_spook for p in pools)


def _special_tail(special) -> int:
    """Paid pulls after the last paid UP across the 限定寻访 pools (their pity is shared)."""
    pulls = sorted(
        ((group.gacha_ts, pull.seq_id, pull, pool) for pool in special for group in pool.records for pull in group.pulls),
        key=lambda entry: (entry[0], entry[1]),
    )
    count = 0
    for _, _, pull, pool in reversed(pulls):
        if pull.is_free:
            continue
        if pull.rarity == 6 and pull.item_id in pool.up_six_chars:
            break
        count += 1
    return count


def up_avg(special, others) -> float:
    """Paid pulls per UP, without the pulls after the last UP (they have not produced one yet)."""
    count = up_count(special + others)
    if not count:
        return 0.0
    paid = sum(p.paid_pulls for p in special + others)
    tail = _special_tail(special) + sum(p.up_pity_count for p in others)
    return (paid - tail) / count
