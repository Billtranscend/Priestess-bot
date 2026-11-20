"""同步 AKEData（https://www.akedata.wiki/），并为终末地图鉴查询生成精简索引。

该站在 data.akedata.wiki 上发布解包的游戏数据表。我们用条件请求轮询它的小清单文件，
版本变化时只下载需要的表，再精简成一份查询用的 index.json。

关卡敌人还需要数据表旁边的场景文件：刷怪配置（spawner）说明一关会刷出哪些敌人变体、
它们出生时带哪些 buff；buff 文件则说明这些 buff 对敌人属性的影响。
"""

from __future__ import annotations

import ast
import asyncio
import json
import operator
import re
from datetime import datetime
import shutil
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from plugins.shared import write_json

DATA_BASE = "https://data.akedata.wiki"
MANIFEST_URL = f"{DATA_BASE}/manifest.json"
MAPS_URL = "https://www.akedata.wiki/public/CH/maps.json"
ASSET_INDEX_URL = f"{DATA_BASE}/asset-sync-index.json"  # 列出 public/Json 下的所有文件
JSON_BASE = f"{DATA_BASE}/public/Json"
STAGE_FETCHES = 4  # 小关卡文件的并发下载数
SAFE_PATH = re.compile(r"^(?:SpawnerConfig/\w[\w.-]*|BuffData)/\w[\w.-]*\.json$")
IMAGE_BASE = f"{DATA_BASE}/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
USER_AGENT = "QQBot-EndfieldWiki/1.0 (non-commercial group bot; data from AKEData)"
TABLES = (
    "I18nTextTable_CN",
    "RichTextStyleTable",
    "HyperlinkTextTable",
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
    "EnemyAbilityDescTable",
    "SpaceshipCharSkillTable",
    "SpaceshipSkillTable",
    "ActivityTable",
    "GachaCharPoolTable",
    "GachaWeaponPoolTable",
    "GachaCharPoolTypeTable",
    "GachaWeaponPoolTypeTable",
    "TextTable",
    "GachaCharPoolContentTable",
    "GachaWeaponPoolContentTable",
    "RewardTable",
    "BattlePassTaskTable",
    "BattlePassConditionTable",
    "ActivityWeeklyTaskTable",
    "ActivityWeeklyTaskMileStoneTable",
)
INDEX_SCHEMA = 27  # 索引结构变化时加一以强制重建（27：复刻武器池沿用其角色池的时间窗口，link；26：常驻活动的新关卡；25：活动结束时间取游戏内日历的关卡，shop_close；18：领袖敌人，19：后勤技能，20：散件装备，21：卡池，22：增辉条件，23：游戏富文本颜色和术语图标，24：来自 HyperlinkTextTable 的 <#term> 样式）
EXTRA_GOAL_FILES = ("GameplayConfig/SubGameInstanceDataTable.json", "GameplayConfig/ScriptTaskExtraInfoTable.json")
# 卡池表的 `type` -> nonebot-plugin-skland 的 pool_type。新手 (1) 和 常驻 (2) 不收录，与它自己的表一致。
CHAR_POOL_TYPES = {0: "special", 3: "extra", 4: "rerun"}
WEAPON_POOL_TYPES = {0: "normal", 1: "rerun"}
# 敌人抗性的属性 id（AttributeShowConfigTable），按游戏内显示顺序。
ENEMY_RESISTANCES = ((94, "物理"), (98, "灼热"), (97, "电磁"), (96, "寒冷"), (95, "自然"), (99, "超域"))
TOWER_DIFFICULTIES = {"1": "普通", "2": "困难", "3": "残酷"}
SKILL_TYPES = {0: "普通攻击", 1: "战技", 3: "连携技", 2: "终结技"}
SKILL_ORDER = (0, 1, 3, 2)
SKILL_LEVELS = ("Lv9", "M1", "M2", "M3")  # 技能十二级中的最后四级，显示在数值表里
COST_LABELS = {1: "技力消耗", 2: "所需终结技能量"}  # costValue 的含义，按技能组类型区分
BASE_ATTRS = (("1", "生命值"), ("2", "攻击力"))  # 终末地里基础防御力总是 0；防御来自装备
STAT_LEVEL = 90  # 当前玩家等级上限；数据表里的等级更高
EQUIP_PARTS = {0: "护甲", 1: "护手", 2: "配件"}
EQUIP_FLAT_ATTRS = {1, 2, 3, 39, 40, 41, 42, 87}  # 生命、攻击、防御、四项能力值、源石技艺强度：是直接数值，其余都是比例


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(120.0, connect=20.0),
        follow_redirects=True,
    )


async def fetch_manifest(client: httpx.AsyncClient) -> dict:
    """该站当前的版本列表。

    总是完整读取并绕过所有缓存：它只有几 KB，而拿它的 `latest` 和索引里的版本比较，
    是唯一不会被过时的 ETag 或 CDN 副本骗过的检查。
    """
    response = await client.get(MANIFEST_URL, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    response.raise_for_status()
    return response.json()


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
    """高难场景的刷怪配置，以及其中敌人出生时带的所有 buff。"""

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
    await asyncio.gather(*(fetch(p) for p in EXTRA_GOAL_FILES if p in files))


# ── 描述文字格式化 ─────────────────────────────────────────────────────

_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _safe_eval(expr: str, values: dict[str, float]) -> float:
    """只对数字和已知变量名做 +-*/ 四则运算求值。"""

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
    """去掉显示玩家自己小队数值的括号部分，例如 智识值({floor:deck_wisd:0})。"""
    return re.sub(r"[（(][^（）()]*\{floor:[^{}]+\}[^（）()]*[）)]", "", text)


# 游戏的战斗文本样式（RichTextStyleTable，ba.*）：每个标签一种颜色、分三个变体，状态类术语
# 还带图标（TermIcon/…）。由 build_index 填充；卡片按游戏里的样子绘制。
RICH_STYLES: dict[str, dict[str, str]] = {}
STYLE_VARIANT = 2  # 第三种颜色在浅色页面上看得清（电磁用 #cc9900 而不是 #ffcc00）


def parse_rich_styles(table: dict) -> dict[str, dict[str, str]]:
    out = {}
    for tag, row in table.items():
        if not tag.startswith("ba."):
            continue
        pre = row.get("preDef") or [""]
        definition = pre[STYLE_VARIANT] if len(pre) > STYLE_VARIANT else pre[0]
        color = re.search(r"<color=(#[0-9a-fA-F]{6})>", definition)
        icon = re.search(r'<image="TermIcon/([^"]+)"', definition)
        out[tag] = {"color": color.group(1).lower() if color else "", "icon": icon.group(1) if icon else ""}
    return out


def rich_class(name: str) -> str:
    return "s-" + name.replace(".", "-")


# <#term> 链接（法术附着、自然脆弱…）的样式来自 HyperlinkTextTable：用它自己的图标（iconPath）和
# 一个 RichTextStyleTable 条目的颜色（richTextId）；没有 richTextId 的术语在游戏里就是普通文字（消耗）。
TERM_STYLES: dict[str, dict[str, str]] = {}


def parse_term_styles(table: dict, styles: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    out = {}
    for term, row in table.items():
        icon = row.get("iconPath") or ""
        out[term] = {"color": styles.get(row.get("richTextId") or "", {}).get("color", ""),
                     "icon": icon.split("/", 1)[1] if icon.startswith("TermIcon/") else ""}
    return out


def term_class(name: str) -> str:
    return "h-" + name.replace(".", "-")


def rich_to_html(text: str) -> str:
    """把游戏富文本（<@style>..</>、<#term>..</>、\\n）转成安全的 HTML。游戏表里的战斗样式
    用它的颜色绘制，前面加上术语图标；没有样式的术语用技能名的蓝色。"""
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
            style = RICH_STYLES.get(name)
            term = TERM_STYLES.get(name) if kind == "#" else None
            icon = ""
            if term is not None:
                cls = f"rt {term_class(name)}" if term["color"] or term["icon"] else "plain"
                icon = f'<i class="ti ti-{term["icon"]}"></i>' if term["icon"] else ""
            elif "info" in name:
                cls = "info"
            elif "vup" in name or "num" in name:
                cls = "up"
            elif "vdown" in name:
                cls = "down"
            elif style and (style["color"] or style["icon"]):
                cls = f"rt {rich_class(name)}"
                icon = f'<i class="ti ti-{style["icon"]}"></i>' if style["icon"] else ""
            elif kind == "#":
                cls = "rt s-term"
            else:
                cls = "key"
            out.append(f'<span class="{cls}">{icon}')
            stack.append(cls)
    out.append(escape(text[pos:]))
    out.extend("</span>" for _ in stack)
    return "".join(out).replace("\\n", "<br>").replace("\n", "<br>")


# ── 生成索引 ───────────────────────────────────────────────────────────


def build_index(table_dir: Path, version: dict, out_file: Path) -> dict[str, int]:
    def load(name: str) -> Any:
        return json.loads((table_dir / f"{name}.json").read_text("utf-8"))

    i18n = load("I18nTextTable_CN")
    maps = load("maps")
    RICH_STYLES.clear()
    RICH_STYLES.update(parse_rich_styles(load("RichTextStyleTable")))
    TERM_STYLES.clear()
    TERM_STYLES.update(parse_term_styles(load("HyperlinkTextTable"), RICH_STYLES))

    def text(obj: Any) -> str:
        if isinstance(obj, dict):
            return i18n.get(str(obj.get("id")), "") or obj.get("text", "") or ""
        return obj if isinstance(obj, str) else ""

    chars, growth = load("CharacterTable"), load("CharGrowthTable")
    ship_chars, ship_skills = load("SpaceshipCharSkillTable"), load("SpaceshipSkillTable")

    def logistics(cid: str) -> list[dict]:
        """后勤技能，与 AKEData 角色页一致：每个槽位一项，含各等级（α/β/γ）及解锁提示。"""
        slots: dict[str, dict] = {}
        for entry in (ship_chars.get(cid) or {}).get("skillList") or []:
            skill = ship_skills.get(entry.get("skillId"))
            if not skill:
                continue
            name = text(skill.get("talentName"))
            slot = slots.setdefault(name, {"name": name, "room": maps["room_type_map"].get(str(skill.get("roomType")), ""), "icon": skill.get("icon", ""), "levels": []})
            slot["levels"].append({"postfix": skill.get("skillNamePostfix", ""), "desc": rich_to_html(text(skill.get("desc"))), "unlock": text(entry.get("unlockHint"))})
        return list(slots.values())

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
                    values[entry["key"].lower()] = entry["value"]  # 同组中靠后的技能优先，与 AKEData 网站一致
                values.setdefault("cooldown", top.get("coolDown", 0.0))
                values.setdefault("costvalue", top.get("costValue", 0.0))
        return values

    def level_number(value: float) -> str:
        return f"{value:g}"

    def level_table(group: dict, form_names: dict[str, str], talent_effects: list[dict]) -> list[dict]:
        """技能组各等级数值表的行，生成方式与 AKEData 角色页相同。

        消耗和冷却取自组内第一个技能；其余行是组内每个技能在游戏里显示的命名数值
        （subDescDataList），按原顺序排列。只在干员某一种形态下生效的数值
        会带上该形态的名字。
        """
        skill_ids = group.get("skillIdList", [])
        bundles = [bundle for skill_id in skill_ids if (bundle := skills.get(skill_id, {}).get("SkillPatchDataBundle", []))]
        if not bundles:
            return []
        shown = slice(-len(SKILL_LEVELS), None)
        rows: list[dict] = []
        cost = [patch.get("costValue") or 0 for patch in bundles[0]]
        cooldown = [patch.get("coolDown") or 0 for patch in bundles[0]]
        kind = group.get("skillGroupType")
        if any(cost):
            rows.append({"label": COST_LABELS.get(kind, "消耗"), "values": [level_number(v) for v in cost[shown]]})
        if any(cooldown):
            if kind == 3 and len(form_names) > 1:
                # 天赋可能只在一种形态下缩短连携技的冷却。
                for form_id, form_name in form_names.items():
                    sums, seen = {2: 0.0, 4: 0.0}, set()
                    for effect in talent_effects:
                        for item in effect.get("dataList", []):
                            param = item.get("skillParamModifier") or {}
                            if param.get("paramType") not in (2, 4) or param.get("modifyType") != 1:
                                continue
                            if param.get("skillId") not in skill_ids or form_id not in (item.get("activeCondition") or []):
                                continue
                            sums[param["paramType"]] += param.get("paramValue") or 0.0
                            seen.add(param["paramType"])
                    shift = sums[4] if 4 in seen else sums[2] if 2 in seen else 0.0
                    rows.append({"label": f"{form_name} · 冷却时间（秒）", "values": [level_number(v + shift) for v in cooldown[shown]]})
            else:
                rows.append({"label": "冷却时间（秒）", "values": [level_number(v) for v in cooldown[shown]]})
        stagger = re.search(r"\{\s*(\w+)[^{}]*\}(?:</>)?点失衡", text(group.get("desc"))) if kind == 0 else None
        if stagger:
            # 重击的失衡值只写在描述里（"重击会造成{poise:0}点失衡"）；它每级的数值
            # 就是填进描述的那个值（组内最后一个设置它的技能）。
            key = stagger.group(1).lower()
            poise = [next((e["value"] for b in reversed(bundles) for e in b[level].get("blackboard", []) if e["key"].lower() == key), None)
                     for level in range(len(bundles[0]))]
            if all(v is not None for v in poise):
                rows.append({"label": "重击失衡值", "values": [level_number(v) for v in poise[shown]]})
        for bundle in bundles:
            named: dict[tuple, dict] = {}
            for level, patch in enumerate(bundle):
                occurrence: dict[tuple, int] = {}
                for sub in patch.get("subDescDataList", []):
                    name = text(sub.get("name"))
                    if not name:
                        continue
                    form_id = sub.get("conditionId") or ""
                    nth = occurrence.get((form_id, name), 0)
                    occurrence[(form_id, name)] = nth + 1
                    row = named.setdefault(
                        (form_id, name, nth),
                        {"label": f"{form_names[form_id]} · {name}" if form_id in form_names else name, "values": [""] * len(bundle)},
                    )
                    row["values"][level] = str(sub.get("desc") or "")
            rows += [{"label": row["label"], "values": row["values"][shown]} for row in named.values()]
        return rows

    # 专武：每个 6★ 干员第一档推荐里的第一把（已对照社区攻略核实）。
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

        # 每个天赋的最高节点，用于取决于干员形态的效果
        top_talents: dict = {}
        for node in grow.get("talentNodeMap", {}).values():
            info = node.get("passiveSkillNodeInfo") or {}
            if node.get("nodeType") != 4 or not info.get("talentEffectId"):
                continue
            best = top_talents.get(info.get("index"))
            if best is None or (info.get("level", 0), info.get("breakStage", 0)) > (best.get("level", 0), best.get("breakStage", 0)):
                top_talents[info.get("index")] = info
        talent_effects = [effects[info["talentEffectId"]] for info in top_talents.values() if info["talentEffectId"] in effects]

        skill_rows = []
        groups = sorted(grow.get("skillGroupMap", {}).values(), key=lambda g: SKILL_ORDER.index(g.get("skillGroupType", 0)) if g.get("skillGroupType") in SKILL_ORDER else 9)
        for group in groups:
            values = max_level_values(group.get("skillIdList", []))
            # 技能在干员的两种形态下可能写法不同（诀：阵诀·智 / 阵诀·意）；
            # 这时通用描述可能为空，内容全写在各形态里。
            forms = [
                {
                    "name": text(group.get(f"conditionName{n}")),
                    "when": rich_to_html(strip_live_values(text(group.get(f"conditionDesc{n}"))).removeprefix("/*").removesuffix("*/").strip()),
                    # 简短的形态文字开头会再写一遍形态名（"阵诀·智：\n- ..."），这里已经作为标题了
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
                    "table": level_table(
                        group, {group[f"conditionId{n}"]: text(group.get(f"conditionName{n}")) for n in (1, 2) if group.get(f"conditionId{n}")}, talent_effects
                    ),
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
            "logistics": logistics(cid),
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
        "rich_styles": dict(RICH_STYLES),
        "rich_terms": dict(TERM_STYLES),
        "equip_sets": build_equip_sets(load, text, maps),
        "equip_items": build_equip_items(load, text, maps),
        "endgame": build_endgame(load, text, maps, table_dir),
        "leaders": build_leaders(load, text),
        "calendar": build_calendar(load, text),
        "gacha_pools": build_gacha_pools(load, text),
        "version": version.get("id", ""),
        "published_at": version.get("publishedAt", ""),
        "operators": operators,
        "weapons": weapons,
    }
    write_json(out_file, index)
    stages = [s for g in index["endgame"]["tower"].values() for s in g["stages"]]
    stages += [s for m in index["endgame"]["monument"] for g in m["groups"] for s in g["stages"]]
    foes = [foe for s in stages for foe in s["enemies"]]
    # "foes_plain" 应该接近零：突然变大说明刷怪文件和关卡对不上了。
    return {"operators": len(operators), "weapons": len(weapons), "foes": len(foes), "foes_plain": sum(1 for f in foes if f.get("plain"))}


def extra_goals(load, text, table_dir: Path) -> dict[str, str]:
    """战争回响关卡 id -> 它的附加挑战目标（增辉条件），查找方式与游戏自己的关卡页相同。

    SubGameInstanceDataTable 给出关卡的脚本和额外任务，ScriptTaskExtraInfoTable 给出该任务的文本键，
    TextTable 给出文字。森空岛上同样含义的 `additionalChallengeTarget` 总是空的。
    """
    if not all((table_dir / path).is_file() for path in EXTRA_GOAL_FILES):
        return {}
    games = load("GameplayConfig/SubGameInstanceDataTable").get("dataTable") or {}
    tasks = load("GameplayConfig/ScriptTaskExtraInfoTable").get("dataTable") or {}
    labels = load("TextTable")
    goals = {}
    for stage_id, game in games.items():
        extra = game.get("extraTasks") or []
        if game.get("gameMechanicsType") != "DungeonSeasonTower" or not extra:
            continue
        script = str(game.get("bindScriptId"))
        info = next((scripts[script][extra[0].get("taskId")] for scripts in tasks.values() if extra[0].get("taskId") in scripts.get(script, {})), None)
        if info is None:
            continue
        aims = [aim.get("description") or {} for aim in (info.get("trackingInfoDict") or {}).values()]
        key = (info.get("singleDescription") or {}).get("key") or next((aim.get("key") for aim in aims if aim.get("key")), "")
        goal = re.sub(r"<[^<>]*>", "", text(labels.get(key))).strip()
        if goal:
            goals[stage_id] = goal
    return goals


def build_endgame(load, text, maps: dict, table_dir: Path) -> dict:
    """战争回响（赛季塔）和影拓丰碑（高难系列）的关卡索引。"""
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
    goals = extra_goals(load, text, table_dir)

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
        """刷出来时没有血条：属于关卡机制，不是要击败的敌人。"""
        born = list(enemy_rows.get(entry["enemyId"], {}).get("bornBuffs") or []) + [b.get("buffId", "") for b in entry.get("bornBuffList") or []]
        return any(hides_health_bar(buff(buff_id)) for buff_id in born)

    def display(enemy_id: str) -> tuple[str, str]:
        probe = template_of(enemy_id)
        name = text(enemies.get(probe, {}).get("name"))
        if not name:  # 表里没有这一行：关卡专用变体会加后缀，例如 eny_0118_klhog_hdg026
            probe = enemy_id
            while probe and not (name := text(enemies.get(probe, {}).get("name"))):
                probe = probe.rpartition("_")[0] if probe.count("_") > 2 else ""
        return (name, probe) if name else (enemy_id, "")

    def spawned(row: dict, pairs: list[tuple[str, int]]) -> list[dict]:
        """本关的刷怪库条目：敌人变体、等级，以及出生时带的 buff。"""
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
            # 所有战争回响关卡共用一个场景；在那里只有当本关列出了它会刷的全部敌人时，
            # 才认为这个刷怪点属于本关。
            if shared and any(template_of(e["enemyId"]) not in templates for e in entries):
                continue
            library += entries
        return library

    def stage(stage_id: str, difficulty: str) -> dict:
        row = dungeons.get(stage_id, {})
        pairs = list(dict.fromkeys(zip(row.get("enemyIds", []), row.get("enemyLevels", []))))
        listed = {enemy_id for enemy_id, _ in pairs}
        # 场景物件在各难度下等级不变，所以在整个场景里查找。
        props = [e for config in spawners(row.get("sceneId", "")) for e in config.get("enemyLibrary") or [] if scenery(e)]
        library = [e for e in spawned(row, pairs) if not scenery(e)]
        rows: dict[tuple[str, int], tuple[str, list[dict]]] = {}  # (变体, 等级) -> (显示为哪个敌人, 它的刷怪条目)
        taken = []
        for enemy_id, level in pairs:
            same_level = [e for e in library if e["enemyLevel"] == level]
            # 关卡列出的要么是具体变体，要么只是这些变体所源自的普通敌人。
            found = [e for e in same_level if e["enemyId"] == enemy_id] or [
                e for e in same_level if e["enemyId"] not in listed and template_of(e["enemyId"]) == template_of(enemy_id)
            ]
            taken += found
            if not found and any(template_of(e["enemyId"]) == template_of(enemy_id) for e in props):
                continue  # 游戏里列出了，但它在关卡里只是场景物件
            for variant in list(dict.fromkeys(e["enemyId"] for e in found)) or [enemy_id]:
                rows[(variant, level)] = (enemy_id, [e for e in found if e["enemyId"] == variant])
        for entry in library:  # 会被刷出，但不在关卡的敌人列表里
            if not any(entry is e for e in taken):
                rows.setdefault((entry["enemyId"], entry["enemyLevel"]), (entry["enemyId"], []))[1].append(entry)
        foes = []
        for (variant, level), (shown, entries) in rows.items():
            born: list[dict] = []
            for entry in entries:  # 与 AKEData 网站一样每个变体一行：汇总它所有刷出点的 buff，每个 buff 只列一次
                born += [b for b in entry.get("bornBuffList") or [] if b.get("buffId") not in {x.get("buffId") for x in born}]
            name, icon = display(shown)
            enemy = enemy_rows.get(variant, {})
            own = [effects(b) for b in enemy.get("bornBuffs") or []]
            placed = [effects(b.get("buffId", ""), b.get("blackboard")) for b in born]
            foe = {"name": name, "icon": icon, "level": level, **enemy_stats(enemy, enemy_attrs, level, own + placed)}
            # 上面的数值里已经包含的加成，措辞与 AKEData 网站一致：敌人变体自带的加成（"born"）
            # 和关卡刷怪点额外加的（"buff"）。
            born_notes = modifier_notes(list(enemy.get("attrModifiers") or []) + [m for mods, _ in own for m in mods], attr_names)
            buff_notes = modifier_notes([m for mods, _ in placed for m in mods], attr_names) + override_notes(placed, attr_names)
            if born_notes:
                foe["born"] = born_notes
            if buff_notes:
                foe["buff"] = buff_notes
            if not entries:  # 不是刷怪点放的（战斗中召唤的）：只算敌人自身数值
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
            "extra_goal": goals.get(stage_id, ""),
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
    """Blackboard 行把数值存在几个带类型的键之一下。"""
    values = {}
    for row in rows or []:
        values[row.get("key")] = next((row[k] for k in ("valueFloat", "valueDouble", "valueInt", "valueLong", "value") if row.get(k) is not None), 0.0)
    return values


def buff_effects(buff: dict, blackboard: list | None, attr_ids: dict[str, int], formulas: dict[str, int]) -> tuple[list[dict], dict[int, float]]:
    """buff 持续期间对其持有者属性的作用：(修正项, 原始值覆盖)。

    修正项的格式同 EnemyTable.attrModifiers。覆盖项直接替换原始属性值，
    关卡就是这样把所有元素抗性拉平到同一个值的。`blackboard`
    是刷怪点传入、用来替代 buff 默认值的数值。
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


# modifierType -> 运算，按 AKEData 网站应用的顺序：先算 Base* 类型，
# 百分比是 x(1 + 数值)，多个百分比相乘。这些数值以该网站为准核对，
# 所以叠加方式完全照它来。
MODIFIER_STEPS = ((5, "add"), (6, "percent"), (7, "add"), (8, "factor"), (3, "add"), (4, "factor"), (0, "add"), (1, "percent"))
MODIFIER_KINDS = dict(MODIFIER_STEPS)
LEGACY_RESISTANCES = {80, 81, 82, 83, 84, 85}  # *DmgResistScalar：该网站的加成说明里不列这些
BALANCED_RESISTANCES = (94, 95, 96, 97, 98)  # 关卡会拉平到同一值的五种属性（超域保持原样）


def hides_health_bar(buff: dict) -> bool:
    """强制隐藏持有者血条的永久 buff。"""
    return buff.get("lifeType") == "Infinity" and any(
        "ForceHideHeadBarAction" in action.get("$type", "")
        for event in buff.get("buffEventAction") or []
        if event.get("buffEvent") == "DuringBuffEnable"
        for group in event.get("actions") or []
        for action in group.get("actionData") or []
    )


def modified(value: float, modifiers: list[dict], attr: int) -> float:
    """应用某一项属性的修正项（见 MODIFIER_STEPS）。"""
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
    """把属性修正写成 "最大生命值 +60%" / "失衡值上限 +40"，每种属性每种类型一条。

    分组和措辞与 AKEData 网站相同：同一属性的百分比连乘，
    倍率相乘后也显示为百分比，其余的直接相加。
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
    """关卡直接设定的原始属性；被拉平的元素抗性合成一行。"""
    overrides = {attr: value for _, sets in effects for attr, value in sets.items()}
    levelled = {overrides.get(attr) for attr in BALANCED_RESISTANCES}
    notes = []
    if len(levelled) == 1 and None not in levelled:
        notes.append(f"属性抗性统一为 {_number(levelled.pop())}")
        overrides = {attr: value for attr, value in overrides.items() if attr not in BALANCED_RESISTANCES}
    return notes + [f"{attr_names[attr]} 固定为 {_number(value)}" for attr, value in overrides.items() if attr in attr_names]


def enemy_stats(row: dict, templates: dict, level: int, effects: list[tuple[list[dict], dict[int, float]]] | tuple = ()) -> dict:
    """敌人在关卡等级下的生命 / 攻击 / 防御 / 失衡值上限和非零抗性。

    计入敌人自身的 attrModifiers，以及它在关卡中出生时所带 buff 的 `effects`（见 buff_effects）。
    全关卡范围的 buff（special_buff 文字）不计入。
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


LEADER_TYPE = 2  # 领袖敌人的 EnemyTemplateDisplayInfoTable.displayType（见 DisplayEnemyTypeTable）
BOSS_CHALLENGE = "dungeon_bossrush"  # 危境遗影 / 延影 / 祸影 / 狂影：每档一个关卡，enemyLevels 给出该档的等级


def build_leaders(load, text) -> list[dict]:
    """所有领袖敌人，含特性，以及它在首领挑战各档等级下的默认属性。

    默认指敌人自身属性模板在该等级下的数值，与 AKEData 敌人页显示的一致；
    关卡额外加的（刷怪点 buff）归 /敌人 关卡名 管。这些敌人出生时带的 buff
    没有属性修正（1.6.3 核对过）。没有自己挑战的敌人
    （是别的战斗的一个阶段或一部分）按 STAT_LEVEL 显示。
    """
    displays, rows, templates = load("EnemyTemplateDisplayInfoTable"), load("EnemyTable"), load("EnemyAttributeTemplateTable")
    abilities, dungeons = load("EnemyAbilityDescTable"), load("DungeonTable")
    template_of = {enemy_id: row.get("templateId") or enemy_id for enemy_id, row in rows.items()}
    tiers: dict[str, list[tuple[str, dict, int]]] = {}
    for dungeon_id, dungeon in sorted(dungeons.items()):
        if dungeon.get("dungeonCategory") != BOSS_CHALLENGE:
            continue
        for enemy_id, level in zip(dungeon.get("enemyIds") or [], dungeon.get("enemyLevels") or []):
            tiers.setdefault(template_of.get(enemy_id, enemy_id), []).append((dungeon_id, dungeon, int(level)))
    leaders = []
    for template, display in displays.items():
        if display.get("displayType") != LEADER_TYPE:
            continue
        row = rows.get(template) or {"attrTemplateId": template}
        levels = [
            {"tier": text(d.get("dungeonLevelDesc")), "stage": text(d.get("dungeonName")), "level": level, **enemy_stats(row, templates, level)}
            for _, d, level in tiers.get(template, [])
        ] or [{"tier": "", "stage": "", "level": STAT_LEVEL, **enemy_stats(row, templates, STAT_LEVEL)}]
        leaders.append({
            "id": template,
            "name": text(display.get("name")),
            "desc": text(display.get("description")),
            "traits": [t for a in display.get("abilityDescIds") or [] if (t := text((abilities.get(a) or {}).get("description")))],
            "challenge": bool(tiers.get(template)),
            "levels": levels,
        })
    return leaders


def stage_cover_url(group: dict) -> str:
    """关卡封面图：影拓丰碑用场景图（20 MB 的 PNG），战争回响用敌人立绘。"""
    folder = "seasontower" if group.get("mode") == "战争回响" else "dungeon"
    return f"{IMAGE_BASE}/{folder}/{group['cover']}.png" if group.get("cover") else ""


def enemy_icon_url(enemy_id: str) -> str:
    return f"{IMAGE_BASE}/monstericonbig/{enemy_id}.png"


def build_calendar(load, text) -> dict:
    """活动、卡池和通行证每周任务，以及它们的开始/结束时间（Asia/Shanghai）。"""
    ranges = load("TimeRangeTable")
    chars, items = load("CharacterTable"), load("ItemTable")  # 武器名在 ItemTable 里

    def window(*time_ids: str) -> tuple[str, str] | None:
        for time_id in time_ids:
            spans = (ranges.get(time_id) or {}).get("timeRangeList") or []
            if time_id and spans and spans[0].get("openTime"):
                return spans[0]["openTime"], spans[0].get("closeTime", "")
        return None

    def stamp(value: str) -> datetime | None:
        try:
            return datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    def calendar_close(activity_id: str) -> str:
        """游戏内活动日历显示的结束时间：它的最后一个阶段（time_<id>_stage_<n>calendar）。危机合约的作战
        随版本结束，之后只有它的商店（机密圣所）开到活动关闭。"""
        pattern = re.compile(rf"time_{re.escape(activity_id)}_stage_\d+calendar")
        closes = [span[1] for time_id in ranges if pattern.fullmatch(time_id) and (span := window(time_id)) and stamp(span[1])]
        return max(closes, key=stamp, default="")

    activities = []
    for activity_id, row in load("ActivityTable").items():
        span, name = window(row.get("timeId", "")), text(row.get("name"))
        if span and name:
            # tab / colour：游戏在活动页签上显示的横幅（sprites/activity/<tab>.png）及其强调色
            entry = {"id": activity_id, "name": name, "open": span[0], "close": span[1], "tab": row.get("tabImg") or "", "color": row.get("tabImgColor") or ""}
            staged = calendar_close(activity_id)
            if staged and stamp(span[1]) and stamp(staged) < stamp(span[1]):
                entry.update(close=staged, shop_close=span[1])  # 日历结束之后剩下的只有兑换
            shown = window(row.get("recommendTime", ""))
            if not span[1] and shown and stamp(shown[0]):
                # 有新关卡的常驻活动（密境行者）：新关卡开放前列为即将开启，开放时通知，
                # 仍然没有结束时间，所以不会一直留在正在开放里
                entry.update(name=f"{name}·新关卡", open=shown[0])
            activities.append(entry)

    pools = []
    char_pools = load("GachaCharPoolTable")
    for kind, table, up_key, names in (("char", "GachaCharPoolTable", "upCharIds", chars), ("weapon", "GachaWeaponPoolTable", "upWeaponIds", items)):
        for pool_id, row in load(table).items():
            suffix = re.search(r"(\d+_\d+_\d+)$", row.get("nameImage") or "")
            link = ""
            if kind == "weapon" and pool_id.startswith("rerun_wpn_"):
                # 细枝申领（rerun_wpn_tangtang）没有自己的时间：它跟着对应的角色复刻池（rerun_chr_tangtang，
                # gachapool_titleicon_1_6_3 -> time_weaponbox_1_6_3）一起开放；官方卡池页也写了那个池
                link = "rerun_chr_" + pool_id.removeprefix("rerun_wpn_")
                suffix = re.search(r"(\d+_\d+_\d+)$", (char_pools.get(link) or {}).get("nameImage") or "")
            span = window(
                f"time_{pool_id.replace('weponbox', 'weaponbox')}",
                row.get("clientTopTimeId", ""),
                f"time_rerun_{suffix.group(1)}" if suffix else "",
                f"time_weaponbox_{suffix.group(1)}" if suffix and link else "",
            )
            if not span or not span[1]:  # 常驻卡池（常驻、新手、常驻武器池）
                continue
            up = [text(names.get(i, {}).get("name")) or i for i in row.get(up_key) or []]
            pools.append({"id": pool_id, "kind": kind, "name": text(row.get("name")), "up": up, "open": span[0], "close": span[1], "link": link if link in char_pools else ""})

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
    # 每周事务（activity_weekly_task_1）：任务 id 形如 "week{N}_..."；第 N 周从开始时间 + (N-1) * 7 天起。
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


def build_gacha_pools(load, text) -> dict:
    """卡池 id -> 按 nonebot-plugin-skland 的 GachaPoolTable.json 格式写的卡池（不含横幅图：由 plugins/skland_ef_pools 补上）。

    {"pool_gacha_type": "char" | "weapon", "pool_name", "pool_type", "up6_name", "all": [{"id", "name", "rarity"}]}
    `all` 是该池能出的所有内容，取自内容表；up6_name 是 UP 干员或武器。
    """
    chars, items = load("CharacterTable"), load("ItemTable")
    pools = {}
    for kind, table, content, types, up_key, id_key, names in (
        ("char", "GachaCharPoolTable", "GachaCharPoolContentTable", CHAR_POOL_TYPES, "upCharIds", "charId", chars),
        ("weapon", "GachaWeaponPoolTable", "GachaWeaponPoolContentTable", WEAPON_POOL_TYPES, "upWeaponIds", "itemId", items),
    ):
        contents = load(content)
        for pool_id, row in load(table).items():
            pool_type = types.get(row.get("type"))
            lineup = (contents.get(pool_id) or {}).get("list") or []
            if pool_type is None or not lineup:
                continue
            up = [text((names.get(i) or {}).get("name")) for i in row.get(up_key) or []]
            pools[pool_id] = {
                "pool_gacha_type": kind,
                "pool_name": text(row.get("name")) or pool_id,  # 新池偶尔还没有名字：先用 id，免得整张表解析失败
                "pool_type": pool_type,
                "up6_name": next((name for name in up if name), ""),
                "all": [{"id": m[id_key], "name": text((names.get(m[id_key]) or {}).get("name")), "rarity": m.get("starLevel", 0)} for m in lineup],
            }
    return pools


def build_gacha_gifts(load, text) -> dict:
    """卡池 id -> 在该池抽到一定次数时游戏直接赠送的东西（不是抽出来的）。

    每项为 {"at": 第一次赠送时的抽数, "every": 间隔（一次性的为 0）,
    "items": [{"name", "icon"}]}；重复赠送的会轮流给出各项（武器池：补给箱、UP 武器、补给箱……）。
    次数按该池中付费的抽数算；免费十连不计。
    `icon` 是赠品所代表的干员 / 武器 id（如果有）。
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
    """修正类型（maps.MODIFIER_TYPE_MAP）：5 / 7 为加算，6 乘基础值，8 缩放受到的伤害（0.96 = 减少 4%）。"""
    if modifier == 8:
        return f"{(1 - value) * 100:.1f}%"
    if modifier == 6 or not flat:
        return f"{value * 100:.1f}%"
    return f"{value:.1f}".removesuffix(".0")


def _equip_attrs(equip: dict, maps: dict) -> list[dict]:
    """一件装备的三个可精锻词条，按游戏内顺序（attrIndex 1..3）。"""
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
    """按名字列出每件装备（套装件和散件）：图标和词条名，用于配装统计。"""
    equips, items = load("EquipTable"), load("ItemTable")
    result: dict[str, dict] = {}
    for equip_id, equip in equips.items():
        item = items.get(equip_id, {})
        name = text(item.get("name"))
        if name:
            result.setdefault(name, {"icon": item.get("iconId") or equip_id, "attrs": [a["name"] for a in _equip_attrs(equip, maps)]})
    return result


def build_equip_sets(load, text, maps) -> dict:
    """装备套装：三件套效果，以及每件装备在满强化前后的词条。"""
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
        pieces = [p for equip_id in row.get("equipList") or [] if (p := _equip_piece(equip_id, equips, items, text, maps))]
        if not pieces:
            continue
        _sort_pieces(pieces)
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
    sets.update(build_loose_series(equips, items, text, maps))
    return sets


def _equip_piece(equip_id: str, equips: dict, items: dict, text, maps: dict) -> dict | None:
    equip, item = equips.get(equip_id), items.get(equip_id, {})
    name = text(item.get("name"))
    if not equip or not name:
        return None
    return {
        "id": equip_id,
        "name": name,
        "part": EQUIP_PARTS.get(equip.get("partType"), ""),
        "rarity": item.get("rarity", 0),
        "level": equip.get("minWearLv", 0),
        "icon": item.get("iconId") or equip_id,
        "defense": round((equip.get("displayBaseAttrModifier") or {}).get("attrValue", 0)),
        "domain": maps["DOMAIN_MAP"].get(equip.get("domainId", ""), ""),
        "attrs": _equip_attrs(equip, maps),
    }


def _sort_pieces(pieces: list[dict]) -> None:
    order = list(EQUIP_PARTS.values())
    pieces.sort(key=lambda p: (-p["rarity"], order.index(p["part"]) if p["part"] in order else 9, p["id"]))


def build_loose_series(equips: dict, items: dict, text, maps: dict) -> dict:
    """散件（不属于套装），按游戏物品 id 的方式分系列：item_equip_t4_parts_wuling02_hand_01 -> …_wuling02。

    每个系列像套装一样存储，带 `loose: True` 且没有效果，所以 /涉渊护手 能打开它。系列名取装备名
    最常见的两字前缀（涉渊、纾难、矿场）；几个系列共用同一前缀时再加上等级。
    """
    groups: dict[str, list[dict]] = {}
    for equip_id, equip in equips.items():
        if not equip.get("suitID") and (piece := _equip_piece(equip_id, equips, items, text, maps)):
            groups.setdefault(re.sub(r"_[a-z]+_\d+$", "", equip_id), []).append(piece)
    found = []
    for series_id, pieces in sorted(groups.items()):
        _sort_pieces(pieces)
        prefix = Counter(p["name"][:2] for p in pieces).most_common(1)[0][0]
        found.append((series_id, prefix, pieces))
    shared = Counter(prefix for _, prefix, _ in found)
    series = {}
    for series_id, prefix, pieces in found:
        level = max(p["level"] for p in pieces)
        name = prefix if shared[prefix] == 1 else f"{prefix} Lv{level}"
        series[f"loose:{series_id}"] = {
            "id": f"loose:{series_id}", "name": name, "loose": True, "count": 0, "effect": "",
            "rarity": pieces[0]["rarity"], "level": level, "domain": pieces[0]["domain"], "pieces": pieces,
        }
    return series


def activity_banner_url(tab: str) -> str:
    return f"{IMAGE_BASE}/activity/{tab}.png"


def equip_icon_url(icon_id: str) -> str:
    return f"{IMAGE_BASE}/itemicon/{icon_id}.png"


def logistics_icon_url(icon_id: str) -> str:
    return f"{IMAGE_BASE}/spaceship/spaceshipskillicon/{icon_id}.png"


def term_icon_url(icon: str) -> str:
    return f"{IMAGE_BASE}/termicon/{icon}.png"


def char_icon_url(char_id: str) -> str:
    return f"{IMAGE_BASE}/charremoteicon/icon_{char_id}.png"


def weapon_icon_url(icon_id: str) -> str:
    return f"{IMAGE_BASE}/itemiconbig/{icon_id}.png"


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
