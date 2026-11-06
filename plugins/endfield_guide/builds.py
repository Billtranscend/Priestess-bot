"""Equipment builds of bound members' operators, for the usage statistics on the wiki cards.

The game has no table of recommended equipment, so the cards show what members actually run.
Once a day the collection (echoes.EchoStore.collect) also reads each member's Skland card and
keeps, per operator at MIN_LEVEL or above, its potential, the four equipped pieces, their sets and how far each
of a piece's three attributes was refined (精锻). Only aggregates are shown. Each set is shown with the whole loadout
members most often wear in it (four pieces worn together, so it is one somebody actually wears; per-slot favourites
could combine into a loadout nobody wears) and the average refinement of each attribute on those pieces.
6-star operators are shown per potential group (POTENTIAL_GROUPS, owner 2026-11-06: low-potential members often
fill a set to four pieces, high-potential ones swap one for a better main attribute), the group's most used set
each; the others, whose potential is mostly full anyway, by their TOP_SETS most used sets. Both are computed here,
the wiki card picks by rarity. Snapshots from before the potential was kept take it from the member's roster when
the member has a single role (the roster of the equipped role is not marked otherwise).
The same card lists every operator a role owns with its potential: a member's `rosters` hold one per role of every
bound account (the equipment above stays the default role's), for /zmd持有率. Roles below HOLDING_MIN_LEVEL are kept but
not counted: they are mostly members who only tried Endfield and play Arknights (checked 2026-10-31).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

CARD_URL = "https://zonai.skland.com/web/v1/game/endfield/card/detail"
SLOTS = ("bodyEquip", "armEquip", "firstAccessory", "secondAccessory")
SLOT_LABELS = ("护甲", "护手", "配件")  # the two accessory slots are interchangeable: they are counted as a pair
MIN_LEVEL = 70  # the top equipment tier needs Lv70; lower operators still wear levelling gear
SET_PIECES = 3  # pieces that activate a set effect
MIN_SAMPLE = 3  # fewer builds than this say nothing
TOP_SETS = 3  # sets listed per operator, most used first
REFINE_SLOTS = 3  # attributes per piece; each is refined 0-3 times
BUILD_SCHEMA = 5  # 2: refinement levels per piece, 3: roster of owned operators, 4: one roster per role of every account, 5: with the role's level
# (an operator's "pot" was added later without a new schema: older snapshots fall back to the roster, see _potential)
POTENTIAL_GROUPS = (("0-3 潜", 0, 3), ("4 潜以上", 4, 5))  # (label, lowest, highest potential)
HOLDING_MIN_LEVEL = 30  # 权限等级, the owner's choice


def extract(detail: dict) -> dict[str, dict]:
    """Operator name -> {"lv", "pot", "equips": [[piece, set, [refine level x3]] | None per slot]} from a card's `detail`."""
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
            chars[name] = {"lv": level, "pot": int(char.get("potentialLevel") or 0), "equips": equips}
    return chars


def roster(detail: dict) -> dict[str, int]:
    """Operator name -> potential (0-5) of everything on a card's `detail`, at any level. Male and female Endministrator share a name."""
    owned: dict[str, int] = {}
    for char in detail.get("chars") or []:
        name = (char.get("charData") or {}).get("name")
        if name:
            owned[name] = max(owned.get(name, 0), int(char.get("potentialLevel") or 0))
    return owned


def role_roster(detail: dict) -> dict:
    """What a role's card says for the ownership statistics: {"level": 权限等级, "owned": roster}."""
    return {"level": int((detail.get("base") or {}).get("level") or 0), "owned": roster(detail)}


def holdings(members: dict, exclude: set[str] = frozenset()) -> tuple[int, dict[str, tuple[int, float]]]:
    """(roles counted, operator name -> (roles owning it, their average potential)); a roster of nothing is a hidden card, not a player without operators."""
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


def _potential(member: dict, name: str, build: dict) -> int | None:
    """The operator's potential in this snapshot; None when it cannot be told."""
    if "pot" in build:
        return int(build["pot"])
    rosters = member.get("rosters") or {}
    if len(rosters) == 1:
        owned = next(iter(rosters.values())).get("owned") or {}
        if name in owned:
            return int(owned[name])
    return None


def _set_block(rows: list[tuple], suit: str, out_of: int) -> dict:
    """{"set", "set_count", "n": out_of, "count", "pieces"} for the most common whole loadout of `suit` among `rows` (potential, set, loadout, equips)."""
    in_set = [row for row in rows if row[1] == suit]
    loadouts = Counter(row[2] for row in in_set)
    # most worn; on a tie the loadout with more pieces filled
    loadout, count = max(loadouts.items(), key=lambda item: (item[1], sum(bool(n) for n in (item[0][0], item[0][1], *item[0][2]))))
    refines: dict[str, list[list[int]]] = {}
    for row in in_set:
        if row[2] == loadout:
            for equip in row[3]:
                if equip and len(equip) > 2:  # snapshots older than BUILD_SCHEMA 2 carry no refinement
                    refines.setdefault(equip[0], []).append(equip[2])
    pieces = []
    for slot, piece in ((SLOT_LABELS[0], loadout[0]), (SLOT_LABELS[1], loadout[1]), *((SLOT_LABELS[2], n) for n in loadout[2])):
        if piece:
            worn = refines.get(piece) or []
            average = [round(sum(levels[i] for levels in worn) / len(worn), 1) for i in range(REFINE_SLOTS)] if worn else None
            pieces.append({"slot": slot, "name": piece, "refine": average})
    return {"set": suit, "set_count": len(in_set), "n": out_of, "count": count, "pieces": pieces}


def aggregate(members: dict, exclude: set[str] = frozenset()) -> dict:
    """{"operators": {name: {n, builds, by_potential}}, "sets": {name: {n, operators}}} over builds with an active set.

    `builds`: the operator's TOP_SETS most used sets over all its `n` builds, each a _set_block.
    `by_potential`: one entry per POTENTIAL_GROUPS group, {"group", "n": the group's builds} plus, when it has any,
    the _set_block of its most used set; only builds whose potential is known are in a group.
    """
    rows_of: dict[str, list[tuple]] = {}  # operator -> [(potential or None, set, loadout, equips)]
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
            rows_of.setdefault(name, []).append((_potential(member, name, build), active, loadout, build["equips"]))
    operators = {}
    for name, rows in rows_of.items():
        top = [_set_block(rows, suit, len(rows)) for suit, _ in Counter(row[1] for row in rows).most_common(TOP_SETS)]
        groups = []
        for label, low, high in POTENTIAL_GROUPS:
            group = [row for row in rows if row[0] is not None and low <= row[0] <= high]
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
    """Builds (Lv70+ operators) wearing at least one of `names`: {"n", "operators": [(operator, wearing, all builds of it)]}.

    For loose pieces, which belong to no set and so are not in aggregate()'s set statistics.
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
        """Whether the stored snapshots were extracted by the current rules."""
        return self.data_file.exists() and self.load().get("schema") == BUILD_SCHEMA

    def save(self, members: dict) -> None:
        tmp = self.data_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema": BUILD_SCHEMA, "members": members}, ensure_ascii=False), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.data_file)

    def wearers(self, names: set[str], exclude: set[str] = frozenset()) -> dict:
        return wearers(self.load().get("members") or {}, names, exclude)

    def holdings(self, exclude: set[str] = frozenset()) -> tuple[int, dict[str, tuple[int, float]]]:
        return holdings(self.load().get("members") or {}, exclude)

    def stats(self, exclude: set[str] = frozenset()) -> dict:
        """Aggregates, recomputed when the file or the opt-out list changes."""
        try:
            key = (self.data_file.stat().st_mtime, frozenset(exclude))
        except OSError:
            return {"operators": {}, "sets": {}}
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, aggregate(self.load().get("members") or {}, exclude))
        return self._cache[1]
