"""已绑定群友的干员配装，用于图鉴卡片上的使用统计。

游戏里没有推荐装备表，所以卡片展示群友实际在用的配装。
每天的采集（echoes.EchoStore.collect）还会读取每位群友的森空岛名片，对等级不低于 MIN_LEVEL 的干员
记下潜能、身上的四件装备、所属套装，以及每件装备三个词条各精锻了几次。只展示汇总结果。每个套装展示
群友最常穿的整套配装（四件一起穿的组合，所以确实有人这么穿；按部位各取最常见的话，可能拼出一套没人穿的配装），
以及这几件装备上每个词条的平均精锻次数。
6 星干员按潜能分组展示（POTENTIAL_GROUPS：低潜能的群友常把一个套装凑满四件，
高潜能的会换掉一件来换更好的主属性），每组取最常用的套装；其他干员潜能大多本来就满了，按最常用的
TOP_SETS 个套装展示。两种都在这里算好，由图鉴卡片按稀有度挑选。
同一张名片还列出角色拥有的所有干员及潜能：群友的 `rosters` 为每个绑定账号的每个角色各存一份
（上面的配装仍只取默认角色的），供 /干员统计 使用。权限等级低于 HOLDING_MIN_LEVEL 的角色会保存但不计入：
这些大多是只试玩过终末地、主要玩明日方舟的群友。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from plugins.shared import write_json

CARD_URL = "https://zonai.skland.com/web/v1/game/endfield/card/detail"
SLOTS = ("bodyEquip", "armEquip", "firstAccessory", "secondAccessory")
SLOT_LABELS = ("护甲", "护手", "配件")  # 两个配件槽可以互换：按一对计数
MIN_LEVEL = 70  # 最高档装备要 Lv70；等级更低的干员还穿着过渡装备
SET_PIECES = 3  # 激活套装效果所需件数
MIN_SAMPLE = 3  # 配装数少于这个就没有参考意义
TOP_SETS = 3  # 每个干员列出的套装数，最常用的在前
REFINE_SLOTS = 3  # 每件装备的词条数；每个词条精锻 0-3 次
BUILD_SCHEMA = 5  # 2：每件装备的精锻等级，3：已拥有干员名单，4：每个账号的每个角色各一份名单，5：附带角色等级
POTENTIAL_GROUPS = (("0-3 潜", 0, 3), ("4 潜以上", 4, 5))  # （标签，最低潜能，最高潜能）
HOLDING_MIN_LEVEL = 30  # 权限等级


def extract(detail: dict) -> dict[str, dict]:
    """从名片的 `detail` 得到 干员名 -> {"pot", "equips": [每个部位为 [装备, 套装, [精锻等级 x3]] 或 None]}。"""
    chars: dict[str, dict] = {}
    for char in detail.get("chars") or []:
        name, level = (char.get("charData") or {}).get("name"), int(char.get("level") or 0)
        if not name or level < MIN_LEVEL:
            continue
        equips = []
        for slot in SLOTS:
            equip = char.get(slot) or {}
            data, enhance = equip.get("equipData") or {}, equip.get("enhance") or {}
            refine = [int(enhance.get(str(index)) or 0) for index in range(1, REFINE_SLOTS + 1)]
            equips.append([data["name"], (data.get("suit") or {}).get("name") or "", refine] if data.get("name") else None)
        if any(equips):
            chars[name] = {"pot": int(char.get("potentialLevel") or 0), "equips": equips}
    return chars


def roster(detail: dict) -> dict[str, int]:
    """名片 `detail` 上所有干员（不限等级）的 干员名 -> 潜能（0-5）。男女管理员同名。"""
    owned: dict[str, int] = {}
    for char in detail.get("chars") or []:
        name = (char.get("charData") or {}).get("name")
        if name:
            owned[name] = max(owned.get(name, 0), int(char.get("potentialLevel") or 0))
    return owned


def role_roster(detail: dict) -> dict:
    """角色名片中用于持有统计的内容：{"level": 权限等级, "owned": 持有名单}。"""
    return {"level": int((detail.get("base") or {}).get("level") or 0), "owned": roster(detail)}


def holdings(members: dict, exclude: set[str] = frozenset()) -> tuple[int, dict[str, tuple[int, float]]]:
    """（计入的角色数，干员名 -> （持有它的角色数，这些角色的平均潜能））；空名单是隐藏了名片，不是没有干员的玩家。"""
    rosters = [
        role["owned"]
        for qq, member in members.items()
        if qq not in exclude
        for role in (member.get("rosters") or {}).values()
        if role.get("owned") and role.get("level", 0) >= HOLDING_MIN_LEVEL
    ]
    owners: dict[str, list[int]] = {}
    for owned in rosters:
        for name, potential in owned.items():
            owners.setdefault(name, []).append(potential)
    return len(rosters), {name: (len(levels), sum(levels) / len(levels)) for name, levels in owners.items()}


def _set_block(rows: list[tuple], suit: str, out_of: int) -> dict:
    """`rows`（潜能, 套装, 配装, 装备）中套装 `suit` 最常见的整套配装：{"set", "set_count", "n": out_of, "count", "pieces"}。"""
    in_set = [row for row in rows if row[1] == suit]
    loadouts = Counter(row[2] for row in in_set)
    # 穿的人最多的；并列时取装备件数更多的配装
    loadout, count = max(loadouts.items(), key=lambda item: (item[1], sum(bool(n) for n in (item[0][0], item[0][1], *item[0][2]))))
    refines: dict[str, list[list[int]]] = {}
    for row in in_set:
        if row[2] == loadout:
            for equip in row[3]:
                if equip:
                    refines.setdefault(equip[0], []).append(equip[2])
    pieces = []
    for slot, piece in ((SLOT_LABELS[0], loadout[0]), (SLOT_LABELS[1], loadout[1]), *((SLOT_LABELS[2], n) for n in loadout[2])):
        if piece:
            worn = refines.get(piece) or []
            average = [round(sum(levels[i] for levels in worn) / len(worn), 1) for i in range(REFINE_SLOTS)] if worn else None
            pieces.append({"slot": slot, "name": piece, "refine": average})
    return {"set": suit, "set_count": len(in_set), "n": out_of, "count": count, "pieces": pieces}


def aggregate(members: dict, exclude: set[str] = frozenset()) -> dict:
    """对激活了套装效果的配装做统计：{"operators": {name: {n, builds, by_potential}}, "sets": {name: {n, operators}}}。

    `builds`：该干员全部 `n` 份配装中最常用的 TOP_SETS 个套装，每个是一个 _set_block。
    `by_potential`：POTENTIAL_GROUPS 每组一项，{"group", "n": 该组配装数}，组内有配装时再加上
    最常用套装的 _set_block。
    """
    rows_of: dict[str, list[tuple]] = {}  # 干员 -> [(潜能, 套装, 配装, 装备)]
    by_set: dict[str, Counter] = {}
    for qq, member in members.items():
        if qq in exclude:
            continue
        for name, build in (member.get("chars") or {}).items():
            suits = Counter(e[1] for e in build["equips"] if e and e[1])
            active = next((s for s, count in suits.most_common(1) if count >= SET_PIECES), None)
            if active is None:
                continue
            by_set.setdefault(active, Counter())[name] += 1
            names = [equip[0] if equip else "" for equip in build["equips"]]
            loadout = (names[0], names[1], tuple(sorted(n for n in names[2:] if n)))
            rows_of.setdefault(name, []).append((int(build["pot"]), active, loadout, build["equips"]))
    operators = {}
    for name, rows in rows_of.items():
        top = [_set_block(rows, suit, len(rows)) for suit, _ in Counter(row[1] for row in rows).most_common(TOP_SETS)]
        groups = []
        for label, low, high in POTENTIAL_GROUPS:
            group = [row for row in rows if low <= row[0] <= high]
            block = {"group": label, "n": len(group)}
            if group:
                block.update(_set_block(group, Counter(row[1] for row in group).most_common(1)[0][0], len(group)))
            groups.append(block)
        operators[name] = {"n": len(rows), "builds": top, "by_potential": groups}
    sets = {
        name: {"n": sum(counter.values()), "operators": [(op, count, len(rows_of[op])) for op, count in counter.most_common(8)]}
        for name, counter in by_set.items()
    }
    return {"operators": operators, "sets": sets}


def wearers(members: dict, names: set[str], exclude: set[str] = frozenset(), top: int = 8) -> dict:
    """至少穿了 `names` 中一件的配装（Lv70+ 干员）：{"n", "operators": [(干员, 穿着的份数, 该干员全部配装数)]}。

    用于不属于任何套装的散件，它们不在 aggregate() 的套装统计里。
    """
    wearing, totals = Counter(), Counter()
    for qq, member in members.items():
        if qq in exclude:
            continue
        for name, build in (member.get("chars") or {}).items():
            totals[name] += 1
            wearing[name] += any(e and e[0] in names for e in build["equips"])
    wearing = +wearing
    return {"n": sum(wearing.values()), "operators": [(op, count, totals[op]) for op, count in wearing.most_common(top)]}


class BuildStore:
    def __init__(self, data_file: Path) -> None:
        self.data_file = data_file
        self._cache: tuple | None = None

    def load(self) -> dict:
        try:
            return json.loads(self.data_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {"members": {}}

    def current(self) -> bool:
        """存储的快照是否按当前规则提取。"""
        return self.data_file.exists() and self.load().get("schema") == BUILD_SCHEMA

    def save(self, members: dict) -> None:
        write_json(self.data_file, {"schema": BUILD_SCHEMA, "members": members}, ensure_ascii=False)

    def wearers(self, names: set[str], exclude: set[str] = frozenset()) -> dict:
        return wearers(self.load().get("members") or {}, names, exclude)

    def holdings(self, exclude: set[str] = frozenset()) -> tuple[int, dict[str, tuple[int, float]]]:
        return holdings(self.load().get("members") or {}, exclude)

    def stats(self, exclude: set[str] = frozenset()) -> dict:
        """汇总结果；文件或退出名单变化时重新计算。"""
        try:
            key = (self.data_file.stat().st_mtime, frozenset(exclude))
        except OSError:
            return {"operators": {}, "sets": {}}
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, aggregate(self.load().get("members") or {}, exclude))
        return self._cache[1]
