"""把 "提丰"、"提丰专武"、"寒夜"、"险关" 这样的玩家输入解析为干员、武器或装备套装。

匹配有意做得保守，因为每条 "/xxx" 消息都会到这个插件：
先精确匹配名字/别名，再找唯一前缀，最后才是相近的错字匹配。
有歧义或不认识的一律不返回结果，机器人就不出声。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

try:  # 同音匹配（"黎诺" -> 梨诺）；没有这个库查询照样能用
    from pypinyin import Style, pinyin
except ImportError:  # pragma: no cover
    pinyin = None

SIGNATURE_SUFFIXES = ("专武", "专属武器")
BUILD_SUFFIXES = ("推荐装备", "配装", "装备", "套装")  # "/庄方宜配装"：给出干员卡，配装统计就在上面
SET_SUFFIXES = ("套装效果", "套效果", "套装", "装备", "套")  # "/长息套装"、"/险关装备"：去掉后缀按套装再找一次
MIN_PREFIX = 2
MIN_PIECE = 2  # 套装名中间的片段至少几个字
FUZZY_CUTOFF = 0.8
MAX_CANDIDATES = 6
# 同一角色的不同版本共用数据；名字解析到下面列出的第一个 id。
PREFERRED_IDS = ("chr_0002_endminm",)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


MAX_READINGS = 32  # 每个词多音字组合数的上限


def to_pinyin(text: str) -> set[str]:
    """一个词的所有无声调读音，这样 缪（miao/miu）这样的多音字也能匹配上。"""
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
    kind: str = ""  # "operator" | "weapon" | "equip_set" | "no_signature" | "candidates"
    id: str = ""
    note: str = ""  # weapon：为什么选了这把；equip_set：查询的是哪件装备
    candidates: list[str] = field(default_factory=list)


class Resolver:
    def __init__(self, index: dict, aliases: dict) -> None:
        self.index = index
        self.keys: dict[str, tuple[str, str]] = {}  # 规范化后的键 -> (类型, id)
        self.pinyin: dict[str, set[tuple[str, str]]] = {}  # 中文键的拼音 -> 目标
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
        # 同名的武器（例如另一个版本的宏愿）：用有专属干员的那把，和“/管理员专武”打开的是同一张卡
        for wid, wp in sorted(index["weapons"].items(), key=lambda item: (not item[1].get("owner"), item[0])):
            name_to_wp.setdefault(wp["name"], wid)
        for name, wid in name_to_wp.items():
            wp = index["weapons"][wid]
            self.labels[("weapon", wid)] = name
            for key in (name, wp["eng"], *aliases.get("weapons", {}).get(name, [])):
                self._add(key, ("weapon", wid))
        self.pieces: dict[str, str] = {}  # 规范化后的装备名 -> 显示名；查单件装备会打开所属套装的卡片
        self.set_names: dict[str, tuple[str, str]] = {}  # 规范化后的套装名 -> 目标：按名字中间的片段找（“应龙”→50式应龙）
        for sid, equip_set in index.get("equip_sets", {}).items():
            self.labels[("equip_set", sid)] = equip_set["name"]
            self.set_names[normalize(equip_set["name"])] = ("equip_set", sid)
            for key in (equip_set["name"], *aliases.get("equip_sets", {}).get(equip_set["name"], [])):
                self._add(key, ("equip_set", sid))
            for piece in equip_set["pieces"]:
                self._add(piece["name"], ("equip_set", sid))
                self.pieces.setdefault(normalize(piece["name"]), piece["name"])
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
        prefixed = {v for k, v in pool.items() if k.startswith(query)} if len(query) >= MIN_PREFIX else set()
        if len(prefixed) == 1:  # 某个名字的开头（“小台”→小台风）比多音字的读音（台 si→祀）更可能是本意
            kind, eid = prefixed.pop()
            return Result(kind=kind, id=eid)
        if len(query) >= 2 and (readings := to_pinyin(query)):
            targets = {t for r in readings for t in self.pinyin.get(r, set()) if t[0] in kinds}
            if len(targets) == 1:
                kind, eid = targets.pop()
                return Result(kind=kind, id=eid)
        if len(query) >= MIN_PREFIX:
            if 1 < len(prefixed) <= MAX_CANDIDATES:  # 只有一个的上面已经返回
                return Result(kind="candidates", candidates=sorted(self.labels[t] for t in prefixed))
            close = difflib.get_close_matches(query, pool.keys(), n=3, cutoff=FUZZY_CUTOFF)
            targets = {pool[k] for k in close}
            if len(targets) == 1:
                kind, eid = targets.pop()
                return Result(kind=kind, id=eid)
        if "equip_set" in kinds and len(query) >= MIN_PIECE:
            # 套装名中间的连续几个字（群友常说“应龙套”，全名是 50式应龙）：只对上一套才算
            targets = {target for name, target in self.set_names.items() if query in name}
            if len(targets) == 1:
                kind, eid = targets.pop()
                return Result(kind=kind, id=eid)
            if 1 < len(targets) <= MAX_CANDIDATES:
                return Result(kind="candidates", candidates=sorted(self.labels[t] for t in targets))
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
                wid = op.get("signature_weapon")  # 专武来自 AKEData 的索引
                if wid:
                    return Result(kind="weapon", id=wid)
                rec = self.weapon_by_name.get(cfg.get("recommended_weapon") or "")
                if rec:
                    return Result(kind="weapon", id=rec, note="（非专武，推荐武器）")
                return Result(kind="no_signature", id=found.id)
        found = self._find(query, ("operator", "weapon", "equip_set"))
        if found is None:
            for suffix in BUILD_SUFFIXES:
                if query.endswith(suffix) and len(query) > len(suffix):
                    owner = self._find(query[: -len(suffix)], ("operator",))
                    if owner and owner.kind in ("operator", "candidates"):
                        return owner
            for suffix in SET_SUFFIXES:  # 不是干员：也许是套装的短名字加后缀（短名字模糊匹配碰不上）
                if query.endswith(suffix) and len(query) > len(suffix):
                    gear = self._find(query[: -len(suffix)], ("equip_set",))
                    if gear and gear.kind in ("equip_set", "candidates"):
                        return gear
            return None
        elif found.kind == "equip_set" and self.keys.get(query) == ("equip_set", found.id):
            found.note = self.pieces.get(query, "")
        return found
