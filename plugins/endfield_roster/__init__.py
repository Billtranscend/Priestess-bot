"""Endfield account roster image (/zmd账号详情 [@用户]).

Reuses Skland's binding lookup, token refresh and card API, then renders every
owned operator with weapon, skill levels and equipment in one image.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from html import escape
from pathlib import Path

from nonebot import logger, on_command, require
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata

from plugins import ef_theme
from plugins.strict_command import strict

require("nonebot_plugin_skland")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_user")
require("nonebot_plugin_orm")

import nonebot_plugin_skland
from nonebot_plugin_alconna import UniMessage, message_reaction
from nonebot_plugin_htmlrender import html_to_pic
from nonebot_plugin_orm import async_scoped_session
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.commands.endfield.utils import check_user_character
from nonebot_plugin_skland.model import Character, SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.schemas.endfield.card import BodyEquip, EndfieldCard
from nonebot_plugin_skland.utils import refresh_access_token_if_needed, refresh_cred_token_if_needed
from nonebot_plugin_user import UserSession, get_user

__plugin_meta__ = PluginMetadata(
    name="Endfield roster",
    description="Renders an Endfield account's full operator roster.",
    usage="/zmd账号详情 [@用户]",
    type="application",
)

WIDTH = 1320
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"
RES_IMAGES = Path(nonebot_plugin_skland.__file__).parent / "resources" / "images" / "endfield"

RARITY_COLOR = {"6": "#FF7101", "5": "#FFCC00", "4": "#A85FD6", "3": "#00B0FF"}
PROPERTY_COLOR = {
    "char_property_fire": "#e5484d",
    "char_property_cryst": "#3fb6e8",
    "char_property_natural": "#3fbf6b",
    "char_property_pulse": "#e8c13f",
    "char_property_physical": "#9aa4b2",
}
PROPERTY_ICON = {
    "char_property_fire": "fire",
    "char_property_cryst": "cryst",
    "char_property_natural": "natural",
    "char_property_pulse": "pulse",
    "char_property_physical": "physical",
}
EQUIP_SLOTS = (("bodyEquip", "护甲"), ("armEquip", "护手"), ("firstAccessory", "配件Ⅰ"), ("secondAccessory", "配件Ⅱ"))

CSS = """
body { width: %dpx; padding: 24px; }
img { display: block; object-fit: contain; }
.ph { background: var(--ef-panel-2); }

/* header plate: avatar + name */
.ef-head.pl { padding-right: 230px; }
.ef-head.pl .code b small { font-family: var(--ef-cjk); font-size: 18px; font-weight: 700; letter-spacing: 0; color: var(--ef-yellow); }
.who { display: flex; gap: 18px; align-items: center; margin-top: 12px; }
.who .avatar { width: 76px; height: 76px; flex: none; border: 1px solid #6a6a64; outline: 3px solid var(--ef-dark); outline-offset: -4px; background: var(--ef-dark-2); }
.who .avatar img, .who .avatar .ph { width: 74px; height: 74px; }
.who .avatar .ph { background: var(--ef-dark-2); }
.who h1 { margin-top: 0; font-size: 36px; }
.who p { margin-top: 4px; font-size: 16px; }

/* status tiles */
.tiles { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-top: 16px; }
.tiles div { position: relative; background: var(--ef-panel); border: 1px solid var(--ef-line); border-top: 3px solid var(--ef-ink); padding: 9px 14px 10px; }
.tiles div:after { content: ""; position: absolute; right: 0; top: 0; width: 18px; height: 4px; background: var(--ef-yellow); }
.tiles small { font-size: 14px; font-weight: 700; color: var(--ef-ink); }
.tiles b { display: block; margin-top: 2px; font-family: var(--ef-num); font-weight: 700; font-size: 32px; line-height: 1.1; letter-spacing: .5px; }
.tiles i { font-style: normal; font-size: 13px; color: var(--ef-sub); }

/* operator rows */
.row { display: grid; grid-template-columns: 280px 220px 240px 290px 1fr; gap: 12px; align-items: center;
  padding: 12px 16px; border-top: 1px solid var(--ef-line-2); }
.ef-sec > h3 + .row { border-top: 0; }
.row:nth-child(odd) { background: #efefeb; }
.op { display: flex; gap: 12px; align-items: center; }
.av { position: relative; width: 66px; height: 66px; flex: none; background: var(--ef-panel-2);
  border: 1px solid var(--ef-ink); border-top: 3px solid; }
.av img, .av .ph { width: 64px; height: 62px; object-fit: cover; }
.av span { position: absolute; left: -1px; bottom: -1px; background: var(--ef-ink); color: #f4f4f0; font-family: var(--ef-num);
  font-weight: 600; font-size: 14px; line-height: 17px; padding: 0 5px; letter-spacing: .3px; }
.nm { font-size: 20px; font-weight: 900; display: flex; align-items: center; gap: 7px; }
.nm b { font-family: var(--ef-num); font-weight: 700; font-size: 13px; line-height: 17px; padding: 0 5px;
  border: 1px solid var(--ef-ink); background: var(--ef-panel); color: var(--ef-ink); }
.nm b.r6 { background: var(--ef-yellow); } .nm b.r5 { background: var(--ef-ink); color: #f4f4f0; }
.tags { display: flex; gap: 5px; align-items: center; margin-top: 5px; }
.tags span { display: inline-flex; align-items: center; gap: 3px; font-size: 12px; line-height: 18px; padding: 0 6px;
  background: var(--ef-panel); border: 1px solid var(--ef-line); color: #3c3c38; }
.tags span.pr { border-left: 3px solid; padding-left: 4px; }
.tags .pi { width: 14px; height: 14px; }
.pot { margin-top: 6px; font-size: 12px; color: var(--ef-sub); display: flex; align-items: center; gap: 3px; }
.pot i { display: inline-block; width: 14px; height: 7px; background: var(--ef-line-2); border: 1px solid var(--ef-line); margin-left: 1px; }
.pot i:first-of-type { margin-left: 4px; }
.pot i.on { background: var(--ef-yellow); border-color: var(--ef-ink); }
.pot small { margin-left: 6px; font-family: var(--ef-num); font-weight: 700; font-size: 16px; line-height: 1; color: var(--ef-ink); }
.wp { display: flex; gap: 10px; align-items: center; }
.wp .wp-icon { width: 54px; height: 54px; flex: none; background: var(--ef-panel-2); border: 1px solid var(--ef-line); padding: 2px; }
.wp b { display: block; font-size: 16px; }
.wp small { display: block; margin-top: 1px; font-size: 12px; color: var(--ef-sub); }
.wp small i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 15px; color: var(--ef-ink); }
.wp u { display: block; height: 3px; width: 80px; margin-top: 5px; }
.wp.empty, .dim { color: var(--ef-faint); font-size: 14px; }
.skills { display: flex; gap: 10px; }
.skill { display: flex; flex-direction: column; align-items: center; }
.sk-icon { width: 48px; height: 48px; padding: 6px; background: var(--ef-dark-2); border: 1px solid var(--ef-ink); }
.sk-icon img, .sk-icon .ph { width: 34px; height: 34px; }
.sk-icon .ph { background: #3a3b3f; }
.mastery { display: flex; justify-content: center; align-items: center; margin-top: 3px; width: 48px; height: 20px; background: var(--ef-ink); }
.mastery polygon { fill: #55565b; } .mastery polygon.on { fill: var(--ef-yellow); }
.skill .lv { display: block; margin-top: 3px; width: 48px; height: 20px; line-height: 20px; text-align: center; font-family: var(--ef-num);
  font-weight: 600; font-size: 15px; background: var(--ef-panel); border: 1px solid var(--ef-line); color: var(--ef-ink); }
.equips { display: flex; gap: 6px; }
.slot { width: 50px; height: 50px; border: 1px solid var(--ef-line); border-top: 3px solid; background: #fbfbf9;
  display: flex; align-items: center; justify-content: center; }
.slot img { width: 42px; height: 42px; }
.slot.empty { border: 1px dashed var(--ef-line); color: var(--ef-faint); font-size: 11px; background: transparent; }
.suit { font-size: 13px; color: var(--ef-ink); line-height: 1; }
.suit em { display: inline-block; font-style: normal; line-height: 19px; background: var(--ef-panel); border: 1px solid var(--ef-ink);
  border-left: 4px solid var(--ef-yellow); padding: 0 6px; margin: 2px 4px 2px 0; white-space: nowrap; }
"""


roster = on_command("zmd账号详情", aliases={"ef账号详情", "终末地账号详情", "账号详情"}, rule=strict, priority=5, block=True)


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


@refresh_cred_token_if_needed
@refresh_access_token_if_needed
async def _fetch_card(user: SkUser, char: Character) -> EndfieldCard:
    return await SklandAPI.endfield_card(CRED(cred=user.cred, token=user.cred_token), user.user_id, char)


def _rarity(key: str) -> str:
    return key.rsplit("_", 1)[-1] if key else ""


def _img(url: str, cls: str = "") -> str:
    return f'<img class="{cls}" src="{escape(url)}">' if url else f'<div class="{cls} ph"></div>'


def _local(path: str) -> str:
    file = RES_IMAGES / path
    return file.as_uri() if file.is_file() else ""


def _equip_cell(equip: BodyEquip | None, label: str) -> str:
    if not equip or not equip.equipData or not equip.equipData.iconUrl:
        return f'<div class="slot empty">{label}</div>'
    data = equip.equipData
    color = RARITY_COLOR.get(_rarity(data.rarity.key), "#5a6478")
    return f'<div class="slot" style="border-top-color:{color}" title="{escape(data.name)}">{_img(data.iconUrl)}</div>'


# Skill levels 10-12 are mastery ranks; the game shows them as three hexagons lit one by one.
_HEX_POINTS = ((9.5, 9.0), (19.0, 4.5), (19.0, 13.5))


def _hexagon(cx: float, cy: float, r: float = 4.6) -> str:
    return " ".join(f"{cx + r * x:.2f},{cy + r * y:.2f}" for x, y in ((1, 0), (0.5, 0.866), (-0.5, 0.866), (-1, 0), (-0.5, -0.866), (0.5, -0.866)))


def _mastery_badge(level: int) -> str:
    lit = min(level - 9, 3)
    hexes = "".join(
        f'<polygon points="{_hexagon(x, y)}" class="{"on" if i < lit else ""}"/>' for i, (x, y) in enumerate(_HEX_POINTS)
    )
    return f'<div class="mastery"><svg viewBox="0 0 28 18" width="28" height="18">{hexes}</svg></div>'


def _skill_cells(char) -> tuple[str, int]:
    cells, matched = [], 0
    for skill in char.charData.skills[:4]:
        level = char.userSkills.get(skill.id)
        matched += level is not None
        badge = _mastery_badge(level.level) if level and level.level > 9 else f'<span class="lv">{f"Lv{level.level}" if level else "Lv?"}</span>'
        cells.append(
            f'<div class="skill"><div class="sk-icon">{_img(skill.iconUrl)}</div>{badge}</div>'
        )
    return "".join(cells), matched


def _row(char) -> tuple[str, int]:
    data = char.charData
    rarity = _rarity(data.rarity.key)
    color = PROPERTY_COLOR.get(data.property.key, "#5a6478")
    potential = "".join(f'<i class="{"on" if i < char.potentialLevel else ""}"></i>' for i in range(5))
    prop_icon = _local(f"property/{PROPERTY_ICON.get(data.property.key, '')}.png")

    weapon = char.weapon.weaponData if char.weapon else None
    weapon_html = '<div class="wp empty">未装备武器</div>'
    if weapon and weapon.name:
        wcolor = RARITY_COLOR.get(_rarity(weapon.rarity.key), "#5a6478")
        weapon_html = (
            f'<div class="wp">{_img(weapon.iconUrl, "wp-icon")}<div><b>{escape(weapon.name)}</b>'
            f'<small><i>Lv.{char.weapon.level}</i> · 潜能 <i>{char.weapon.refineLevel}</i></small>'
            f'<u style="background:{wcolor}"></u></div></div>'
        )

    equips = [getattr(char, attr) for attr, _ in EQUIP_SLOTS]
    suits = Counter(e.equipData.suit.name for e in equips if e and e.equipData and e.equipData.suit and e.equipData.suit.name)
    suit_text = " ".join(f"<em>{escape(n)} ×{c}</em>" for n, c in suits.most_common()) or '<span class="dim">未成套</span>'
    tactical = char.tacticalItem.tacticalItemData if char.tacticalItem else None
    tactical_html = (
        f'<div class="slot" title="{escape(tactical.name)}">{_img(tactical.iconUrl)}</div>'
        if tactical and tactical.iconUrl
        else '<div class="slot empty">道具</div>'
    )
    skills_html, matched = _skill_cells(char)

    html = f"""
<div class="row">
  <div class="op">
    <div class="av" style="border-top-color:{RARITY_COLOR.get(rarity, '#5a6478')}">{_img(data.avatarSqUrl)}<span>Lv.{char.level}</span></div>
    <div class="info">
      <div class="nm">{escape(data.name)}<b class="r{rarity}">{rarity}★</b></div>
      <div class="tags"><span class="pr" style="border-left-color:{color}">{_img(prop_icon, "pi") if prop_icon else ""}{escape(data.property.value)}</span>
        <span>{escape(data.profession.value)}</span><span>{escape(data.weaponType.value)}</span></div>
      <div class="pot">潜能 {potential} <small>{char.potentialLevel}</small></div>
    </div>
  </div>
  {weapon_html}
  <div class="skills">{skills_html}</div>
  <div class="equips">{"".join(_equip_cell(e, label) for e, (_, label) in zip(equips, EQUIP_SLOTS))}{tactical_html}</div>
  <div class="suit">{suit_text}</div>
</div>"""
    return html, matched


def _render(card: EndfieldCard) -> tuple[str, int, int]:
    base = card.base
    chars = sorted(
        card.chars,
        key=lambda c: (-int(_rarity(c.charData.rarity.key) or 0), -c.level, c.charData.id),
    )
    rows, matched, total_skills = [], 0, 0
    for char in chars:
        html, m = _row(char)
        rows.append(html)
        matched += m
        total_skills += min(len(char.charData.skills), 4)

    dungeon = card.dungeon
    cur, mx = int(dungeon.curStamina or 0), int(dungeon.maxStamina or 0)
    now = float(card.currentTs) if card.currentTs else time.time()
    remain = max(0.0, float(dungeon.maxTs) - now) if dungeon.maxTs else 0.0
    stamina_note = "已回满" if cur >= mx or remain <= 0 else f"{int(remain // 3600)}小时{int(remain % 3600 // 60)}分后回满"
    uid = base.roleId[-4:].rjust(len(base.roleId), "*") if base.roleId else ""

    tiles = [
        ("理智", f"{cur} / {mx}", stamina_note),
        ("日常", f"{card.dailyMission.dailyActivation} / {card.dailyMission.maxDailyActivation}", "活跃度"),
        ("周常", f"{card.weeklyMission.score} / {card.weeklyMission.total}", "分数"),
        ("通行证", f"{card.bpSystem.curLevel} / {card.bpSystem.maxLevel}", "等级"),
        ("干员", str(base.charNum or len(card.chars)), "已拥有"),
        ("武器", str(base.weaponNum), "已拥有"),
    ]
    tiles_html = "".join(f"<div><small>{a}</small><b>{b}</b><i>{c}</i></div>" for a, b, c in tiles)
    mission = f" · 主线「{escape(base.mainMission.description)}」" if base.mainMission.description else ""

    html = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS % WIDTH}</style></head><body class="ef">'
        f"""<div class="ef-head pl">
  <div class="code">ROSTER<b>{len(chars)} <small>位干员</small></b></div>
  <div class="k">Endfield · Account Roster</div>
  <div class="who">
    <div class="avatar">{_img(base.avatarUrl)}</div>
    <div><h1>{escape(base.name)}</h1>
      <p>UID {uid} · {escape(base.serverName)} · 权限等级 {base.level} · 探索等级 {base.worldLevel}{mission}</p></div>
  </div>
</div>
<div class="tiles">{tiles_html}</div>
<div class="ef-sec"><h3>干员数据<small class="cjk">按稀有度、等级与干员编号排序</small></h3>
{"".join(rows)}
</div>"""
        + ef_theme.foot(f'数据来源 森空岛<br>生成时间 {time.strftime("%Y-%m-%d %H:%M", time.localtime(now))}')
        + "</body></html>"
    )
    return html, matched, total_skills


@roster.handle()
async def _(event: MessageEvent, user_session: UserSession, session: async_scoped_session) -> None:
    at = next((seg.data.get("qq") for seg in event.message if seg.type == "at" and seg.data.get("qq") != "all"), None)
    target_id = (await get_user(user_session.platform, str(at))).id if at else user_session.user_id
    user, character = await check_user_character(target_id, session)

    await _react(REACTION_PROCESSING)
    try:
        card = await _fetch_card(user, character)
        if not card:
            await _react(REACTION_FAIL)
            return
        html, matched, total_skills = _render(card)
        logger.info(f"Endfield roster: {len(card.chars)} operators, skill levels matched {matched}/{total_skills}")
        image = await html_to_pic(
            html, type="jpeg", quality=82, device_scale_factor=1, viewport={"width": WIDTH, "height": 800}
        )
        image = await asyncio.to_thread(ef_theme.shrink, image, 0.85)
        await UniMessage.image(raw=image).send()
        await session.commit()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Endfield roster failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await roster.finish("账号详情生成失败，请稍后再试")
    await _react(REACTION_DONE)
