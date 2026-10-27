"""Arknights account card (/mrfz开盒 [@用户] [-r 序号]): the 明日方舟 counterpart of /zmd开盒.

Reuses Skland's binding lookup, token refresh and Arknights card API, then draws the account
overview (sanity, missions, annihilation, base …), the assist operators and every owned operator in
one picture, in the game's own terminal style (plugins/ark_theme). Replaces upstream's card under the
/mrfz开盒 /mrfz卡片 /mrfz查询 names.

Every image comes from local files (upstream's resource folder and its avatar / skill icon cache):
upstream's operator roster drew remote portraits and timed out when several members asked at once
(2026-10-26). A skin avatar that is not cached falls back to the operator's default avatar; a default
avatar that is not cached yet (operators newer than the cache) is fetched into the cache before the
page is drawn, with a short time limit, and left blank if that fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from html import escape
from pathlib import Path

import httpx
from nonebot import logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.consts import PREFIX_KEY, RAW_CMD_KEY
from nonebot.exception import MatcherException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.typing import T_State

from plugins import ark_theme, ef_theme

require("nonebot_plugin_skland")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_user")
require("nonebot_plugin_orm")
require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as store
import nonebot_plugin_skland
from nonebot_plugin_alconna import UniMessage, message_reaction
from nonebot_plugin_orm import async_scoped_session
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.commands.selection import check_user_character
from nonebot_plugin_skland.data_source import gacha_table_data
from nonebot_plugin_skland.model import Character, SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.schemas.arknights.card import ArkCard
from nonebot_plugin_skland.services.auth import refresh_credentials
from nonebot_plugin_user import UserSession, get_user

__plugin_meta__ = PluginMetadata(
    name="Arknights roster",
    description="Renders an Arknights account's overview, base and every owned operator.",
    usage="/mrfz开盒 [@用户] [-r 序号]",
    type="application",
)

WIDTH = 1320
SCALE = 1.25  # 161 operators at 1.5x were 783 KB; 1.25x keeps the tiles sharp at ~70% of the bytes
QUALITY = 55
# Big rosters get a denser wall and a lower scale: upload to QQ ran at ~30 KB/s on 2026-10-27, and a
# 434-operator card at the normal layout was 1838 KB (61 s upload).
DENSE_FROM = 200  # operators
DENSE_SCALE = 1.0
DENSE_QUALITY = 50
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"
ROLE = re.compile(r"(?:-r|--role)\s*(\d+)")
RES = Path(nonebot_plugin_skland.__file__).parent / "resources" / "images"
CACHE = store.get_cache_dir("nonebot_plugin_skland")
AVATAR_URL = "https://web.hycdn.cn/arknights/game/assets/char/avatar/{}.png"  # as upstream's charId_to_avatarUrl
AVATAR_TIMEOUT = 6  # seconds per avatar; the page is drawn without the ones that are still missing
PROFESSION_ICON = {"先锋": "pioneer", "近卫": "warrior", "重装": "tank", "狙击": "sniper",
                   "术师": "caster", "医疗": "medic", "辅助": "support", "特种": "special"}

CSS = """
body { width: %dpx; padding: 24px; }
img { display: block; object-fit: contain; }
.ph { background: var(--ak-panel-2); }

/* head: doctor + secretary */
.ak-head.pl { padding-right: 260px; }
.ak-head.pl .code b small { font-family: var(--ak-cjk); font-size: 17px; font-weight: 700; letter-spacing: 0; color: var(--ak-blue); }
.who { display: flex; gap: 18px; align-items: center; margin-top: 12px; }
.who .avatar { width: 78px; height: 78px; flex: none; border: 1px solid var(--ak-line-2); background: var(--ak-panel-2); position: relative; }
.who .avatar:after { content: ""; position: absolute; right: -1px; bottom: -1px; width: 12px; height: 12px; background: var(--ak-blue); clip-path: polygon(100%% 0, 100%% 100%%, 0 100%%); }
.who .avatar img, .who .avatar .ph { width: 76px; height: 76px; }
.who h1 { margin-top: 0; font-size: 36px; }
.who p { margin-top: 4px; font-size: 16px; }
.sec { position: absolute; right: 26px; bottom: 26px; display: flex; gap: 10px; align-items: center; }
.sec img, .sec .ph { width: 52px; height: 52px; border: 1px solid var(--ak-line-2); }
.sec small { display: block; font-family: var(--ak-num); font-size: 12px; letter-spacing: 3px; color: var(--ak-faint); text-align: right; }
.sec b { display: block; font-size: 16px; text-align: right; }

/* status tiles */
.tiles { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-top: 16px; }
.tiles div { position: relative; background: var(--ak-panel); border: 1px solid var(--ak-line); padding: 9px 14px 10px; }
.tiles div:before { content: ""; position: absolute; left: -1px; top: -1px; width: 26px; height: 3px; background: var(--ak-blue); }
.tiles small { font-size: 14px; font-weight: 700; color: var(--ak-sub); }
.tiles b { display: block; margin-top: 2px; font-family: var(--ak-num); font-weight: 700; font-size: 32px; line-height: 1.1; letter-spacing: .5px; color: #fff; }
.tiles b span { font-size: 20px; color: var(--ak-faint); }
.tiles i { font-style: normal; font-size: 13px; color: var(--ak-faint); }

/* assist operators */
.assist { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; padding: 14px 16px 16px; }
.as { display: flex; gap: 14px; background: var(--ak-panel-2); border: 1px solid var(--ak-line); padding: 10px; }
.as .pic { width: 88px; height: 88px; flex: none; position: relative; border-bottom: 4px solid; background: #111; }
.as .pic img, .as .pic .ph { width: 88px; height: 84px; object-fit: cover; }
.as .nm { font-size: 20px; font-weight: 900; color: #fff; }
.as .meta { margin-top: 4px; display: flex; gap: 6px; align-items: center; font-size: 13px; color: var(--ak-sub); }
.as .meta img { width: 22px; height: 22px; }
.as .meta b { font-family: var(--ak-num); font-size: 19px; color: #fff; }
.as .sk { margin-top: 6px; display: flex; gap: 8px; align-items: center; font-size: 13px; color: var(--ak-sub); }
.as .sk img, .as .sk .ph { width: 34px; height: 34px; border: 1px solid var(--ak-line-2); }

/* base */
.base { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px; background: var(--ak-line); border-top: 1px solid var(--ak-line); }
.base div { background: var(--ak-panel); padding: 10px 14px; display: flex; gap: 10px; align-items: center; }
.base img { width: 30px; height: 30px; opacity: .85; }
.base small { display: block; font-size: 13px; color: var(--ak-sub); }
.base b { display: block; font-family: var(--ak-num); font-size: 22px; line-height: 1.15; color: #fff; }
.base b span { font-family: var(--ak-cjk); font-size: 13px; font-weight: 400; color: var(--ak-faint); }

/* operator wall */
.stats { display: flex; gap: 8px; padding: 12px 16px 0; }
.wall { display: grid; grid-template-columns: repeat(10, 1fr); gap: 8px; padding: 12px 16px 16px; }
.op { background: #121214; border: 1px solid var(--ak-line); position: relative; }
.op .pic { position: relative; height: 112px; overflow: hidden; }
.op .pic:after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 46px; background: linear-gradient(transparent, rgba(0,0,0,.85)); }
.op .pic > img.av, .op .pic > .ph { width: 100%%; height: 112px; object-fit: cover; }
.op .pf { position: absolute; left: 3px; top: 3px; width: 22px; height: 22px; z-index: 2; background: rgba(0,0,0,.55); padding: 2px; }
.op .el { position: absolute; right: 3px; top: 3px; width: 24px; height: 24px; z-index: 2; }
.op .pt { position: absolute; right: 3px; bottom: 4px; width: 22px; height: 22px; z-index: 2; }
.op .lv { position: absolute; left: 5px; bottom: 2px; z-index: 2; font-family: var(--ak-num); font-weight: 700; font-size: 26px; line-height: 1; color: #fff; }
.op .lv small { font-size: 11px; color: var(--ak-sub); margin-right: 1px; letter-spacing: 1px; }
.op .bar { height: 4px; }
.op .nm { padding: 4px 6px 0; font-size: 14px; font-weight: 700; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.op .ln { display: flex; gap: 3px; align-items: center; padding: 3px 6px 6px; min-height: 25px; }
.op .m { font-family: var(--ak-num); font-weight: 700; font-size: 12px; line-height: 16px; padding: 0 4px; border: 1px solid var(--ak-line-2); color: var(--ak-faint); }
.op .m.on { border-color: var(--ak-blue); color: #fff; background: var(--ak-blue-2); }
.op .sl { font-family: var(--ak-num); font-size: 13px; color: var(--ak-sub); }
.wall.dense { grid-template-columns: repeat(14, 1fr); gap: 6px; }
.dense .op .pic, .dense .op .pic > img.av, .dense .op .pic > .ph { height: 80px; }
.dense .op .pic:after { height: 34px; }
.dense .op .pf { width: 18px; height: 18px; } .dense .op .el, .dense .op .pt { width: 19px; height: 19px; }
.dense .op .lv { font-size: 20px; } .dense .op .lv small { font-size: 9px; }
.dense .op .nm { font-size: 12px; padding: 3px 4px 0; }
.dense .op .ln { padding: 2px 4px 4px; min-height: 20px; gap: 2px; }
.dense .op .m, .dense .op .md { font-size: 11px; line-height: 14px; padding: 0 3px; }
.dense .op .sl { font-size: 11px; }
.op .md { margin-left: auto; font-family: var(--ak-num); font-weight: 700; font-size: 12px; line-height: 16px; padding: 0 4px; color: #111; background: var(--ak-yellow); }
""" % WIDTH


def _local(path: Path) -> str:
    return path.as_uri() if path.is_file() else ""


def _res(path: str) -> str:
    return _local(RES / path)


def _avatar(char_id: str, skin_id: str = "") -> str:
    """Cached avatar of the skin, else of the operator (file names as upstream's charId_to_avatarUrl)."""
    for key in (skin_id, char_id):
        if not key:
            continue
        name = next((key.replace(symbol, "_", 1) for symbol in ("@", "#") if symbol in key), key)
        if url := _local(CACHE / "avatar" / f"{name}.png"):
            return url
    return ""


async def fill_avatars(char_ids: set[str]) -> int:
    """Download the default avatars of these operators that are not cached yet; returns how many were added."""
    folder = CACHE / "avatar"
    missing = [c for c in char_ids if c and not (folder / f"{c}.png").is_file()]
    if not missing:
        return 0
    folder.mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(4)

    async def fetch(client: httpx.AsyncClient, char_id: str) -> bool:
        async with gate:
            try:
                response = await client.get(AVATAR_URL.format(char_id))
            except httpx.HTTPError:
                return False
        if response.status_code != 200 or not response.content.startswith(b"\x89PNG"):
            return False
        tmp = folder / f".{char_id}.png.tmp"
        tmp.write_bytes(response.content)
        tmp.replace(folder / f"{char_id}.png")
        return True

    async with httpx.AsyncClient(timeout=AVATAR_TIMEOUT) as client:
        added = sum(await asyncio.gather(*(fetch(client, c) for c in missing)))
    logger.info(f"Arknights roster: cached {added}/{len(missing)} missing avatars")
    return added


def _skill_icon(skill_id: str) -> str:
    return _local(CACHE / "skill" / f"skill_icon_{skill_id}.png")


def _img(url: str, cls: str = "") -> str:
    return f'<img class="{cls}" src="{escape(url)}">' if url else f'<div class="{cls} ph"></div>'


def _rarity(entry) -> int:
    return (entry.rarity + 1) if entry else 0


def _module(char, card: ArkCard) -> str:
    """The selected module when it is a branch module (not the original 证章), as its type and stage."""
    equip_id = char.defaultEquipId
    info = card.equipmentInfoMap.get(equip_id) if equip_id else None
    if not info or info.typeIcon == "original":
        return ""
    level = next((e.level for e in char.equip if e.id == equip_id), 1)
    branch = info.typeIcon.rsplit("-", 1)[-1].upper()  # "dec-y" -> Y; the third branch is written d -> Δ
    return f"{'Δ' if branch == 'D' else branch}{level}"


def _op_tile(char, card: ArkCard, entry) -> str:
    rarity = _rarity(entry)
    color = ark_theme.RARITY_COLOR.get(rarity, "#777")
    name = entry.name if entry else card.charInfoMap.get(char.charId).name if char.charId in card.charInfoMap else char.charId
    prof = _res(f"profession/icon_profession_{PROFESSION_ICON.get(entry.profession, '')}.png") if entry else ""
    elite = _res(f"ark_card/elite/elite_{char.evolvePhase}.png") if char.evolvePhase else ""
    potential = _res(f"ark_card/potential/potential_{char.potentialRank}.png") if char.potentialRank else ""
    masteries = [s.specializeLevel for s in char.skills]
    if any(masteries):
        skills = "".join(f'<span class="m{" on" if m else ""}">{f"M{m}" if m else "-"}</span>' for m in masteries)
    else:
        skills = f'<span class="sl">技能 {char.mainSkillLvl}</span>' if char.skills else ""
    module = _module(char, card)
    return (
        f'<div class="op"><div class="pic">{_img(_avatar(char.charId, char.skinId), "av")}'
        f'{_img(prof, "pf") if prof else ""}{_img(elite, "el") if elite else ""}{_img(potential, "pt") if potential else ""}'
        f'<div class="lv"><small>LV</small>{char.level}</div></div>'
        f'<div class="bar" style="background:{color}"></div>'
        f'<div class="nm">{escape(name)}</div>'
        f'<div class="ln">{skills}{f"<span class=md>{escape(module)}</span>" if module else ""}</div></div>'
    )


def _assist(char, card: ArkCard, catalog) -> str:
    entry = catalog.get(char.charId)
    rarity = _rarity(entry)
    name = entry.name if entry else card.charInfoMap[char.charId].name if char.charId in card.charInfoMap else char.charId
    elite = _res(f"ark_card/elite/elite_{char.evolvePhase}.png")
    potential = _res(f"ark_card/potential/potential_{char.potentialRank}.png") if char.potentialRank else ""
    mastery = f"专精 {char.specializeLevel}" if char.specializeLevel else f"技能 {char.mainSkillLvl}"
    module = card.equipmentInfoMap.get(char.equip.id) if char.equip else None
    module_text = f" · 模组 {module.typeIcon.upper()} 等级{char.equip.level}" if module and module.typeIcon != "original" else ""
    return (
        f'<div class="as"><div class="pic" style="border-bottom-color:{ark_theme.RARITY_COLOR.get(rarity, "#777")}">{_img(_avatar(char.charId, char.skinId))}</div>'
        f'<div><div class="nm">{escape(name)}</div>'
        f'<div class="meta">{_img(elite) if elite else ""}<b>Lv.{char.level}</b>{_img(potential) if potential else ""}<span>{rarity}★</span></div>'
        f'<div class="sk">{_img(_skill_icon(char.skillId))}<span>{mastery}{module_text}</span></div></div></div>'
    )


def _countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}天{seconds % 86400 // 3600}小时"
    return f"{seconds // 3600}小时{seconds % 3600 // 60}分"


def _base(card: ArkCard, now: float) -> str:
    b = card.building
    icon = lambda n: _res(f"ark_card/building/{n}.png")
    levels = lambda rooms: "·".join(str(r.level) for r in rooms) or "-"
    trading_limit = b.trading_stock_limit if hasattr(b, "trading_stock_limit") else 0
    training = b.training
    trainee = card.trainee_char
    cells = [
        (icon("manufact"), "制造站", f"{len(b.manufactures)}<span> 座 Lv{levels(b.manufactures)}</span>"),
        (icon("trading"), "贸易站", f"{b.trading_stock}/{trading_limit}<span> 订单</span>"),
        (icon("dorm"), "宿舍", f"{len(b.dormitories)}<span> 间 Lv{levels(b.dormitories)}</span>"),
        (icon("meeting"), "会客室", f"{len(b.meeting.clue.board) if b.meeting else 0}/7<span> 线索</span>"),
        (icon("labor"), "无人机", f"{b.labor.labor_now}/{b.labor.maxValue}"),
        ("", "控制中枢", f"Lv{b.control.level}"),
        ("", "发电站", f"{len(b.powers)}<span> 座 Lv{levels(b.powers)}</span>"),
        ("", "办公室", f"{b.hire.refreshCount if b.hire else 0}/3<span> 次刷新</span>"),
        ("", "训练室", f"{escape(trainee) if trainee else ('空闲' if training else '未建造')}"),
        (icon("tired"), "疲劳干员", f"{len(b.tiredChars)}<span> 位</span>"),
    ]
    return "".join(f'<div>{_img(i) if i else ""}<div><small>{a}</small><b>{v}</b></div></div>' for i, a, v in cells)


def render(card: ArkCard, catalog: dict, now: float | None = None) -> str:
    now = now or time.time()
    st = card.status
    chars = sorted(card.chars, key=lambda c: (-_rarity(catalog.get(c.charId)), -c.evolvePhase, -c.level, -c.potentialRank, c.charId))
    tiles_html = ""
    ap_max = st.ap.max  # above the cap (potions, rewards) the stored value is the real one; upstream's ap_now caps it
    ap_now = st.ap.current if st.ap.current >= ap_max else st.ap.ap_now
    ap_note = "已回满" if ap_now >= ap_max or st.ap.completeRecoveryTime <= now else f"{_countdown(st.ap.completeRecoveryTime - now)}后回满"
    recruit_total = len(card.recruit)
    tower = card.tower.reward
    tiles = [
        ("理智", f"{ap_now}<span>/{ap_max}</span>", ap_note),
        ("日常任务", f"{card.routine.daily.current}<span>/{card.routine.daily.total}</span>", "已完成"),
        ("周常任务", f"{card.routine.weekly.current}<span>/{card.routine.weekly.total}</span>", "已完成"),
        ("剿灭作战", f"{card.campaign.reward.current}<span>/{card.campaign.reward.total}</span>", "本周合成玉"),
        ("保全派驻", f"{tower.higherItem.current}<span>/{tower.higherItem.total}</span>", f"{_countdown(tower.termTs - now)}后结束" if tower.termTs > now else "本期已结束"),
        ("公开招募", f"{card.recruit_finished}<span>/{recruit_total}</span>", escape(card.recruit_complete_time) if recruit_total else "未开放"),
        ("干员", str(st.charCnt or len(card.chars)), "已招募"),
        ("时装", str(st.skinCnt), "已拥有"),
        ("家具", str(max(st.furnitureCnt, card.building.furniture.total)), "已拥有"),
        ("蚀刻章", str(card.medal.total), "已获得"),
    ]
    tiles_html = "".join(f"<div><small>{a}</small><b>{b}</b><i>{c}</i></div>" for a, b, c in tiles)

    e2 = sum(1 for c in card.chars if c.evolvePhase == 2)
    six = sum(1 for c in card.chars if _rarity(catalog.get(c.charId)) == 6)
    m3 = sum(1 for c in card.chars for s in c.skills if s.specializeLevel == 3)
    modules = sum(1 for c in card.chars if _module(c, card))
    stats = (f'<span class="ak-tag b">6★ {six}</span><span class="ak-tag">精英二 {e2}</span>'
             f'<span class="ak-tag">专精三技能 {m3}</span><span class="ak-tag">已装备模组 {modules}</span>')

    uid = st.uid[-4:].rjust(len(st.uid), "*") if st.uid else ""
    stage = re.sub(r"^(st|main)_", "", st.mainStageProgress or "")
    stage_text = f" · 主线 {escape(stage)}" if stage else " · 主线已全部通关"
    doctor_avatar = _avatar(st.avatar.id) if st.avatar and st.avatar.id else ""
    doctor_avatar = doctor_avatar or (st.avatar.url if st.avatar else "")
    sec = st.secretary
    sec_entry = catalog.get(sec.charId) if sec else None
    secretary = (
        f'<div class="sec"><div><small>ASSISTANT</small><b>{escape(sec_entry.name if sec_entry else "")}</b></div>'
        f'{_img(_avatar(sec.charId, sec.skinId))}</div>' if sec and sec.charId else ""
    )
    assists = "".join(_assist(c, card, catalog) for c in card.assistChars) or '<div class="ak-empty">未设置助战干员</div>'

    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ark_theme.css()}{CSS}</style></head><body class="ak">'
        f"""<div class="ak-head pl"><span class="ak-corner tl"></span><span class="ak-corner br"></span>
  <div class="code">OPERATORS<b>{len(chars)} <small>位干员</small></b></div>
  <div class="k">Rhodes Island · Doctor Record</div>
  <div class="who">
    <div class="avatar">{_img(doctor_avatar)}</div>
    <div><h1>{escape(st.name)}</h1>
      <p>UID {uid} · 等级 {st.level} · 入职 {st.register_time}{stage_text}</p></div>
  </div>
  {secretary}
</div>
<div class="tiles">{tiles_html}</div>
<div class="ak-sec"><h3>助战干员<small>Support Unit</small></h3><div class="assist">{assists}</div></div>
<div class="ak-sec"><h3>基建<small>Rhodes Island Base</small></h3><div class="base">{_base(card, now)}</div></div>
<div class="ak-sec"><h3>干员<small class="cjk">按稀有度、精英化与等级排序</small></h3><div class="stats">{stats}</div>
<div class="wall{" dense" if len(chars) >= DENSE_FROM else ""}">{"".join(_op_tile(c, card, catalog.get(c.charId)) for c in chars)}</div></div>"""
        + ark_theme.foot(f'数据来源 森空岛<br>生成时间 {time.strftime("%Y-%m-%d %H:%M", time.localtime(now))}')
        + "</body></html>"
    )


def catalog_by_id() -> dict:
    catalog = gacha_table_data.operator_catalog
    if not catalog.entries:
        catalog = gacha_table_data.load_operator_catalog()
    return catalog.by_id


async def _card_arguments(event: MessageEvent, state: T_State) -> bool:
    """Like strict_command, but "/mrfz开盒-r 2" written without a space counts too."""
    message = event.get_message()
    if not message or message[0].type != "text":
        return True
    raw = state.get(PREFIX_KEY, {}).get(RAW_CMD_KEY) or ""
    text = str(message[0]).lstrip()
    if not raw or not text.startswith(raw):
        return True
    rest = text[len(raw) :]
    return not rest or rest[0].isspace() or rest.lower().startswith(("-r", "--role"))


roster = on_command(
    "mrfz开盒",
    aliases={"方舟开盒", "明日方舟开盒", "mrfz卡片", "方舟卡片", "明日方舟卡片", "mrfz查询", "方舟查询", "明日方舟查询"},
    rule=Rule(_card_arguments),
    priority=5,  # upstream's shortcuts for these names are retired in skland_shortcuts
    block=True,
)


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


@refresh_credentials
async def _fetch_card(user: SkUser, char: Character) -> ArkCard:
    return await SklandAPI.ark_card(CRED(cred=user.cred, token=user.cred_token), char.uid)


@roster.handle()
async def _(event: MessageEvent, user_session: UserSession, session: async_scoped_session, arg: Message = CommandArg()) -> None:
    at = next((seg.data.get("qq") for seg in event.message if seg.type == "at" and seg.data.get("qq") != "all"), None)
    target_id = (await get_user(user_session.platform, str(at))).id if at else user_session.user_id
    role = ROLE.search(arg.extract_plain_text())
    selected = await check_user_character(target_id, user_session, session, app_code="arknights", role_index=int(role[1]) if role else None)
    if selected is None:  # not bound, or no default role: the reason has been sent
        return
    user, character = selected

    await _react(REACTION_PROCESSING)
    try:
        card = await _fetch_card(user, character)
        if not card:
            await _react(REACTION_FAIL)
            return
        await fill_avatars({c.charId for c in card.chars} | {c.charId for c in card.assistChars} | {card.status.secretary.charId})
        html = render(card, catalog_by_id())
        dense = len(card.chars) >= DENSE_FROM
        image = await ef_theme.render_page(html, WIDTH, scale=DENSE_SCALE if dense else SCALE, quality=DENSE_QUALITY if dense else QUALITY)
        logger.info(f"Arknights roster: {len(card.chars)} operators, {len(image) // 1024} KB")
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Arknights roster failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await roster.finish("角色卡生成失败，请稍后再试")
    await _react(REACTION_DONE)
    try:
        await session.commit()  # only saves a token refreshed on the way
    except Exception as e:  # the picture is out: a busy database is not a failed command
        logger.warning(f"Arknights roster: refreshed token not saved ({type(e).__name__})")
