"""AKEData (https://www.akedata.wiki/) sync and compact index builder for Endfield wiki lookups.

The site publishes unpacked game tables on data.akedata.wiki. We poll its small
manifest with conditional requests, download only the tables we need when the
version changes, and reduce them to one compact index.json used for lookups.
"""

from __future__ import annotations

import ast
import json
import operator
import re
import shutil
from html import escape
from pathlib import Path
from typing import Any

import httpx

DATA_BASE = "https://data.akedata.wiki"
MANIFEST_URL = f"{DATA_BASE}/manifest.json"
MAPS_URL = "https://www.akedata.wiki/public/CH/maps.json"
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
    "BattlePassTaskTable",
    "BattlePassConditionTable",
    "ActivityWeeklyTaskTable",
    "ActivityWeeklyTaskMileStoneTable",
)
INDEX_SCHEMA = 8  # bump to force a rebuild when the index layout changes
# Enemy resistance attribute ids (AttributeShowConfigTable), in the in-game display order.
ENEMY_RESISTANCES = ((94, "物理"), (98, "灼热"), (97, "电磁"), (96, "寒冷"), (95, "自然"), (99, "超域"))
TOWER_DIFFICULTIES = {"1": "普通", "2": "困难", "3": "残酷"}
SKILL_TYPES = {0: "普通攻击", 1: "战技", 3: "连携技", 2: "终结技"}
SKILL_ORDER = (0, 1, 3, 2)
BASE_ATTRS = (("1", "生命值"), ("2", "攻击力"))  # base DEF is always 0 in Endfield; defense comes from gear
STAT_LEVEL = 90  # current player level cap; tables extend further


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
                    values.setdefault(entry["key"].lower(), entry["value"])
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
            skill_rows.append(
                {
                    "type": SKILL_TYPES.get(group.get("skillGroupType"), "技能"),
                    "name": text(group.get("name")),
                    "desc": rich_to_html(fill_placeholders(text(group.get("desc")), values)),
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
        "endgame": build_endgame(load, text),
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
    return {"operators": len(operators), "weapons": len(weapons)}


def build_endgame(load, text) -> dict:
    """Stage index for 战争回响 (season tower) and 影拓丰碑 (high-difficulty series)."""
    dungeons, series = load("DungeonTable"), load("DungeonSeriesTable")
    enemies = load("EnemyTemplateDisplayInfoTable")
    enemy_rows, enemy_attrs = load("EnemyTable"), load("EnemyAttributeTemplateTable")
    tower_groups, tower_seasons, tower_buffs = load("SeasonTowerGameGroupTable"), load("SeasonTowerTable"), load("SeasonTowerDungeonTable")
    time_ranges = load("TimeRangeTable")

    def base_name(stage_id: str) -> str:
        return text(dungeons.get(stage_id, {}).get("dungeonName")).split("·")[0].strip()

    def stage(stage_id: str, difficulty: str) -> dict:
        row = dungeons.get(stage_id, {})
        foes = []
        for enemy_id, level in zip(row.get("enemyIds", []), row.get("enemyLevels", [])):
            name, icon, probe = "", "", enemy_id
            while not name and probe:  # stage-specific variants append suffixes, e.g. eny_0118_klhog_hdg026
                name, icon = text(enemies.get(probe, {}).get("name")), probe
                probe = probe.rpartition("_")[0] if probe.count("_") > 2 else ""
            foe = {
                "name": name or enemy_id,
                "icon": icon if name else "",
                "level": level,
                **enemy_stats(enemy_rows.get(enemy_id, {}), enemy_attrs, level),
            }
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
            groups.append({"id": base, "name": base_name(base), "mode": "影拓丰碑", "series": text(row.get("name")), "stages": variants})
        monument.append({"id": series_id, "name": text(row.get("name")), "groups": groups})

    return {"tower": tower, "tower_seasons": seasons, "monument": monument}


def enemy_stats(row: dict, templates: dict, level: int) -> dict:
    """HP / ATK / DEF at the stage level with the enemy's own modifiers, plus non-zero resistances.

    attrModifiers: modifierType 1 adds a percentage (-0.5 = -50%), modifierType 4 multiplies.
    Stage-wide buffs (special_buff text) are not applied.
    """
    template = templates.get(row.get("attrTemplateId", ""), {})
    per_level = template.get("levelDependentAttributes") or []
    if not 0 < level <= len(per_level):
        return {}
    base = {a["attrType"]: a["attrValue"] for a in per_level[level - 1]["attrs"]}
    fixed = {a["attrType"]: a["attrValue"] for a in (template.get("levelIndependentAttributes") or {}).get("attrs", [])}
    stats = {}
    for attr, key in ((1, "hp"), (2, "atk"), (3, "def")):
        value, percent, factor = base.get(attr, 0.0), 0.0, 1.0
        for mod in row.get("attrModifiers") or []:
            if mod.get("attrType") == attr and mod.get("modifierType") == 1:
                percent += mod.get("attrValue", 0.0)
            elif mod.get("attrType") == attr and mod.get("modifierType") == 4:
                factor *= mod.get("attrValue", 1.0)
        stats[key] = round(value * (1 + percent) * factor)
    stats["res"] = [[label, round(fixed[attr])] for attr, label in ENEMY_RESISTANCES if fixed.get(attr)]
    return stats


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
            activities.append({"id": activity_id, "name": name, "open": span[0], "close": span[1]})

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
    return {"activities": activities, "pools": pools, "passes": passes, "weekly": weekly, "versions": versions}


def char_icon_url(char_id: str) -> str:
    return f"{IMAGE_BASE}/charremoteicon/icon_{char_id}.png"


def weapon_icon_url(icon_id: str) -> str:
    return f"{IMAGE_BASE}/itemiconbig/{icon_id}.png"


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
