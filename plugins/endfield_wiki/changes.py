"""What a new data version added, and what looks wrong in a freshly built index.

Pure functions over index.json, used for the update notice in the groups and for the private
alert to the superusers.
"""

from __future__ import annotations

import re

LIST_LIMIT = 12  # names per line in the group notice


def names(index: dict) -> dict[str, list[str]]:
    """Category -> names in the index's own order."""
    endgame = index.get("endgame") or {}
    monuments = [
        f'{series.get("name", "")}（{"、".join(group.get("name", "") for group in series.get("groups", []))}）'
        for series in endgame.get("monument", [])
    ]
    return {
        "干员": [op.get("name", "") for op in (index.get("operators") or {}).values()],
        "武器": [weapon.get("name", "") for weapon in (index.get("weapons") or {}).values()],
        "套装": [equip_set.get("name", "") for equip_set in (index.get("equip_sets") or {}).values()],
        "战争回响": [group.get("name", "") for group in (endgame.get("tower") or {}).values()],
        "影拓丰碑": monuments,
    }


def added(old: dict, new: dict) -> dict[str, list[str]]:
    """Names the new index has and the old one lacked. A monument that gained a stage counts as new, with all its stages."""
    before, after = names(old), names(new)
    return {kind: [name for name in after[kind] if name and name not in set(before.get(kind, ()))] for kind in after}


def _joined(items: list[str]) -> str:
    shown = "、".join(items[:LIST_LIMIT])
    return shown if len(items) <= LIST_LIMIT else f"{shown} 等 {len(items)} 个"


def notice_lines(new_names: dict[str, list[str]]) -> list[str]:
    """The body of the group notice: one line per category with something new, then how to look things up."""
    lines = [f"新增{kind}：{_joined(items)}" for kind, items in new_names.items() if items]
    if not lines:
        return ["本次没有新增干员、武器、套装或关卡，已有资料的数值和文本已同步更新", "发送 /干员名 或 /武器名 可查看最新资料，例如 /提丰、/提丰专武"]
    examples = [f"/{new_names[kind][0]}" for kind in ("干员", "武器", "套装") if new_names.get(kind)]
    if examples:
        lines.append(f'现在就可以查：发送 /名称 查看资料卡，例如 {"、".join(examples)}')
    stage = next((new_names[kind][0].split("（")[0] for kind in ("战争回响", "影拓丰碑") if new_names.get(kind)), "")
    if stage:
        lines.append(f"关卡发 /攻略 {stage} 看机制、敌人与阵容，/敌人 {stage} 只看敌人属性")
    return lines


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def problems(index: dict) -> list[str]:
    """Signs that the game data uses something this code does not read yet: a card would show a blank or a hole."""
    found: list[str] = []
    for kind, minimum in (("operators", 1), ("weapons", 1), ("equip_sets", 1)):
        if len(index.get(kind) or {}) < minimum:
            found.append(f"索引里没有任何{ {'operators': '干员', 'weapons': '武器', 'equip_sets': '套装'}[kind] }")
    for op in (index.get("operators") or {}).values():
        who = f'干员「{op.get("name", "?")}」'
        if not op.get("skills"):
            found.append(f"{who}没有任何技能")
        for skill in op.get("skills", []):
            texts = [skill.get("desc", "")] + [form.get("desc", "") for form in skill.get("forms", [])]
            if not any(_plain(t) for t in texts):
                found.append(f'{who}的{skill.get("type", "技能")}「{skill.get("name", "?")}」没有说明')
            elif any("{" in _plain(t) or "?" in _plain(t) for t in texts):
                found.append(f'{who}的{skill.get("type", "技能")}「{skill.get("name", "?")}」有没填上的数值')
        for label, rows in (("天赋", op.get("talents", [])), ("潜能", op.get("potentials", []))):
            for row in rows:
                if not _plain(row.get("desc", "")):
                    found.append(f'{who}的{label}「{row.get("name", "?")}」没有说明')
                elif "{" in _plain(row["desc"]) or "?" in _plain(row["desc"]):
                    found.append(f'{who}的{label}「{row.get("name", "?")}」有没填上的数值')
    for weapon in (index.get("weapons") or {}).values():
        for skill in weapon.get("skills", []):
            levels = skill.get("levels", [])
            if not levels or not all(_plain(level) for level in levels):
                found.append(f'武器「{weapon.get("name", "?")}」的技能「{skill.get("name", "?")}」没有说明')
            elif any("{" in _plain(level) or "?" in _plain(level) for level in levels):
                found.append(f'武器「{weapon.get("name", "?")}」的技能「{skill.get("name", "?")}」有没填上的数值')
    return list(dict.fromkeys(found))  # the two 管理员 entries share their texts
