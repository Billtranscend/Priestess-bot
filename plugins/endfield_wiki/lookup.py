"""Resolve player input like "提丰", "提丰专武" or "寒夜" to an operator or weapon.

Matching is deliberately conservative because every "/xxx" message reaches this
plugin: exact names/aliases first, then a unique prefix, then a close typo match.
Anything ambiguous or unknown returns no result so the bot stays silent.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

try:  # homophone matching ("黎诺" -> 梨诺); lookups still work without it
    from pypinyin import Style, pinyin
except ImportError:  # pragma: no cover
    pinyin = None

SIGNATURE_SUFFIXES = ("专武", "专属武器")
MIN_PREFIX = 2
FUZZY_CUTOFF = 0.8
MAX_CANDIDATES = 6
# Variants of the same character share data; resolve names to the first listed id.
PREFERRED_IDS = ("chr_0002_endminm",)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


MAX_READINGS = 32  # cap on heteronym combinations per word


def to_pinyin(text: str) -> set[str]:
    """All toneless readings of a word, so heteronyms like 缪 (miao/miu) still match."""
    if not pinyin or not _has_cjk(text):
        return set()
    readings = {""}
    for options in pinyin(text, style=Style.NORMAL, heteronym=True):
        readings = {r + o for r in readings for o in dict.fromkeys(options)}
        if len(readings) > MAX_READINGS:
            readings = set(sorted(readings)[:MAX_READINGS])
    return readings


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s·・\-_.:：,，'\"“”‘’()（）]", "", text)


@dataclass
class Result:
    kind: str = ""  # "operator" | "weapon" | "no_signature" | "candidates"
    id: str = ""
    owner: str = ""  # operator id when the query asked for a signature weapon
    note: str = ""
    candidates: list[str] = field(default_factory=list)


class Resolver:
    def __init__(self, index: dict, aliases: dict) -> None:
        self.index = index
        self.keys: dict[str, tuple[str, str]] = {}  # normalized key -> (kind, id)
        self.pinyin: dict[str, set[tuple[str, str]]] = {}  # pinyin of CJK keys -> targets
        self.labels: dict[tuple[str, str], str] = {}
        name_to_op: dict[str, str] = {}
        for cid in sorted(index["operators"], key=lambda c: (c not in PREFERRED_IDS, c)):
            op = index["operators"][cid]
            name_to_op.setdefault(op["name"], cid)
        for name, cid in name_to_op.items():
            op = index["operators"][cid]
            self.labels[("operator", cid)] = name
            for key in (name, op["eng"], *aliases.get("operators", {}).get(name, {}).get("aliases", [])):
                self._add(key, ("operator", cid))
        name_to_wp = {}
        for wid, wp in index["weapons"].items():
            name_to_wp.setdefault(wp["name"], wid)
        for name, wid in name_to_wp.items():
            wp = index["weapons"][wid]
            self.labels[("weapon", wid)] = name
            for key in (name, wp["eng"], *aliases.get("weapons", {}).get(name, [])):
                self._add(key, ("weapon", wid))
        self.alias_cfg = aliases.get("operators", {})
        self.weapon_by_name = name_to_wp

    def _add(self, key: str, target: tuple[str, str]) -> None:
        key = normalize(key)
        if len(key) >= 1 and key not in self.keys:
            self.keys[key] = target
        if len(key) >= 2:
            for reading in to_pinyin(key):
                self.pinyin.setdefault(reading, set()).add(target)

    def _find(self, query: str, kinds: tuple[str, ...]) -> Result | None:
        pool = {k: v for k, v in self.keys.items() if v[0] in kinds}
        if query in pool:
            kind, eid = pool[query]
            return Result(kind=kind, id=eid)
        if len(query) >= 2 and (readings := to_pinyin(query)):
            targets = {t for r in readings for t in self.pinyin.get(r, set()) if t[0] in kinds}
            if len(targets) == 1:
                kind, eid = targets.pop()
                return Result(kind=kind, id=eid)
        if len(query) >= MIN_PREFIX:
            targets = {v for k, v in pool.items() if k.startswith(query)}
            if len(targets) == 1:
                kind, eid = targets.pop()
                return Result(kind=kind, id=eid)
            if 1 < len(targets) <= MAX_CANDIDATES:
                return Result(kind="candidates", candidates=sorted(self.labels[t] for t in targets))
            close = difflib.get_close_matches(query, pool.keys(), n=3, cutoff=FUZZY_CUTOFF)
            targets = {pool[k] for k in close}
            if len(targets) == 1:
                kind, eid = targets.pop()
                return Result(kind=kind, id=eid)
        return None

    def resolve(self, raw: str) -> Result | None:
        query = normalize(raw)
        if not query:
            return None
        for suffix in SIGNATURE_SUFFIXES:
            if query.endswith(suffix) and len(query) > len(suffix):
                found = self._find(query[: -len(suffix)], ("operator",))
                if not found or found.kind != "operator":
                    return found if found and found.kind == "candidates" else None
                op = self.index["operators"][found.id]
                cfg = self.alias_cfg.get(op["name"], {})
                wid = op.get("signature_weapon") or self.weapon_by_name.get(cfg.get("signature_weapon") or "")
                if wid:
                    return Result(kind="weapon", id=wid, owner=found.id)
                rec = self.weapon_by_name.get(cfg.get("recommended_weapon") or "")
                if rec:
                    return Result(kind="weapon", id=rec, owner=found.id, note="（非专武，推荐武器）")
                return Result(kind="no_signature", id=found.id)
        return self._find(query, ("operator", "weapon"))
