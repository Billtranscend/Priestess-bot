"""AKEData (https://www.akedata.wiki/) sync and compact index builder for Endfield wiki lookups.

The site publishes unpacked game tables on data.akedata.wiki. We poll its small
manifest with conditional requests, download only the tables we need when the
version changes, and reduce them to one compact index.json used for lookups.

Stage enemies also need the scene files next to the tables: the spawner configs
say which enemy variants a stage spawns and which buffs they are born with, and
the buff files hold what those buffs do to the enemy's attributes.
"""

from __future__ import annotations

import ast
import asyncio
import json
import operator
import re
import shutil
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

DATA_BASE = "https://data.akedata.wiki"
MANIFEST_URL = f"{DATA_BASE}/manifest.json"
MAPS_URL = "https://www.akedata.wiki/public/CH/maps.json"
ASSET_INDEX_URL = f"{DATA_BASE}/asset-sync-index.json"  # lists every file under public/Json
JSON_BASE = f"{DATA_BASE}/public/Json"
STAGE_FETCHES = 4  # concurrent downloads of the small stage files
SAFE_PATH = re.compile(r"^(?:SpawnerConfig/\w[\w.-]*|BuffData)/\w[\w.-]*\.json$")
IMAGE_BASE = f"{DATA_BASE}/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
USER_AGENT = "QQBot-EndfieldWiki/1.0 (non-commercial group bot; data from AKEData)"
TABLES = (
    "I18nTextTable_CN",
    "CharacterTable",
    "CharGrowthTable",
    "CharacterPotentialTable",
    "PotentialTalentEffectTable",
    "SkillPatchTable",
    "CharWpnRecommendTable",
    "WeaponBasicTable",
    "WeaponUpgradeTemplateTable",
    "ItemTable",
    "EquipTable",
    "EquipSuitTable",
    "DungeonTable",
    "DungeonSeriesTable",
    "SeasonTowerTable",
    "SeasonTowerGameGroupTable",
    "SeasonTowerDungeonTable",
    "TimeRangeTable",
    "EnemyTemplateDisplayInfoTable",
    "EnemyTable",
    "EnemyAttributeTemplateTable",
    "ActivityTable",
    "GachaCharPoolTable",
    "GachaWeaponPoolTable",
    "GachaCharPoolTypeTable",
    "GachaWeaponPoolTypeTable",
    "RewardTable",
    "BattlePassTaskTable",
    "BattlePassConditionTable",
    "ActivityWeeklyTaskTable",
    "ActivityWeeklyTaskMileStoneTable",
)
INDEX_SCHEMA = 16  # bump to force a rebuild when the index layout changes
# Enemy resistance attribute ids (AttributeShowConfigTable), in the in-game display order.
ENEMY_RESISTANCES = ((94, "物理"), (98, "灼热"), (97, "电磁"), (96, "寒冷"), (95, "自然"), (99, "超域"))
TOWER_DIFFICULTIES = {"1": "普通", "2": "困难", "3": "残酷"}
SKILL_TYPES = {0: "普通攻击", 1: "战技", 3: "连携技", 2: "终结技"}
SKILL_ORDER = (0, 1, 3, 2)
BASE_ATTRS = (("1", "生命值"), ("2", "攻击力"))  # base DEF is always 0 in Endfield; defense comes from gear
STAT_LEVEL = 90  # current player level cap; tables extend further
EQUIP_PARTS = {0: "护甲", 1: "护手", 2: "配件"}
EQUIP_FLAT_ATTRS = {1, 2, 3, 39, 40, 41, 42, 87}  # HP, ATK, DEF, the four abilities, 源石技艺强度: plain numbers, the rest are ratios


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(120.0, connect=20.0),
        follow_redirects=True,
    )


async def fetch_manifest(client: httpx.AsyncClient, etag: str | None) -> tuple[dict | None, str | None]:
    """Return (manifest, etag); manifest is None when unchanged (HTTP 304)."""
    headers = {"If-None-Match": etag} if etag else {}
    response = await client.get(MANIFEST_URL, headers=headers)
    if response.status_code == 304:
        return None, etag
    response.raise_for_status()
    return response.json(), response.headers.get("ETag")


async def download_tables(client: httpx.AsyncClient, table_path: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in TABLES:
        response = await client.get(f"{DATA_BASE}/{table_path}/{name}.json")
        response.raise_for_status()
        (target / f"{name}.json").write_bytes(response.content)
    response = await client.get(MAPS_URL)
    response.raise_for_status()
    (target / "maps.json").write_bytes(response.content)
    await download_stage_data(client, target)


def endgame_stage_ids(series: dict, tower_groups: dict) -> list[str]:
    stage_ids = [star["gameId"] for group in tower_groups.values() for star in group.get("stars", {}).values() if star.get("gameId")]
    for row in series.values():
        if row.get("gameCategory") == "dungeon_highdifficulty":
            stage_ids += row.get("includeDungeonIds", [])
    return stage_ids


async def download_stage_data(client: httpx.AsyncClient, target: Path) -> None:
    """Spawner configs of the endgame scenes plus every buff their enemies are born with."""

    def table(name: str) -> dict:
        return json.loads((target / f"{name}.json").read_text("utf-8"))

    dungeons, enemies = table("DungeonTable"), table("EnemyTable")
    stage_ids = [s for s in endgame_stage_ids(table("DungeonSeriesTable"), table("SeasonTowerGameGroupTable")) if s in dungeons]
    scenes = {dungeons[s].get("sceneId") for s in stage_ids}
    response = await client.get(ASSET_INDEX_URL)
    response.raise_for_status()
    files = response.json()["datasets"]["json"]["files"]
    limit = asyncio.Semaphore(STAGE_FETCHES)

    async def fetch(path: str) -> dict:
        async with limit:
            response = await client.get(f"{JSON_BASE}/{quote(path)}")
            response.raise_for_status()
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(response.content)
        return response.json()

    paths = [p for p in files if SAFE_PATH.match(p) and p.startswith("SpawnerConfig/") and p.split("/")[1] in scenes]
    configs = await asyncio.gather(*(fetch(p) for p in paths))
    enemy_ids = {e for s in stage_ids for e in dungeons[s].get("enemyIds", [])}
    buff_ids = set()
    for config in configs:
        for entry in config.get("enemyLibrary") or []:
            enemy_ids.add(entry.get("enemyId"))
            buff_ids.update(b.get("buffId") for b in entry.get("bornBuffList") or [])
    for enemy_id in enemy_ids:
        buff_ids.update(enemies.get(enemy_id, {}).get("bornBuffs") or [])
    paths = [p for p in (f"BuffData/{b}.json" for b in sorted(filter(None, buff_ids))) if p in files and SAFE_PATH.match(p)]
    await asyncio.gather(*(fetch(p) for p in paths))


# ── description formatting ─────────────────────────────────────────────

_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _safe_eval(expr: str, values: dict[str, float]) -> float:
    """Evaluate +-*/ arithmetic over numbers and known variable names only."""

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            return values[node.id.lower()]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](walk(node.left), walk(node.right))
        raise ValueError("unsupported expression")

    return walk(ast.parse(expr, mode="eval"))


def _format_number(value: float, fmt: str) -> str:
    if "%" in fmt:
        decimals = len(fmt.split(".")[1].rstrip("%")) if "." in fmt else 0
        return f"{value * 100:.{decimals}f}%"
    if "." in fmt:
        return f"{value:.{len(fmt.split('.')[1])}f}"
    if "0" in fmt:
        return str(round(value))
    return f"{value:g}"


def fill_placeholders(text: str, values: dict[str, float]) -> str:
    def replace(match: re.Match) -> str:
        expr, _, fmt = match.group(1).partition(":")
        try:
            return _format_number(_safe_eval(expr.replace(" ", ""), values), fmt.strip())
        except (KeyError, ValueError, SyntaxError, ZeroDivisionError):
            return "?"

    return re.sub(r"\{([^{}]+)\}", replace, text)


def strip_live_values(text: str) -> str:
    """Drop the bracketed parts that show a value of the player's own squad, e.g. 智识值({floor:deck_wisd:0})."""
    return re.sub(r"[（(][^（）()]*\{floor:[^{}]+\}[^（）()]*[）)]", "", text)


def rich_to_html(text: str) -> str:
    """Convert game rich text (<@style>..</>, <#term>..</>, \\n) to safe HTML."""
    out, stack, pos = [], [], 0
    for match in re.finditer(r"<([@#])([^<>]*)>|</>|<image[^>]*>", text):
        out.append(escape(text[pos : match.start()]))
        pos = match.end()
        token = match.group(0)
        if token == "</>":
            if stack:
                out.append("</span>")
                stack.pop()
        elif token.startswith("<image"):
            continue
        else:
            kind, name = match.group(1), match.group(2)
            if kind == "#":
                cls = "term"
            elif "info" in name:
                cls = "info"
            elif "vup" in name or "num" in name:
                cls = "up"
            elif "vdown" in name:
                cls = "down"
            else:
                cls = "key"
            out.append(f'<span class="{cls}">')
            stack.append(cls)
    out.append(escape(text[pos:]))
    out.extend("</span>" for _ in stack)
    return "".join(out).replace("\\n", "<br>").replace("\n", "<br>")


# ── index building ─────────────────────────────────────────────────────


def build_index(table_dir: Path, version: dict, out_file: Path) -> dict[str, int]:
    def load(name: str) -> Any:
        return json.loads((table_dir / f"{name}.json").read_text("utf-8"))

    i18n = load("I18nTextTable_CN")
    maps = load("maps")

    def text(obj: Any) -> str:
        if isinstance(obj, dict):
            return i18n.get(str(obj.get("id")), "") or obj.get("text", "") or ""
        return obj if isinstance(obj, str) else ""

    chars, growth = load("CharacterTable"), load("CharGrowthTable")
    potentials, effects = load("CharacterPotentialTable"), load("PotentialTalentEffectTable")
    skills, recommend = load("SkillPatchTable"), load("CharWpnRecommendTable")
    weapons_basic, upgrades, items = load("WeaponBasicTable"), load("WeaponUpgradeTemplateTable"), load("ItemTable")
    attr_names, attr_keys = maps["ATTR_MAP"], maps["ATTR_MAP_EN"]
    param_keys = maps["param_type_map"]

    def effect_values(effect: dict) -> dict[str, float]:
        values: dict[str, float] = {}
        for data in effect.get("dataList", []):
            for holder in ("attachBuff", "attachSkill"):
                for entry in data.get(holder, {}).get("blackboard", []):
                    values[entry["key"].lower()] = entry["value"]
            bb = data.get("skillBbModifier", {})
            if bb.get("bbKey"):
                values[bb["bbKey"].lower()] = bb.get("floatValue", 0.0)
            attr = data.get("attrModifier", {})
            if attr.get("attrType"):
                key = attr_keys.get(str(attr["attrType"]), "")
                if key:
                    values[key.lower()] = attr.get("attrValue", 0.0)
            param = data.get("skillParamModifier", {})
            if param.get("paramType"):
                values[param_keys.get(str(param["paramType"]), "").lower()] = param.get("paramValue", 0.0)
        return values

    def max_level_values(skill_ids: list[str]) -> dict[str, float]:
        values: dict[str, float] = {}
        for skill_id in skill_ids:
            bundle = skills.get(skill_id, {}).get("SkillPatchDataBundle", [])
            if bundle:
                top = bundle[-1]
                for entry in top.get("blackboard", []):
                    values[entry["key"].lower()] = entry["value"]  # a later skill of the group wins, as on the AKEData site
                values.setdefault("cooldown", top.get("coolDown", 0.0))
                values.setdefault("costvalue", top.get("costValue", 0.0))
        return values

    # Signature weapons: first tier-1 recommendation of each 6★ operator (verified against community guides).
    signature = {
        cid: rec["weaponIds1"][0]
        for cid, rec in recommend.items()
        if chars.get(cid, {}).get("rarity") == 6 and rec.get("weaponIds1")
    }

    operators: dict[str, dict] = {}
    for cid, char in chars.items():
        name = text(char.get("name"))
        if not name:
            continue
        grow = growth.get(cid, {})
        levels = [{str(a["attrType"]): a["attrValue"] for a in row["Attribute"]["attrs"]} for row in char.get("attributes", [])]
        top = next((a for a in levels if round(a.get("0", 0)) == STAT_LEVEL), max(levels, key=lambda a: a.get("0", 0)) if levels else {})
        stats = [(label, round(top.get(key, 0))) for key, label in BASE_ATTRS]
        stats += [(attr_names.get(k, k), round(top.get(k, 0))) for k in ("39", "40", "41", "42") if k in top]

        skill_rows = []
        groups = sorted(grow.get("skillGroupMap", {}).values(), key=lambda g: SKILL_ORDER.index(g.get("skillGroupType", 0)) if g.get("skillGroupType") in SKILL_ORDER else 9)
        for group in groups:
            values = max_level_values(group.get("skillIdList", []))
            # A skill may read differently in each of two forms of the operator (诀: 阵诀·智 / 阵诀·意);
            # its common description can then be empty, with everything said in the forms.
            forms = [
                {
                    "name": text(group.get(f"conditionName{n}")),
                    "when": rich_to_html(strip_live_values(text(group.get(f"conditionDesc{n}"))).removeprefix("/*").removesuffix("*/").strip()),
                    # a short form text opens with the form's own name again ("阵诀·智：\n- ..."), already the heading here
                    "desc": rich_to_html(fill_placeholders(
                        re.sub(r"^<@[^<>]*>%s</>[：:]\n" % re.escape(text(group.get(f"conditionName{n}"))), "", text(group.get(f"conditionPostDesc{n}"))), values)),
                }
                for n in (1, 2)
                if group.get(f"conditionId{n}")
            ]
            skill_rows.append(
                {
                    "type": SKILL_TYPES.get(group.get("skillGroupType"), "技能"),
                    "name": text(group.get("name")),
                    "desc": rich_to_html(fill_placeholders(text(group.get("desc")), values)),
                    "forms": [form for form in forms if form["desc"]],
                    "icon": group.get("icon", ""),
                }
            )

        talents: dict[str, dict] = {}
        for node in grow.get("talentNodeMap", {}).values():
            info = node.get("passiveSkillNodeInfo", {})
            effect = effects.get(info.get("talentEffectId", ""))
            if not effect:
                continue
            tname = text(info.get("name"))
            if tname not in talents or info.get("level", 0) >= talents[tname]["level"]:
                talents[tname] = {
                    "level": info.get("level", 0),
                    "name": tname,
                    "desc": rich_to_html(fill_placeholders(text(effect.get("desc")), effect_values(effect))),
                }

        potential_rows = []
        for bundle in potentials.get(cid, {}).get("potentialUnlockBundle", []):
            effect = effects.get(bundle.get("potentialEffectId", ""), {})
            potential_rows.append(
                {
                    "level": bundle.get("level"),
                    "name": text(bundle.get("name")),
                    "desc": rich_to_html(fill_placeholders(text(effect.get("desc")), effect_values(effect))),
                }
            )

        operators[cid] = {
            "id": cid,
            "name": name,
            "eng": char.get("engName", ""),
            "rarity": char.get("rarity", 0),
            "profession": maps["profession_id_map"].get(str(char.get("profession")), ""),
            "weapon_type": maps["weapon_id_map"].get(str(char.get("weaponType")), ""),
            "element": maps["char_type_map"].get(char.get("charTypeId", ""), ""),
            "main_attr": attr_names.get(str(char.get("mainAttrType")), ""),
            "sub_attr": attr_names.get(str(char.get("subAttrType")), ""),
            "department": char.get("department", ""),
            "stats": stats,
            "skills": skill_rows,
            "talents": sorted(talents.values(), key=lambda t: t["name"]),
            "potentials": potential_rows,
            "signature_weapon": signature.get(cid),
            "recommended": [w for w in recommend.get(cid, {}).get("weaponIds1", [])],
        }

    owner_of = {wid: cid for cid, wid in signature.items() if not cid.startswith("chr_9000")}
    weapons: dict[str, dict] = {}
    for wid, basic in weapons_basic.items():
        item = items.get(wid, {})
        name = text(item.get("name"))
        if not name:
            continue
        curve = upgrades.get(basic.get("levelTemplateId", ""), {}).get("list", [])
        max_atk = max((row.get("baseAtk", 0) for row in curve), default=0)
        skill_rows = []
        for skill_id in basic.get("weaponSkillList", []):
            bundle = skills.get(skill_id, {}).get("SkillPatchDataBundle", [])
            if not bundle:
                continue
            levels = []
            for rank in bundle:
                values = {e["key"].lower(): e["value"] for e in rank.get("blackboard", [])}
                levels.append(rich_to_html(fill_placeholders(text(rank.get("description")), values)))
            skill_rows.append({"name": text(bundle[0].get("skillName")), "levels": levels})
        owner = owner_of.get(wid)
        weapons[wid] = {
            "id": wid,
            "name": name,
            "eng": text(basic.get("engName")) or basic.get("engName", ""),
            "rarity": basic.get("rarity", 0),
            "type": maps["weapon_id_map"].get(str(basic.get("weaponType")), ""),
            "max_atk": max_atk,
            "max_lv": basic.get("maxLv", 0),
            "icon": item.get("iconId") or wid,
            "deco": text(item.get("decoDesc")),
            "owner": operators[owner]["name"] if owner in operators else "",
            "skills": skill_rows,
        }

    index = {
        "schema": INDEX_SCHEMA,
        "equip_sets": build_equip_sets(load, text, maps),
        "equip_items": build_equip_items(load, text, maps),
        "endgame": build_endgame(load, text, maps, table_dir),
        "calendar": build_calendar(load, text),
        "version": version.get("id", ""),
        "published_at": version.get("publishedAt", ""),
        "operators": operators,
        "weapons": weapons,
    }
    tmp = out_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(out_file)
    stages = [s for g in index["endgame"]["tower"].values() for s in g["stages"]]
    stages += [s for m in index["endgame"]["monument"] for g in m["groups"] for s in g["stages"]]
    foes = [foe for s in stages for foe in s["enemies"]]
    # "foes_plain" should stay near zero: a jump means the spawner files no longer match the stages.
    return {"operators": len(operators), "weapons": len(weapons), "foes": len(foes), "foes_plain": sum(1 for f in foes if f.get("plain"))}


def build_endgame(load, text, maps: dict, table_dir: Path) -> dict:
    """Stage index for 战争回响 (season tower) and 影拓丰碑 (high-difficulty series)."""
    dungeons, series = load("DungeonTable"), load("DungeonSeriesTable")
    enemies = load("EnemyTemplateDisplayInfoTable")
    enemy_rows, enemy_attrs = load("EnemyTable"), load("EnemyAttributeTemplateTable")
    tower_groups, tower_seasons, tower_buffs = load("SeasonTowerGameGroupTable"), load("SeasonTowerTable"), load("SeasonTowerDungeonTable")
    time_ranges = load("TimeRangeTable")
    attr_ids = {name: int(key) for key, name in maps["ATTR_MAP_EN"].items()}
    attr_names = {int(key): name for key, name in maps["ATTR_MAP"].items() if str(key).isdigit()}
    formulas = {name: int(key) for key, name in maps["MODIFIER_TYPE_MAP"].items()}
    scene_use = Counter(
        (dungeons[s].get("sceneId"), level)
        for s in endgame_stage_ids(series, tower_groups)
        if s in dungeons
        for level in set(dungeons[s].get("enemyLevels", []))
    )
    spawner_cache: dict[str, list[dict]] = {}
    buff_cache: dict[str, dict] = {}

    def base_name(stage_id: str) -> str:
        return text(dungeons.get(stage_id, {}).get("dungeonName")).split("·")[0].strip()

    def spawners(scene: str) -> list[dict]:
        if scene not in spawner_cache:
            folder = table_dir / "SpawnerConfig" / scene
            spawner_cache[scene] = [json.loads(p.read_text("utf-8")) for p in sorted(folder.glob("*.json"))] if scene and folder.is_dir() else []
        return spawner_cache[scene]

    def effects(buff_id: str, blackboard: list | None = None) -> tuple[list[dict], dict[int, float]]:
        if buff_id not in buff_cache:
            file = table_dir / "BuffData" / f"{buff_id}.json"
            buff_cache[buff_id] = json.loads(file.read_text("utf-8")) if file.is_file() else {}
        return buff_effects(buff_cache[buff_id], blackboard, attr_ids, formulas)

    def template_of(enemy_id: str) -> str:
        return enemy_rows.get(enemy_id, {}).get("templateId") or enemy_id

    def buff(buff_id: str) -> dict:
        effects(buff_id)
        return buff_cache[buff_id]

    def scenery(entry: dict) -> bool:
        """Spawned without a health bar: part of the stage's mechanics, not an enemy to defeat."""
        born = list(enemy_rows.get(entry["enemyId"], {}).get("bornBuffs") or []) + [b.get("buffId", "") for b in entry.get("bornBuffList") or []]
        return any(hides_health_bar(buff(buff_id)) for buff_id in born)

    def display(enemy_id: str) -> tuple[str, str]:
        probe = template_of(enemy_id)
        name = text(enemies.get(probe, {}).get("name"))
        if not name:  # no table row: stage-specific variants append suffixes, e.g. eny_0118_klhog_hdg026
            probe = enemy_id
            while probe and not (name := text(enemies.get(probe, {}).get("name"))):
                probe = probe.rpartition("_")[0] if probe.count("_") > 2 else ""
        return (name, probe) if name else (enemy_id, "")

    def spawned(row: dict, pairs: list[tuple[str, int]]) -> list[dict]:
        """Spawner library entries of this stage: an enemy variant, its level and the buffs it is born with."""
        scene, levels = row.get("sceneId", ""), {level for _, level in pairs}
        templates = {template_of(enemy_id) for enemy_id, _ in pairs}
        shared = any(scene_use[(scene, level)] > 1 for level in levels)
        library = []
        for config in spawners(scene):
            used = {
                action.get("libraryKey")
                for wave in (config.get("waveMap") or {}).values()
                for group in (wave.get("groupMap") or {}).values()
                for action in (group.get("actionMap") or {}).values()
            } - {None, ""}
            entries = [e for e in config.get("enemyLibrary") or [] if e.get("enemyLevel") in levels and (not used or e.get("key") in used)]
            # All 战争回响 stages share one scene; there a spawner is this stage's only when the
            # stage lists everything it spawns.
            if shared and any(template_of(e["enemyId"]) not in templates for e in entries):
                continue
            library += entries
        return library

    def stage(stage_id: str, difficulty: str) -> dict:
        row = dungeons.get(stage_id, {})
        pairs = list(dict.fromkeys(zip(row.get("enemyIds", []), row.get("enemyLevels", []))))
        listed = {enemy_id for enemy_id, _ in pairs}
        # Scenery keeps its level across difficulties, so it is looked up in the whole scene.
        props = [e for config in spawners(row.get("sceneId", "")) for e in config.get("enemyLibrary") or [] if scenery(e)]
        library = [e for e in spawned(row, pairs) if not scenery(e)]
        rows: dict[tuple[str, int], tuple[str, list[dict]]] = {}  # (variant, level) -> (enemy it is shown as, its spawner entries)
        taken = []
        for enemy_id, level in pairs:
            same_level = [e for e in library if e["enemyLevel"] == level]
            # The stage lists either the exact variant or only the plain enemy its variants derive from.
            found = [e for e in same_level if e["enemyId"] == enemy_id] or [
                e for e in same_level if e["enemyId"] not in listed and template_of(e["enemyId"]) == template_of(enemy_id)
            ]
            taken += found
            if not found and any(template_of(e["enemyId"]) == template_of(enemy_id) for e in props):
                continue  # listed by the game, but it only stands in the stage as scenery
            for variant in list(dict.fromkeys(e["enemyId"] for e in found)) or [enemy_id]:
                rows[(variant, level)] = (enemy_id, [e for e in found if e["enemyId"] == variant])
        for entry in library:  # spawned without being on the stage's enemy list
            if not any(entry is e for e in taken):
                rows.setdefault((entry["enemyId"], entry["enemyLevel"]), (entry["enemyId"], []))[1].append(entry)
        foes = []
        for (variant, level), (shown, entries) in rows.items():
            born: list[dict] = []
            for entry in entries:  # one row per variant as on the AKEData site: the buffs of all its spawns, each buff once
                born += [b for b in entry.get("bornBuffList") or [] if b.get("buffId") not in {x.get("buffId") for x in born}]
            name, icon = display(shown)
            enemy = enemy_rows.get(variant, {})
            own = [effects(b) for b in enemy.get("bornBuffs") or []]
            placed = [effects(b.get("buffId", ""), b.get("blackboard")) for b in born]
            foe = {"name": name, "icon": icon, "level": level, **enemy_stats(enemy, enemy_attrs, level, own + placed)}
            # What the numbers above already contain, worded as on the AKEData site: the enemy
            # variant's own bonuses ("born") and those the stage's spawner adds ("buff").
            born_notes = modifier_notes(list(enemy.get("attrModifiers") or []) + [m for mods, _ in own for m in mods], attr_names)
            buff_notes = modifier_notes([m for mods, _ in placed for m in mods], attr_names) + override_notes(placed, attr_names)
            if born_notes:
                foe["born"] = born_notes
            if buff_notes:
                foe["buff"] = buff_notes
            if not entries:  # not placed by a spawner (summoned in the fight): the enemy's own values only
                foe["plain"] = True
            if foe not in foes:
                foes.append(foe)
        buff = text(tower_buffs.get(stage_id, {}).get("specialBuffDesc"))
        params = {p["key"].lower(): p["value"] for p in row.get("paramList") or [] if "key" in p}
        return {
            "id": stage_id,
            "difficulty": difficulty,
            "recommend_lv": row.get("recommendLv", 0),
            "feature": rich_to_html(fill_placeholders(text(row.get("featureDesc")), params)),
            "special_buff": rich_to_html(fill_placeholders(buff, params)) if buff else "",
            "enemies": foes,
        }

    tower = {}
    for group_id, group in tower_groups.items():
        stars = group.get("stars", {})
        first = stars.get("1", {}).get("gameId", group_id)
        tower[group_id] = {
            "id": group_id,
            "name": base_name(first),
            "mode": "战争回响",
            "cover": group.get("icon", ""),
            "stages": [stage(stars[k]["gameId"], TOWER_DIFFICULTIES.get(k, k)) for k in sorted(stars) if stars[k].get("gameId")],
        }

    seasons = []
    for season_id, season in sorted(tower_seasons.items(), key=lambda kv: int(kv[0])):
        weeks = []
        for week_id, week in sorted(season.get("weeks", {}).items(), key=lambda kv: int(kv[0])):
            ranges = time_ranges.get(f"time_activity_seasontower_season_{season_id}_week_{week_id}", {}).get("timeRangeList", [])
            weeks.append(
                {
                    "week": int(week_id),
                    "name": text(week.get("weekShowName")),
                    "groups": week.get("includeGameIdList", []),
                    "open": ranges[0]["openTime"] if ranges else "",
                    "close": ranges[0]["closeTime"] if ranges else "",
                }
            )
        seasons.append({"id": int(season_id), "name": text(season.get("name")), "weeks": weeks})

    monument = []
    for series_id, row in sorted(series.items(), key=lambda kv: kv[1].get("sortId", 0)):
        if row.get("gameCategory") != "dungeon_highdifficulty":
            continue
        stage_ids = row.get("includeDungeonIds", [])
        groups = []
        for base in [sid for sid in stage_ids if not sid.endswith("_s")]:
            variants = [stage(base, "普通")] + ([stage(base + "_s", "苦难")] if base + "_s" in stage_ids else [])
            groups.append(
                {
                    "id": base,
                    "name": base_name(base),
                    "mode": "影拓丰碑",
                    "series": text(row.get("name")),
                    "cover": dungeons.get(base, {}).get("dungeonPicPath", ""),
                    "stages": variants,
                }
            )
        monument.append({"id": series_id, "name": text(row.get("name")), "groups": groups})

    return {"tower": tower, "tower_seasons": seasons, "monument": monument}


def _blackboard(rows: list | None) -> dict[str, float]:
    """Blackboard rows keep their number under one of several typed keys."""
    values = {}
    for row in rows or []:
        values[row.get("key")] = next((row[k] for k in ("valueFloat", "valueDouble", "valueInt", "valueLong", "value") if row.get(k) is not None), 0.0)
    return values


def buff_effects(buff: dict, blackboard: list | None, attr_ids: dict[str, int], formulas: dict[str, int]) -> tuple[list[dict], dict[int, float]]:
    """What a buff does to its owner's attributes for as long as it lasts: (modifiers, raw overrides).

    Modifiers have the shape of EnemyTable.attrModifiers. Overrides replace the raw attribute
    outright, which is how stages level all elemental resistances to one value. `blackboard`
    holds the values the spawner passes in place of the buff's defaults.
    """
    values = {**_blackboard(buff.get("blackboard")), **_blackboard(blackboard)}

    def number(param: dict) -> float:
        if param.get("useBlackboardKey") and param.get("blackboardKey"):
            return values.get(param["blackboardKey"], param.get("value", 0.0))
        return param.get("value", 0.0)

    modifiers = []
    for mod in (buff.get("attributeModifier") or {}).get("attributeModifiers") or []:
        attr, formula = attr_ids.get(mod.get("attributeType")), formulas.get(mod.get("formulaItem"))
        if attr is not None and formula is not None:
            modifiers.append({"attrType": attr, "modifierType": formula, "attrValue": number(mod.get("param") or {})})
    overrides = {}
    for event in buff.get("buffEventAction") or []:
        if event.get("buffEvent") != "DuringBuffEnable":
            continue
        for group in event.get("actions") or []:
            for action in group.get("actionData") or []:
                if "OverrideRawAttributeAction" not in action.get("$type", ""):
                    continue
                for item in action.get("attributeOverrides") or []:
                    attr = attr_ids.get(item.get("attributeType"))
                    if attr is not None:
                        overrides[attr] = number(item.get("overrideValue") or {})
    return modifiers, overrides


# modifierType -> operation, in the order the AKEData site applies them: Base* types first, a
# percentage is x(1 + value) and several of them multiply. The site is the reference these numbers
# are checked against, so stacking follows it exactly.
MODIFIER_STEPS = ((5, "add"), (6, "percent"), (7, "add"), (8, "factor"), (3, "add"), (4, "factor"), (0, "add"), (1, "percent"))
MODIFIER_KINDS = dict(MODIFIER_STEPS)
LEGACY_RESISTANCES = {80, 81, 82, 83, 84, 85}  # *DmgResistScalar: the site leaves these out of its bonus lines
BALANCED_RESISTANCES = (94, 95, 96, 97, 98)  # the five elements a stage levels to one value (超域 stays as it is)


def hides_health_bar(buff: dict) -> bool:
    """A permanent buff that force-hides its owner's health bar."""
    return buff.get("lifeType") == "Infinity" and any(
        "ForceHideHeadBarAction" in action.get("$type", "")
        for event in buff.get("buffEventAction") or []
        if event.get("buffEvent") == "DuringBuffEnable"
        for group in event.get("actions") or []
        for action in group.get("actionData") or []
    )


def modified(value: float, modifiers: list[dict], attr: int) -> float:
    """Apply the attribute modifiers of one attribute (see MODIFIER_STEPS)."""
    for kind, step in MODIFIER_STEPS:
        for mod in modifiers:
            if mod.get("attrType") != attr or mod.get("modifierType") != kind:
                continue
            amount = mod.get("attrValue", 0.0)
            value = value + amount if step == "add" else value * (1 + amount) if step == "percent" else value * amount
    return value


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def modifier_notes(modifiers: list[dict], attr_names: dict[int, str]) -> list[str]:
    """Attribute modifiers as "最大生命值 +60%" / "失衡值上限 +40", one per attribute and kind.

    Same grouping and wording as the AKEData site: percentages of one attribute compound,
    factors multiply and are shown as a percentage too, the rest adds up.
    """
    merged: dict[tuple[int, int], float] = {}
    for mod in modifiers:
        key, amount = (mod.get("attrType"), mod.get("modifierType")), mod.get("attrValue", 0.0)
        if key[0] in LEGACY_RESISTANCES or key[0] not in attr_names or key[1] not in MODIFIER_KINDS:
            continue
        step = MODIFIER_KINDS[key[1]]
        if key not in merged:
            merged[key] = amount
        elif step == "percent":
            merged[key] = (1 + merged[key]) * (1 + amount) - 1
        elif step == "factor":
            merged[key] *= amount
        else:
            merged[key] += amount
    notes = []
    for (attr, kind), amount in merged.items():
        step = MODIFIER_KINDS[kind]
        amount = amount - 1 if step == "factor" else amount
        shown = f"{_number(amount * 100)}%" if step in ("percent", "factor") else _number(amount)
        notes.append(f"{attr_names[attr]} {'+' if amount > 0 else ''}{shown}")
    return notes


def override_notes(effects: list[tuple[list[dict], dict[int, float]]], attr_names: dict[int, str]) -> list[str]:
    """Raw attributes a stage sets outright; levelled elemental resistances become one line."""
    overrides = {attr: value for _, sets in effects for attr, value in sets.items()}
    levelled = {overrides.get(attr) for attr in BALANCED_RESISTANCES}
    notes = []
    if len(levelled) == 1 and None not in levelled:
        notes.append(f"属性抗性统一为 {_number(levelled.pop())}")
        overrides = {attr: value for attr, value in overrides.items() if attr not in BALANCED_RESISTANCES}
    return notes + [f"{attr_names[attr]} 固定为 {_number(value)}" for attr, value in overrides.items() if attr in attr_names]


def enemy_stats(row: dict, templates: dict, level: int, effects: list[tuple[list[dict], dict[int, float]]] | tuple = ()) -> dict:
    """HP / ATK / DEF / 失衡值上限 and non-zero resistances of an enemy at the stage level.

    Counts the enemy's own attrModifiers and the `effects` (see buff_effects) of the buffs it is
    born with in the stage. Stage-wide buffs (special_buff text) are not applied.
    """
    template = templates.get(row.get("attrTemplateId", ""), {})
    per_level = template.get("levelDependentAttributes") or []
    if not 0 < level <= len(per_level):
        return {}
    base = {a["attrType"]: a["attrValue"] for a in (template.get("levelIndependentAttributes") or {}).get("attrs", [])}
    base.update({a["attrType"]: a["attrValue"] for a in per_level[level - 1]["attrs"]})
    modifiers = list(row.get("attrModifiers") or [])
    for buff_modifiers, overrides in effects:
        modifiers += buff_modifiers
        base.update(overrides)
    stats = {key: round(modified(base.get(attr, 0.0), modifiers, attr)) for attr, key in ((1, "hp"), (2, "atk"), (3, "def"), (20, "poise"))}
    stats["res"] = [[label, value] for attr, label in ENEMY_RESISTANCES if (value := round(modified(base.get(attr, 0.0), modifiers, attr)))]
    return stats


def stage_cover_url(group: dict) -> str:
    """Cover art of a stage: the scene picture for 影拓丰碑 (a 20 MB PNG), the enemy artwork for 战争回响."""
    folder = "seasontower" if group.get("mode") == "战争回响" else "dungeon"
    return f"{IMAGE_BASE}/{folder}/{group['cover']}.png" if group.get("cover") else ""


def enemy_icon_url(enemy_id: str) -> str:
    return f"{IMAGE_BASE}/monstericonbig/{enemy_id}.png"


def build_calendar(load, text) -> dict:
    """Activities, gacha pools and battle-pass weekly tasks with their open/close times (Asia/Shanghai)."""
    ranges = load("TimeRangeTable")
    chars, items = load("CharacterTable"), load("ItemTable")  # weapon names live in ItemTable

    def window(*time_ids: str) -> tuple[str, str] | None:
        for time_id in time_ids:
            spans = (ranges.get(time_id) or {}).get("timeRangeList") or []
            if time_id and spans and spans[0].get("openTime"):
                return spans[0]["openTime"], spans[0].get("closeTime", "")
        return None

    activities = []
    for activity_id, row in load("ActivityTable").items():
        span, name = window(row.get("timeId", "")), text(row.get("name"))
        if span and name:
            # tab / colour: the banner the game shows on the activity's tab (sprites/activity/<tab>.png) and its accent colour
            activities.append({"id": activity_id, "name": name, "open": span[0], "close": span[1], "tab": row.get("tabImg") or "", "color": row.get("tabImgColor") or ""})

    pools = []
    for kind, table, up_key, names in (("char", "GachaCharPoolTable", "upCharIds", chars), ("weapon", "GachaWeaponPoolTable", "upWeaponIds", items)):
        for pool_id, row in load(table).items():
            suffix = re.search(r"(\d+_\d+_\d+)$", row.get("nameImage") or "")
            span = window(
                f"time_{pool_id.replace('weponbox', 'weaponbox')}",
                row.get("clientTopTimeId", ""),
                f"time_rerun_{suffix.group(1)}" if suffix else "",
            )
            if not span or not span[1]:  # permanent pools (standard, beginner, constant weapon boxes)
                continue
            up = [text(names.get(i, {}).get("name")) or i for i in row.get(up_key) or []]
            pools.append({"id": pool_id, "kind": kind, "name": text(row.get("name")), "up": up, "open": span[0], "close": span[1]})

    tasks, conditions = load("BattlePassTaskTable"), load("BattlePassConditionTable")
    passes = []
    for time_id in sorted(k for k in ranges if re.fullmatch(r"time_bp_\d+", k)):
        bp, span = time_id.removeprefix("time_"), window(time_id)
        if not span:
            continue
        weeks = []
        for week in range(1, 13):
            rows = sorted((t for t in tasks.values() if t.get("groupId") == f"{bp}_taskgroup_weekly_{week}"), key=lambda t: t.get("sortId", 0))
            if not rows:
                break
            opened = span[0] if week == 1 else (window(f"time_{bp}_sublabel_week_{week}") or ("", ""))[0]
            names = []
            for row in rows:
                goal = (conditions.get((row.get("conditionIds") or [""])[0]) or {}).get("progressToCompare", "")
                names.append(re.sub(r"<[^<>]*>", "", text(row.get("name")).replace("%d", str(goal))))
            weeks.append({"week": week, "open": opened, "tasks": names})
        passes.append({"id": bp, "open": span[0], "close": span[1], "weeks": weeks})
    # 每周事务 (activity_weekly_task_1): task ids are "week{N}_..."; week N starts open + (N-1) * 7 days.
    weekly_activity = load("ActivityTable").get("activity_weekly_task_1", {})
    weekly_span = window(weekly_activity.get("timeId", ""))
    weekly_weeks: dict[str, list] = {}
    for row in sorted(load("ActivityWeeklyTaskTable").values(), key=lambda r: r.get("sortId", 0)):
        week = re.match(r"week(\d+)_", str(row.get("taskId", "")))
        if row.get("activityId") != "activity_weekly_task_1" or not week:
            continue
        factor = row.get("displayFactor")
        target = (row.get("progressToCompare") or 0) * (1 if factor is None else factor)
        target_text = str(int(target)) if float(target).is_integer() else f"{target:g}"
        desc = re.sub(r"<[^<>]*>", "", re.sub(r"%[-+0-9.]*[dfs]", target_text, text(row.get("desc"))))
        weekly_weeks.setdefault(week.group(1), []).append({"desc": desc, "score": row.get("score", 0)})
    milestones = (load("ActivityWeeklyTaskMileStoneTable").get("activity_weekly_task_1") or {}).get("mileStones") or {}
    weekly = {
        "name": text(weekly_activity.get("name")) or "每周事务",
        "open": weekly_span[0] if weekly_span else "",
        "weeks": weekly_weeks,
        "milestones": sorted(m.get("score", 0) for m in milestones.values()),
    }
    versions = []
    for time_id in ranges:
        match = re.fullmatch(r"time_version_v(\d+)d(\d+)", time_id)
        span = window(time_id) if match else None
        if span:
            versions.append({"name": f"{match.group(1)}.{match.group(2)}", "open": span[0], "close": span[1]})
    return {
        "activities": activities, "pools": pools, "passes": passes, "weekly": weekly, "versions": versions,
        "gifts": build_gacha_gifts(load, text),
    }


def build_gacha_gifts(load, text) -> dict:
    """Pool id -> what the game hands out by itself at pull milestones in that pool (not pulled).

    Each entry is {"at": pull count of the first one, "every": interval or 0 for a one-off,
    "items": [{"name", "icon"}]}; a repeating gift cycles through its items (weapon pools: supply
    case, UP weapon, case, ...). Counts are paid pulls in that pool; the free ten-pull is not counted.
    `icon` is the operator / weapon id the gift stands for, when there is one.
    """
    rewards, items = load("RewardTable"), load("ItemTable")

    def gift(reward_id: str) -> dict | None:
        bundles = (rewards.get(reward_id) or {}).get("itemBundles") or []
        item_id = bundles[0].get("id", "") if bundles else ""
        name = text((items.get(item_id) or {}).get("name"))
        if not name:
            return None
        count = bundles[0].get("count", 1)
        icon = item_id if item_id.startswith("wpn_") else item_id.removeprefix("item_charpotentialup_") if item_id.startswith("item_charpotentialup_") else ""
        return {"name": f"{name}×{count}" if count > 1 else name, "icon": icon}

    def entry(at: int, every: int, reward_ids: list[str]) -> dict | None:
        found = [g for g in map(gift, reward_ids) if g]
        return {"at": at, "every": every, "items": found} if at > 0 and found and len(found) == len(reward_ids) else None

    result: dict[str, list] = {}
    for table, type_table in (("GachaCharPoolTable", "GachaCharPoolTypeTable"), ("GachaWeaponPoolTable", "GachaWeaponPoolTypeTable")):
        types = load(type_table)
        for pool_id, row in load(table).items():
            rule = types.get(str(row.get("type"))) or {}
            every = rule.get("intervalAutoRewardPerPullCount") or 0
            entries = [
                entry(rule.get("intervalAutoRewardStartPullCount", 0) + every, every, row.get("intervalAutoRewardIds") or []) if every else None,
                entry(rule.get("onceRewardIdPullCount") or 0, 0, [row.get("onceRewardId") or ""]),
                entry(rule.get("onceRewardId2PullCount") or 0, 0, [row.get("onceRewardId2") or ""]),
                *(entry(at, 0, [reward_id]) for at, reward_id in zip(rule.get("cumulativeRewardsPullCount") or [], row.get("cumulativeRewardIds") or [])),
            ]
            if any(entries):
                result[pool_id] = sorted((e for e in entries if e), key=lambda e: e["at"])
    return result


def _equip_value(value: float, modifier: int, flat: bool) -> str:
    """Modifier types (maps.MODIFIER_TYPE_MAP): 5 / 7 add, 6 multiplies the base, 8 scales damage taken (0.96 = 4% less)."""
    if modifier == 8:
        return f"{(1 - value) * 100:.1f}%"
    if modifier == 6 or not flat:
        return f"{value * 100:.1f}%"
    return f"{value:.1f}".removesuffix(".0")


def _equip_attrs(equip: dict, maps: dict) -> list[dict]:
    """The three refinable attributes of a piece, in the game's order (attrIndex 1..3)."""
    attrs = []
    for mod in equip.get("displayAttrModifiers") or []:
        composite = mod.get("compositeAttr") or ""
        flat = mod.get("attrType") in EQUIP_FLAT_ATTRS or composite in ("Main", "Sub")
        enhanced = mod.get("enhancedAttrValues") or []
        attrs.append(
            {
                "name": maps["COMPOSITE_NAME_MAP"].get(composite) or maps["ATTR_MAP"].get(str(mod.get("attrType")), ""),
                "base": _equip_value(mod.get("attrValue", 0.0), mod.get("modifierType"), flat),
                "max": _equip_value(enhanced[-1], mod.get("modifierType"), flat) if enhanced else "",
            }
        )
    return attrs


def build_equip_items(load, text, maps) -> dict:
    """Every piece of equipment by name (set pieces and loose ones): icon and attribute names, for the build statistics."""
    equips, items = load("EquipTable"), load("ItemTable")
    result: dict[str, dict] = {}
    for equip_id, equip in equips.items():
        item = items.get(equip_id, {})
        name = text(item.get("name"))
        if name:
            result.setdefault(name, {"icon": item.get("iconId") or equip_id, "attrs": [a["name"] for a in _equip_attrs(equip, maps)]})
    return result


def build_equip_sets(load, text, maps) -> dict:
    """Equipment sets: the 3-piece effect and every piece with its attributes before and after full enhancement."""
    equips, items, skills = load("EquipTable"), load("ItemTable"), load("SkillPatchTable")
    sets: dict[str, dict] = {}
    for set_id, row in load("EquipSuitTable").items():
        rule = (row.get("list") or [{}])[0]
        name = text(rule.get("suitName"))
        bundle = skills.get(rule.get("skillID", ""), {}).get("SkillPatchDataBundle", [])
        if not name or not bundle:
            continue
        rank = bundle[min(max(rule.get("skillLv", 1), 1), len(bundle)) - 1]
        values = {e["key"].lower(): e["value"] for e in rank.get("blackboard", [])}
        pieces = []
        for equip_id in row.get("equipList") or []:
            equip, item = equips.get(equip_id), items.get(equip_id, {})
            piece_name = text(item.get("name"))
            if not equip or not piece_name:
                continue
            pieces.append(
                {
                    "id": equip_id,
                    "name": piece_name,
                    "part": EQUIP_PARTS.get(equip.get("partType"), ""),
                    "rarity": item.get("rarity", 0),
                    "level": equip.get("minWearLv", 0),
                    "icon": item.get("iconId") or equip_id,
                    "defense": round((equip.get("displayBaseAttrModifier") or {}).get("attrValue", 0)),
                    "domain": maps["DOMAIN_MAP"].get(equip.get("domainId", ""), ""),
                    "attrs": _equip_attrs(equip, maps),
                }
            )
        if not pieces:
            continue
        order = list(EQUIP_PARTS.values())
        pieces.sort(key=lambda p: (-p["rarity"], order.index(p["part"]) if p["part"] in order else 9, p["id"]))
        sets[set_id] = {
            "id": set_id,
            "name": name,
            "count": rule.get("equipCnt", 3),
            "effect": rich_to_html(fill_placeholders(text(rank.get("description")), values)),
            "rarity": pieces[0]["rarity"],
            "level": max(p["level"] for p in pieces),
            "domain": pieces[0]["domain"],
            "pieces": pieces,
        }
    return sets


def activity_banner_url(tab: str) -> str:
    return f"{IMAGE_BASE}/activity/{tab}.png"


def equip_icon_url(icon_id: str) -> str:
    return f"{IMAGE_BASE}/itemicon/{icon_id}.png"


def char_icon_url(char_id: str) -> str:
    return f"{IMAGE_BASE}/charremoteicon/icon_{char_id}.png"


def weapon_icon_url(icon_id: str) -> str:
    return f"{IMAGE_BASE}/itemiconbig/{icon_id}.png"


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
