"""明日方舟 卫戍协议：/卫 [@用户]（战绩）和 /卫戍榜（每天 10:30 刷新的奖杯榜）。赛季名取自森空岛的资源表。

数据用上游 nonebot-plugin-skland 的接口和 GarrisonView（雷达图、偏好、对局记录都由它算好）；
图由本插件按 /mrfz开盒 的明日方舟风格绘制，每个图标都写上名字和数字（render.py）。
资源表约 500 KB，在内存里缓存一天，战绩每次查询时现取。

/卫 的选项用中文：全部（全部记录）、收藏（收藏的对局）、详情 [第几局]（不带数字时列出各局供点选）。
能挂按钮的机器人上，查自己时下面跟着这些选项的按钮；显示不了按钮的机器人上写成一行文字，全部记录放进一张聊天记录卡片。

/卫戍榜 不分群（与 /zmd欧非榜 相同）：每天 10:30（官方每天 10:00 刷新之后，避开夜间签到）
用每个成员自己的森空岛登录读取他每个明日方舟角色的战绩，奖杯最多的角色上榜（/卫 仍查默认角色），
WORKERS 个成员同时读，只保存榜单要用的几个数字；某个成员这次一个角色也没读到时沿用他上一次的数字。成员可以发 /卫戍榜 更新 只更新自己（5 分钟一次）；超级用户可以发 /卫戍榜 刷新 立即统计一次（在服务器上则是放一个 recount 文件再重启）。
本群成员显示群名片并标“本群”，其他人显示游戏内的名字；/卫戍榜 退出 / 加入 控制自己是否上榜。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode

import httpx

from nonebot import get_driver, logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import MatcherException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.typing import T_State

from plugins import ef_theme
from plugins.quick_buttons import Rows, can_show_buttons, is_official_bot, rows_fit, rows_of, send_buttons
from plugins.strict_command import command_ends_cleanly, strict

require("nonebot_plugin_skland")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_user")
require("nonebot_plugin_orm")
require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("plugins.arknights_roster")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import CustomNode, UniMessage
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import async_scoped_session, get_session
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.commands.selection import check_user_character
from nonebot_plugin_skland.exception import RequestException, SklandException
from nonebot_plugin_skland.model import Character, CharacterDefault, SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.schemas.arknights.garrison import GarrisonData, GarrisonResources, GarrisonView
from nonebot_plugin_skland.services.auth import refresh_credentials
from nonebot_plugin_user import UserSession, get_user
from sqlalchemy import select

from plugins.arknights_roster import fill_avatars, _avatar
from plugins.shared import REACTION_DONE, REACTION_FAIL, REACTION_PROCESSING, SLOW_UPLOAD, react as _react, upload_timed_out, write_json
from plugins.skland_roles import member_numbers

from . import render
from .stats import Standing, ranked, standing

__plugin_meta__ = PluginMetadata(
    name="Arknights stronghold protocol",
    description="卫戍协议：战绩图（全部记录、收藏、单局详情），以及每天刷新的奖杯榜。",
    usage="/卫 [@用户] [全部|收藏|详情 [第几局]] | /卫戍榜 | /卫戍榜 退出 | /卫戍榜 加入",
    type="application",
)

RESOURCE_MAX_AGE = 86400  # 资源表缓存多久；换赛季后一天内用上新赛季
BOARD_FILE = store.get_plugin_data_file("board.json")
OPTOUT_FILE = store.get_plugin_data_file("optout.json")
RECOUNT_FILE = store.get_plugin_data_file("recount")  # 有这个文件时，启动后统计一次（统计开始前删掉）；内容为 multi 时只读多角色成员
BOARD_VERSION = 1
WORKERS = 5  # 每天统计时同时读取的成员数，与竞速榜采集相同
COLLECT_PAUSE = 1.0  # 同一个 worker 两次读取之间的秒数
DETAIL = re.compile(r"^(?:详情|第)\s*(\d+)?\s*局?$")
PAGE = re.compile(r"^第\s*(\d+)\s*页$")
USAGE = "用法：/卫 [@用户]，后面可加 全部（翻页：第2页）、收藏、第几局（如 /卫 3），看别的角色加 角色序号（如 /卫 角色2）"
FORWARD_PAGES = 16  # 一张聊天记录卡片最多放几页（与上游的卫戍战绩相同）
SEVERAL_PICTURES = "卫戍协议对局记录过多，将以多张图片形式发送"  # plugins.forward_reply_owner 认得这句：吞掉它，卡片标上群名片，卡片后 @ 本人

driver = get_driver()


# ---------------------------------------------------------------- 数据（上游的接口和模型）


@refresh_credentials
async def _fetch_resources(user: SkUser) -> GarrisonResources:
    return await SklandAPI.ark_garrison_resources(CRED(cred=user.cred, token=user.cred_token))


class NotPlayed(Exception):
    """这个角色还没打过卫戍协议：森空岛照常回答，只是对局历史为空（生涯里全是 0）。"""


def season_title(resources: GarrisonResources | None = None) -> str:
    """“卫戍协议·赛季名”，赛季名取自森空岛的资源表（换赛季后跟着变）；还没取到资源表时只说“卫戍协议”。"""
    resources = resources or (_resources[1] if _resources else None)
    name = resources.resources[0].versionInfo.seasonName.strip() if resources and resources.resources else ""
    return f"卫戍协议·{name}" if name else "卫戍协议"


@refresh_credentials
async def _fetch_data(user: SkUser, uid: str, activity: str) -> GarrisonData:
    cred = CRED(cred=user.cred, token=user.cred_token)
    try:
        return await SklandAPI.ark_garrison(cred, uid=uid, activity_id=activity)
    except RequestException as e:
        if "格式异常" in str(e) and await _history_missing(cred, uid, activity):
            raise NotPlayed from None
        raise


async def _history_missing(cred: CRED, uid: str, activity: str) -> bool:
    """上游对“对局历史为空”只说“接口响应格式异常”：再取一次原样的回答，看是不是这种情况。"""
    url = "https://zonai.skland.com/api/ms/game/arknights/garrison/info?" + urlencode({"uid": uid, "activityId": activity})
    try:
        async with httpx.AsyncClient() as client:
            headers = await SklandAPI.get_sign_header(cred, url, method="get")
            payload = (await client.get(url, headers=headers)).json()
    except Exception:
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return isinstance(data, dict) and not payload.get("code") and data.get("history") is None


_resources: tuple[float, GarrisonResources] | None = None
_resource_lock = asyncio.Lock()  # 统计时几个 worker 可能同时发现要更新，只让一个去取


async def resources_for(user: SkUser) -> GarrisonResources:
    """当前赛季的资源表：缓存一天；过期时重新取，取不到就先用旧的。"""
    global _resources
    async with _resource_lock:
        if _resources and time.time() - _resources[0] < RESOURCE_MAX_AGE:
            return _resources[1]
        try:
            fresh = await _fetch_resources(user)
        except Exception:
            if _resources:
                logger.warning("Arknights stronghold: resource table not refreshed; the kept one is used")
                return _resources[1]
            raise
        _resources = (time.time(), fresh)
        return fresh


async def view_of(user: SkUser, character: Character, **choice) -> GarrisonView:
    """取一个角色的战绩，交给上游的 GarrisonView 算好（choice：record_index / favored）。"""
    res = await resources_for(user)
    data = await _fetch_data(user, character.uid, res.current_activity_id)
    return GarrisonView.from_data(
        data, res, uid=str(character.uid), server_name=character.server_name, channel_master_id=character.channel_master_id,
        activity_id=res.current_activity_id, **choice,
    )


def parse_choice(text: str) -> dict | str | None:
    """/卫 后面的中文选项 -> {"show_all"/"page"/"favored"/"record_index"/"role"}（单独一个数字也是第几局；"角色2" 已由
    command_tolerance 换成 "-r 2"）；"详情" 不带数字时为 "menu"；认不出时为 None。"""
    words = text.split()
    choice: dict = {}
    menu = False
    i = 0
    while i < len(words):
        word = words[i]
        following = words[i + 1] if i + 1 < len(words) and words[i + 1].isdigit() else None
        if word in ("-r", "--role") and following:
            choice["role"] = int(following)
            i += 1
        elif word == "全部":
            choice["show_all"] = True
        elif page := PAGE.match(word):  # /卫 第2页：全部记录的第 2 页
            choice.update(show_all=True, page=int(page.group(1)))
        elif word == "收藏":
            choice["favored"] = True
        elif match := DETAIL.match(word):
            number = match.group(1) or following
            if number is None:
                menu = True
            else:
                choice["record_index"] = int(number)
                i += 1 if not match.group(1) and following else 0
        elif word.isdigit():  # /卫 3：第 3 局
            choice.setdefault("record_index", int(word))
        else:
            return None
        i += 1
    if menu and "record_index" not in choice:
        return {**choice, "menu": True}
    return choice


# ---------------------------------------------------------------- /卫

GLUED = ("全部", "收藏", "详情", "第", "角色", "-r")  # 可以紧挨着指令名写的选项


async def _options_may_touch(event: MessageEvent, state: T_State) -> bool:
    """像 strict_command，但 "/卫3"、"/卫全部" 这样紧挨着指令名的选项也算；"/卫生" 之类不算。"""
    return command_ends_cleanly(event, state, GLUED, digits=True)


garrison = on_command(
    "卫",
    aliases={"卫戍战绩", "mrfz卫戍", "卫戍协议", "方舟卫戍", "明日方舟卫戍", "mrfz卫戍协议"},
    rule=Rule(_options_may_touch),
    priority=5,
    block=True,
)


def _buttons(view: GarrisonView, *, favored: bool, role: str = "") -> list[tuple[str, str]]:
    """查自己时图下面的按钮：全部 / 收藏 / 单局详情（role：" 角色2" 这样的后缀）。"""
    base = f"/卫{role}"
    prefix = f"{base} 收藏" if favored else base
    return [("全部记录", f"{prefix} 全部"), ("最近的对局", base) if favored else ("收藏的对局", f"{base} 收藏"), ("单局详情", f"{prefix} 详情")]


def _page_buttons(number: int, total: int, *, favored: bool, role: str = "") -> list[list[tuple[str, str]]]:
    """全部记录翻页：五页以内直接列出其他各页，更多时给上一页、下一页。"""
    prefix = f"/卫{role}" + (" 收藏" if favored else "")
    if total <= 1:
        return []
    if total <= 5:
        return [[(f"第{i}页", f"{prefix} 第{i}页") for i in range(1, total + 1) if i != number]]
    nearby = [("上一页", f"{prefix} 第{number - 1}页")] if number > 1 else []
    return [nearby + ([("下一页", f"{prefix} 第{number + 1}页")] if number < total else [])]


def _detail_menu(view: GarrisonView, page: int, *, favored: bool, role: str = "") -> tuple[str, Rows, str]:
    """「单局详情」的下一级：和全部记录同样分页。按钮是每局的编号和策略，最后一行翻页；
    叠了层的盟约放不进按钮，写在上方的文字里，一局一行（行首不能是 #，QQ 会当成标题）。
    返回 (文字, 按钮, 显示不了按钮时的文字菜单：同样的各局，不用 markdown，末尾写明怎么选、怎么翻页)；
    没有对局时按钮为空，文字是一句说明。"""
    size = render.PAGE_RECORDS
    total = max(1, -(-len(view.records) // size))
    page = min(max(page, 1), total)
    prefix = f"/卫{role}" + (" 收藏" if favored else "")
    games = view.records[(page - 1) * size : page * size]
    if not games:
        nothing = "没有收藏的对局" if favored else "没有对局记录"
        return nothing, [], nothing
    head = "选一局查看详情" + (f"（第 {page}/{total} 页）" if total > 1 else "")
    lines = []
    for r in games:
        bonds = sorted((b for b in r.bonds if b.count), key=lambda b: -b.count)
        lines.append((f"{r.index:02d}", r.strategy.name, " · ".join(f"{b.name} {b.count}" for b in bonds) or "没有叠层的盟约"))
    buttons = [(f"#{r.index:02d} {r.strategy.name[:6]}", f"{prefix} {r.index}") for r in games]
    turns = [("上一页", f"{prefix} 详情 第{page - 1}页")] if page > 1 else []
    turns += [("下一页", f"{prefix} 详情 第{page + 1}页")] if page < total else []
    how = f"发送 {prefix} 加编号查看，例如 {prefix} {games[0].index}" + ("；" + "，".join(f"{label} {command}" for label, command in turns) if turns else "")
    text = "\n".join([head] + [f"{number} **{name}**：{bonds}" for number, name, bonds in lines])
    words = "\n".join([head] + [f"{number} {name}：{bonds}" for number, name, bonds in lines] + [how])
    return text, rows_of(buttons, 2) + ([turns] if turns else []), words


def _next_steps(view: GarrisonView, *, favored: bool, role: str, show_all: bool, page: int | None = None, total: int = 1) -> tuple[str, Rows]:
    """查自己时图后的下一步：(标题, 按钮)。概览后是全部 / 收藏 / 单局详情；全部记录的一页（page）后是其他各页、
    这一页的单局详情、返回概览；全部记录一次发完（显示不了按钮的机器人）后是单局详情、返回概览。"""
    if not show_all:
        return "还可以查看", rows_fit(_buttons(view, favored=favored, role=role))
    prefix = f"/卫{role}" + (" 收藏" if favored else "")
    back = rows_fit([("单局详情", f"{prefix} 详情" + (f" 第{page}页" if page else "")), ("返回概览", prefix)])
    if page is None:
        return "还可以查看", back
    return f"第 {page}/{total} 页", _page_buttons(page, total, favored=favored, role=role) + back


async def _send_pictures(bot: Bot, view: GarrisonView, images: list[bytes], about_other: bool) -> None:
    """查自己时第一张图 @ 本人，查别人时引用指令。显示不了按钮的机器人上，多页的全部记录放进聊天记录卡片
    （每张最多 FORWARD_PAGES 页），不在群里连刷大图；查别人时不发预告：forward_reply_owner 会把卡片标成提问人的记录，
    页名就保留游戏内的名字。"""
    if len(images) > 1 and not is_official_bot(bot):
        if not about_other:
            await UniMessage.text(SEVERAL_PICTURES).send(reply_to=True)
        name = view.nickname or render.NAMELESS
        for start in range(0, len(images), FORWARD_PAGES):
            await UniMessage.reference(*(
                CustomNode(bot.self_id, f"{name} | 第 {n}/{len(images)} 页", UniMessage.image(raw=image))
                for n, image in enumerate(images[start : start + FORWARD_PAGES], start + 1)
            )).send()
        return
    for index, image in enumerate(images):
        await UniMessage.image(raw=image).send(at_sender=index == 0 and not about_other, reply_to=index == 0 and about_other)


@garrison.handle()
async def answer(bot: Bot, event: MessageEvent, user_session: UserSession, session: async_scoped_session, arg: Message = CommandArg()) -> None:
    """回复一次 /卫。"""
    at = next((seg.data.get("qq") for seg in event.message if seg.type == "at" and seg.data.get("qq") != "all"), None)
    about_other = bool(at) and str(at) != event.get_user_id()
    choice = parse_choice(arg.extract_plain_text())
    if choice is None:
        await garrison.finish(USAGE)
    target_id = (await get_user(user_session.platform, str(at))).id if at else user_session.user_id
    role_index = choice.pop("role", None)
    selected = await check_user_character(target_id, user_session, session, app_code="arknights", role_index=role_index)
    if selected is None:  # 未绑定或没有默认角色：原因已经发出
        return
    user, character = selected
    menu = bool(choice.pop("menu", False))
    options = dict(choice)
    role = f" 角色{role_index}" if role_index is not None else ""
    show_all = options.pop("show_all", False)
    page_number = options.pop("page", None)
    favored = options.get("favored", False)

    await _react(REACTION_PROCESSING)
    try:
        view = await view_of(user, character, **options)
        await session.commit()  # 途中刷新的 token
        if not view.records and not sum(m.total for m in view.modes):
            await _react(REACTION_DONE)
            await garrison.finish(("对方" if about_other else "") + f"还没有{season_title()}的记录", at_sender=not about_other)
        if menu:
            text, rows, words = _detail_menu(view, page_number or 1, favored=favored, role=role)
            if not rows:  # 没有可选的对局：只是一句回答，不是菜单（NapCat 群里不撤回）
                await _react(REACTION_DONE)
                await garrison.finish(text)
            await send_buttons(text, rows, fallback=words)
            await _react(REACTION_DONE)
            return
        await fill_avatars({o.char_id for r in view.records for o in r.operators})
        avatars = {o.char_id: _avatar(o.char_id) for r in view.records for o in r.operators}
        shown, total = None, 1
        if view.is_detail:
            pages = [render.detail(view, avatars)]
        elif show_all:
            size = render.PAGE_RECORDS
            chunks = [view.records[i : i + size] for i in range(0, len(view.records), size)] or [[]]
            total = len(chunks)
            # 能挂按钮时一次一页（查自己时下面有翻页按钮）；指定了第几页也只发那一页；否则全部发出（见 _send_pictures）
            paged = page_number is not None or (can_show_buttons() and not about_other)
            shown = min(max(page_number or 1, 1), total) if paged else None
            numbered = [(shown, chunks[shown - 1])] if shown else list(enumerate(chunks, 1))
            pages = [render.records_page(view, chunk, avatars, page=n, page_total=total) for n, chunk in numbered]
        else:
            pages = [render.summary(view, avatars, page_total=1)]
        images = [await ef_theme.render_page(page, render.WIDTH, scale=render.SCALE, quality=render.QUALITY) for page in pages]
        logger.info(f"Arknights stronghold: {len(images)} pictures, {sum(len(i) for i in images) // 1024} KB")
        await _send_pictures(bot, view, images, about_other)
        if not about_other and not view.is_detail:  # 看别人的、单局详情不给下一步（和官方机器人不挂按钮一致）
            title, rows = _next_steps(view, favored=favored, role=role, show_all=show_all, page=shown, total=total)
            await send_buttons(title, rows, fallback=True)
    except MatcherException:
        raise
    except NotPlayed:
        await _react(REACTION_DONE)
        await garrison.finish(f"这个角色还没有{season_title()}的对局记录")
    except SklandException as e:
        logger.warning(f"Arknights stronghold: query failed ({type(e).__name__})")
        await _react(REACTION_FAIL)
        await garrison.finish("卫戍协议战绩获取失败，请稍后再试；如果一直失败，可能需要重新绑定森空岛")
    except ValueError as e:  # GarrisonView：没有这一局等
        await _react(REACTION_FAIL)
        await garrison.finish(str(e))
    except Exception as e:
        if upload_timed_out(e):  # 图已经做好，只是上传太久
            logger.warning("Arknights stronghold: picture upload timed out")
            await garrison.finish(SLOW_UPLOAD)
        logger.opt(exception=e).warning(f"Arknights stronghold card failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await garrison.finish("卫戍协议战绩生成失败，请稍后再试")
    await _react(REACTION_DONE)


# ---------------------------------------------------------------- /卫戍榜

garrison_board = on_command("卫戍榜", aliases={"mrfz卫戍榜", "卫戍协议榜", "方舟卫戍榜"}, rule=strict, priority=5, block=True)
_collecting = asyncio.Lock()  # 全员统计和个人更新都会整个重写榜单：一次只能有一个
_counting = False  # 正在做全员统计（而不只是某个人在更新自己）
last_reasons: Counter[str] = Counter()  # 最近一次统计里，一个角色也没读到的成员各因为什么（森空岛的说法）-> 人数


def board_season(path: Path = BOARD_FILE) -> str:
    """保存的榜单是哪个赛季统计的（“卫戍协议·赛季名”）；没记下时为“卫戍协议”。"""
    try:
        return str(json.loads(path.read_text("utf-8")).get("season") or "卫戍协议")
    except Exception:
        return "卫戍协议"


def load_board(path: Path = BOARD_FILE) -> tuple[float, list[Standing]]:
    """保存的榜单：统计时间和每个成员的数字；没有或读不了时为空。"""
    try:
        saved = json.loads(path.read_text("utf-8"))
        if saved.get("version") != BOARD_VERSION:
            return 0.0, []
        return float(saved["counted_at"]), [Standing(**row) for row in saved["standings"]]
    except FileNotFoundError:
        return 0.0, []
    except Exception as e:
        logger.warning(f"Arknights stronghold: the kept board could not be read ({type(e).__name__})")
        return 0.0, []


def save_board(counted_at: float, standings: list[Standing], path: Path = BOARD_FILE, season: str = "") -> None:
    write_json(path, {"version": BOARD_VERSION, "counted_at": counted_at, "season": season or "卫戍协议", "standings": [asdict(s) for s in standings]})


def _load_optout() -> set[str]:
    # 文件不存在说明没人选择退出；文件读不了时，绝不能悄悄把人重新列上榜。
    if not OPTOUT_FILE.exists():
        return set()
    return set(json.loads(OPTOUT_FILE.read_text("utf-8")))


async def _members() -> list[tuple[int, list[int]]]:
    """（成员，他全部明日方舟角色的 id，默认的在前）：只算设了默认明日方舟角色的成员。"""
    async with get_session() as session:
        defaults = (await session.execute(
            select(CharacterDefault.owner_id, CharacterDefault.character_id).where(CharacterDefault.app_code == "arknights")
        )).all()
        roles = (await session.execute(
            select(SkUser.owner_id, Character.id).join(Character, Character.account_id == SkUser.id).where(Character.app_code == "arknights")
        )).all()
    others: dict[int, list[int]] = {}
    for owner, character_id in roles:
        others.setdefault(owner, []).append(character_id)
    return [(owner, [default, *(c for c in others.get(owner, []) if c != default)]) for owner, default in defaults]


async def _read_member(owner: int, character_id: int) -> Standing | None:
    """读一个成员的战绩；没有角色或没有对局时为 None，读不到时抛出异常。"""
    async with get_session() as session:
        character = await session.get(Character, character_id)
        user = await session.get(SkUser, character.account_id) if character else None
        if user is None:
            return None
        view = await view_of(user, character)
        try:
            await session.commit()  # 途中刷新的 token；几个 worker 同时写时可能被锁，下次用到会再刷新
        except Exception as e:
            logger.info(f"Arknights stronghold board: refreshed token not saved ({type(e).__name__})")
    row = standing(owner, view)
    return row if row.total > 0 else None


async def best_of(owner: int, characters: list[int], reasons: Counter[str] | None = None) -> tuple[Standing | None, str]:
    """读一个成员的每个明日方舟角色：（读到的角色里奖杯最多的那一行，没有对局时为 None；有角色没读到时的原因，
    都读到了为空）。每个读不到的角色的原因另记进 `reasons`。"""
    rows, missed = [], ""
    for character_id in characters:
        try:
            row = await _read_member(owner, character_id)
            if row is not None:
                rows.append(row)
        except NotPlayed:
            pass  # 没打过：和读到了 0 局一样，不上榜，也不算读不到
        except Exception as e:
            said = e.args[0] if e.args and isinstance(e.args[0], str) else type(e).__name__
            missed = said.rpartition("：")[2] or said
            if reasons is not None:
                reasons[f"{type(e).__name__}: {said}"[:120]] += 1
        await asyncio.sleep(COLLECT_PAUSE)
    return (ranked(rows)[0] if rows else None), missed


def keep_or_replace(best: Standing | None, missed: str, before: Standing | None) -> Standing | None:
    """这次读到的和上一次的，榜上该放哪一行：有角色没读到，而上一次的奖杯比这次读到的多（多半是奖杯多的那个
    角色这次没读到），沿用上一次的；否则用这次的。"""
    if missed and before is not None and (best is None or before.trophies > best.trophies):
        return before
    return best


async def collect(multi_only: bool = False) -> tuple[int, int]:
    """为每个有默认明日方舟角色的成员读他每个明日方舟角色的战绩（WORKERS 个成员同时读），奖杯最多的角色上榜，保存榜单。
    multi_only：只读有几个明日方舟角色的成员，其他人保留上一次的数字。
    返回（上榜人数，一个角色也没读到的人数）。
    开始时记一行日志，结束时记上榜人数或失败：deploy/restart_when_idle.py 看到开始了还没结束就不重启。"""
    global _counting
    async with _collecting:
        _counting = True
        logger.info("Arknights stronghold board: count started" + (" (members with several roles)" if multi_only else ""))
        try:
            return await _collect(multi_only)
        except Exception as e:
            logger.opt(exception=e).warning(f"Arknights stronghold board: count failed: {type(e).__name__}")
            raise
        finally:
            _counting = False


async def _collect(multi_only: bool) -> tuple[int, int]:
    _, before = load_board()
    kept = {s.owner: s for s in before}
    members = await _members()
    standings: list[Standing] = []
    if multi_only:
        members = [m for m in members if len(m[1]) > 1]
        asked = {owner for owner, _ in members}
        standings = [s for s in before if s.owner not in asked]
    queue: asyncio.Queue[tuple[int, list[int]]] = asyncio.Queue()
    for member in members:
        queue.put_nowait(member)
    reasons: Counter[str] = Counter()  # 角色读不到的原因（森空岛的说法）-> 角色数
    members_missed: Counter[str] = Counter()
    failed = 0

    async def worker() -> None:
        nonlocal failed
        while not queue.empty():
            owner, characters = queue.get_nowait()
            best, missed = await best_of(owner, characters, reasons)
            if best is None and missed:
                failed += 1
                members_missed[missed[:40]] += 1
            row = keep_or_replace(best, missed, kept.get(owner))
            if row is not None:
                standings.append(row)

    await asyncio.gather(*(worker() for _ in range(min(WORKERS, len(members)) or 1)))
    last_reasons.clear()
    last_reasons.update(members_missed)
    save_board(time.time(), standings, season=season_title() if _resources else board_season())
    logger.info(f"Arknights stronghold board: {len(standings)} on the board of {len(members)} members, {failed} members not read at all")
    for reason, count in reasons.most_common(10):
        logger.info(f"Arknights stronghold board: roles not read x{count}: {reason}")
    return len(standings), failed


@scheduler.scheduled_job("cron", hour=10, minute=30, timezone="Asia/Shanghai", id="arknights_garrison_board", misfire_grace_time=3600)
async def _daily() -> None:
    with contextlib.suppress(Exception):  # 原因 collect 已记下
        await collect()


COLD_START_DELAY = 60  # 启动后多少秒补做第一次统计（还没有任何榜单时）
_tasks: set[asyncio.Task] = set()


async def _count_after_start(multi_only: bool = False) -> None:
    await asyncio.sleep(COLD_START_DELAY)
    with contextlib.suppress(Exception):  # 原因 collect 已记下
        await collect(multi_only)


@driver.on_startup
async def _count_if_nothing_kept() -> None:
    """还没有任何榜单（第一次启动，或统计途中被重启打断），或者放了 RECOUNT_FILE（在服务器上要求重新统计；
    文件内容为 multi 时只读有几个明日方舟角色的成员）：启动后不等 10:30，先统计一次。"""
    try:
        asked = RECOUNT_FILE.read_text("utf-8-sig").strip() if RECOUNT_FILE.exists() else None  # utf-8-sig：带 BOM 也认得
    except (OSError, UnicodeError) as e:  # 读不了也照样启动：当作要求全员统计
        logger.warning(f"Arknights stronghold board: the recount file could not be read ({type(e).__name__})")
        asked = ""
    with contextlib.suppress(OSError):
        RECOUNT_FILE.unlink(missing_ok=True)
    if asked is not None or not load_board()[0]:
        # 重启脚本从这一行起就等着（包括统计开始前的这一分钟）：否则刚删掉的 recount 文件就白放了
        logger.info(f"Arknights stronghold board: count scheduled in {COLD_START_DELAY} s")
        task = asyncio.create_task(_count_after_start(asked == "multi"))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


REFRESH_COOLDOWN = 300  # 同一个人两次 /卫戍榜 更新 之间的秒数（与 /竞速更新 相同）
REFRESH_WAIT = 60  # 别人正在更新自己时最多等多久（一次个人更新只要几秒）
RETRY_AFTER_FAILURE = 60  # 个人更新什么也没读到时，多少秒后可以再试
_retry_at: dict[str, float] = {}  # QQ -> 上次失败后，monotonic 到这个时间才可以再试
_refreshed: dict[str, float] = {}  # QQ -> 上次成功（或仍在进行）的 /卫戍榜 更新 的 monotonic 时间


async def refresh_one(qq: str) -> str:
    """/卫戍榜 更新：只重新读这个人的战绩，换掉榜上他那一行（统计时间不变）。返回要回复的话。"""
    wait = _retry_at.get(qq, 0) - time.monotonic()
    if wait > 0:
        return f"上次没有读到你的战绩，请 {int(wait) + 1} 秒后再试"
    since = time.monotonic() - _refreshed.get(qq, -REFRESH_COOLDOWN)
    if since < REFRESH_COOLDOWN:
        ago = "刚刚" if since < 10 else f"{int(since)} 秒前" if since < 60 else f"{int(since // 60)} 分钟前"
        return f"你{ago}已更新过，{REFRESH_COOLDOWN // 60} 分钟内不用重复更新"
    _refreshed[qq] = time.monotonic()  # 先占上：连点两下，第二下就会看到上面那句
    try:
        return await _refresh_one(qq)
    except BaseException:
        _refreshed.pop(qq, None)
        raise


async def _refresh_one(qq: str) -> str:
    busy = "卫戍榜正在统计中，结束后榜单就会包含你的最新成绩，不用再发"
    if _counting:
        _refreshed.pop(qq, None)
        return busy
    ids = await _bound_ids()
    owner = next((o for o, numbers in ids.items() if qq in numbers), None)
    entry = next((m for m in await _members() if m[0] == owner), None) if owner is not None else None
    if entry is None:
        _refreshed.pop(qq, None)
        return "还没有绑定森空岛的明日方舟角色，请先发送 /skl绑定"
    try:  # 别人正在更新自己：等他几秒；等了这么久还没轮到，就是全员统计刚好开始了
        await asyncio.wait_for(_collecting.acquire(), REFRESH_WAIT)
    except asyncio.TimeoutError:
        _refreshed.pop(qq, None)
        return busy
    await _react(REACTION_PROCESSING)
    try:
        try:
            best, missed = await best_of(*entry)
        except Exception as e:
            best, missed = None, type(e).__name__
        if best is None and missed:  # 什么也没读到：一分钟后可以再试（不是马上，免得一直失败的人不停地请求）
            _refreshed.pop(qq, None)
            _retry_at[qq] = time.monotonic() + RETRY_AFTER_FAILURE
            await _react(REACTION_FAIL)
            return "森空岛登录已失效，请发送 /skl绑定 重新扫码" if "登录" in missed else "森空岛暂时没有返回你的战绩，请稍后再试"
        counted_at, standings = load_board()
        before = next((s for s in standings if s.owner == owner), None)
        row = keep_or_replace(best, missed, before)
        standings = [s for s in standings if s.owner != owner] + ([row] if row else [])
        save_board(counted_at or time.time(), standings, season=season_title() if _resources else board_season())
    finally:
        _collecting.release()
    _refreshed[qq] = time.monotonic()  # 冷却从更新完成时算起
    await _react(REACTION_DONE)
    if row is None:
        return f"你还没有{season_title()}的对局记录"
    shown = visible(standings, ids)
    rank = next((i for i, s in enumerate(shown, 1) if s.owner == owner), None)
    change = f"（之前 {before.trophies}）" if before is not None and before.trophies != row.trophies else ""
    kept_note = "（有角色这次没读到，沿用上一次的成绩）" if row is before else ""
    place = f"，现在第 {rank} 名" if rank else "（你已退出卫戍榜，榜上不显示）"
    return f"卫戍榜成绩已更新：{row.name} {row.trophies} 奖杯{change}{place}{kept_note}"


async def _bound_ids() -> dict[int, set[str]]:
    """成员 -> 他被认出的号码。"""
    async with get_session() as session:
        return await member_numbers(session)


def visible(standings: list[Standing], ids: dict[int, set[str]]) -> list[Standing]:
    """榜上显示的人（排好序）：还绑定着、没有退出的。/卫戍榜 的图和 /卫戍榜 更新 的名次都按这个算。"""
    optout = _load_optout()
    return ranked([s for s in standings if s.owner in ids and not (ids[s.owner] & optout)])


async def _names(bot: Bot, event: MessageEvent, rows: list[Standing]) -> tuple[dict[int, tuple[str, bool]], dict[int, set[str]]]:
    """成员 -> (显示的名字, 是否本群)，以及成员 -> 他被认出的号码。"""
    ids = await _bound_ids()
    cards: dict[str, str] = {}
    if isinstance(event, GroupMessageEvent):
        try:
            members = await bot.get_group_member_list(group_id=event.group_id)
            cards = {str(m["user_id"]): str(m.get("card") or m.get("nickname") or "").strip() for m in members}
        except Exception as e:  # 榜单仍然值得显示：只是没人会被标为本群
            logger.info(f"Arknights stronghold board: no member list for this group ({type(e).__name__})")
    names = {}
    for row in rows:
        here = next((n for n in sorted(ids.get(row.owner, ())) if n in cards), None)
        names[row.owner] = ((cards[here] if here else "") or row.name, here is not None)
    return names, ids


@garrison_board.handle()
async def _(bot: Bot, event: MessageEvent, user_session: UserSession, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    qq = str(event.user_id)
    if action in ("退出", "加入"):
        optout = _load_optout()
        (optout.add if action == "退出" else optout.discard)(qq)
        write_json(OPTOUT_FILE, sorted(optout))
        await _react(REACTION_DONE)
        await garrison_board.finish("已退出卫戍榜，不会再出现在榜单上" if action == "退出" else "已重新加入卫戍榜")
    if action == "刷新":
        if qq not in {str(s) for s in driver.config.superusers}:
            await garrison_board.finish("卫戍榜每天 10:30 自动刷新")
        if _collecting.locked():
            await garrison_board.finish("卫戍榜正在统计中")
        await garrison_board.send("开始统计卫戍榜，约需十分钟")
        counted, failed = await collect()
        try:
            why = "；".join(f"{reason} {count} 人" for reason, count in last_reasons.most_common(5))
            await garrison_board.send(f"卫戍榜统计完成：{counted} 人上榜" + (f"，{failed} 人这次没读到（沿用上一次的数字）\n原因：{why}" if failed else ""))
        except ActionFailed as e:  # 统计超过了平台允许回复的时限，而该群不让机器人主动发言
            logger.info(f"Arknights stronghold board: the count is done, but the reply could not be sent ({e.retcode})")
        await garrison_board.finish()
    if action == "更新":
        await garrison_board.finish(await refresh_one(qq), at_sender=True)
    if action:
        await garrison_board.finish("用法：/卫戍榜 | /卫戍榜 更新 | /卫戍榜 退出 | /卫戍榜 加入")

    counted_at, standings = load_board()
    if not counted_at:
        await garrison_board.finish("卫戍榜还没有统计过，每天 10:30 刷新")
    await _react(REACTION_PROCESSING)
    try:
        names, ids = await _names(bot, event, standings)
        rows = visible(standings, ids)
        image = await ef_theme.render_page(render.board(rows, names, user_session.user_id, counted_at, season=board_season()), render.BOARD_WIDTH, scale=render.SCALE, quality=render.QUALITY)
        await UniMessage.image(raw=image).send(reply_to=True)
        if any(s.owner == user_session.user_id for s in rows):  # 只给榜上有他的人（没打过、退出了的不给）
            await send_buttons("**卫戍榜**", [[("更新我的成绩", "/卫戍榜 更新")]], fallback=True)
    except MatcherException:
        raise
    except Exception as e:
        if upload_timed_out(e):  # 图已经做好，只是上传太久
            logger.warning("Arknights stronghold board: picture upload timed out")
            await garrison_board.finish(SLOW_UPLOAD)
        logger.opt(exception=e).warning(f"Arknights stronghold board failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await garrison_board.finish("卫戍榜生成失败，请稍后再试")
    await _react(REACTION_DONE)
