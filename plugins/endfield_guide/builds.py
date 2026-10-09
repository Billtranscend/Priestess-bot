"""Equipment builds of bound members' operators, for the usage statistics on the wiki cards.

The game has no table of recommended equipment, so the cards show what members actually run.
Once a day the collection (echoes.EchoStore.collect) also reads each member's Skland card and
keeps, per operator at MIN_LEVEL or above, the four equipped pieces, their sets and how far each
of a piece's three attributes was refined (精锻). Only aggregates are shown: how many members run
which set on an operator, the most common piece per slot for each of the most used sets, and the
average refinement of each attribute on that piece. Members who opted out of the statistics are neither fetched nor counted.
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
BUILD_SCHEMA = 2  # 2: refinement levels per piece


def extract(detail: dict) -> dict[str, dict]:
    """Operator name -> {"lv", "equips": [[piece, set, [refine level x3]] | None per slot]} from a card's `detail`."""
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
            chars[name] = {"lv": level, "equips": equips}
    return chars


def aggregate(members: dict, exclude: set[str] = frozenset()) -> dict:
    """{"operators": {name: {n, builds}}, "sets": {name: {n, operators}}} over builds with an active set.

    An operator's `builds` are its TOP_SETS most used sets: {"set", "count", "pieces": the most common piece per slot}.
    """
    by_operator: dict[str, dict] = {}
    by_set: dict[str, Counter] = {}
    refines: dict[tuple[str, str], list[list[int]]] = {}  # (operator, piece) -> refinement levels of every copy worn
    for qq, member in members.items():
        if qq in exclude:
            continue
        for name, build in (member.get("chars") or {}).items():
            suits = Counter(e[1] for e in build["equips"] if e and e[1])
            active = next((s for s, count in suits.most_common(1) if count >= SET_PIECES), None)
            if active is None:
                continue
            entry = by_operator.setdefault(name, {"n": 0, "sets": Counter(), "pieces": {}})
            entry["n"] += 1
            entry["sets"][active] += 1
            by_set.setdefault(active, Counter())[name] += 1
            body, arm, accessories = entry["pieces"].setdefault(active, [Counter() for _ in SLOT_LABELS])
            names = [equip[0] if equip else "" for equip in build["equips"]]
            body[names[0]] += bool(names[0])
            arm[names[1]] += bool(names[1])
            accessories[tuple(sorted(n for n in names[2:] if n))] += 1
            for equip in build["equips"]:
                if equip and len(equip) > 2:  # snapshots older than BUILD_SCHEMA 2 carry no refinement
                    refines.setdefault((name, equip[0]), []).append(equip[2])
    operators = {}
    for name, entry in by_operator.items():
        common = []
        for suit, wearers in entry["sets"].most_common(TOP_SETS):
            body, arm, accessories = entry["pieces"][suit]
            typical = [(label, *counter.most_common(1)[0]) for label, counter in ((SLOT_LABELS[0], +body), (SLOT_LABELS[1], +arm)) if counter]
            pair, count = accessories.most_common(1)[0]
            typical += [(SLOT_LABELS[2], piece, count) for piece in pair]
            pieces = []
            for slot, piece, count in typical:
                worn = refines.get((name, piece)) or []
                average = [round(sum(levels[i] for levels in worn) / len(worn), 1) for i in range(REFINE_SLOTS)] if worn else None
                pieces.append({"slot": slot, "name": piece, "count": count, "refine": average})
            common.append({"set": suit, "count": wearers, "pieces": pieces})
        operators[name] = {"n": entry["n"], "builds": common}
    sets = {
        name: {"n": sum(counter.values()), "operators": [(op, count, by_operator[op]["n"]) for op, count in counter.most_common(8)]}
        for name, counter in by_set.items()
    }
    return {"operators": operators, "sets": sets}


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

    def stats(self, exclude: set[str] = frozenset()) -> dict:
        """Aggregates, recomputed when the file or the opt-out list changes."""
        try:
            key = (self.data_file.stat().st_mtime, frozenset(exclude))
        except OSError:
            return {"operators": {}, "sets": {}}
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, aggregate(self.load().get("members") or {}, exclude))
        return self._cache[1]
