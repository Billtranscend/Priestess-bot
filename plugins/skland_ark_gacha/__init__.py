"""明日方舟抽卡记录，做法和终末地一样：保存记录、手动更新、每日同步。

上游的 `skland gacha` 每次都先去问游戏，也没有同步。这里和终末地一样：

    /mrfz抽卡记录        直接显示已保存的记录（不请求游戏的抽卡记录 API）
    /mrfz抽卡记录更新    请求游戏、保存所选角色新增的记录，再像上面一样显示
    /mrfz抽卡记录 第2页   记录需要好几张图时，只看其中一张（选项 -p）

成员自己保存的记录按卡池开放的年份一年一张图，先发最新的一张；一年里卡池太多时再按月份平均切开
（year_pages）。有好几张时图下方写着第几页、怎么看其他页（page_note）。能在消息下面挂按钮的机器人
（官方 QQ 机器人，plugins/quick_buttons.py）上，其余每张图各有一个按钮（按钮上写年份或月份），另有
一个按钮拉取最新记录（刚拉取过时没有）。看别人的记录、用 -b / -l 截取时一次画出（超过上限分成几张）。

此外，成员绑定的每个账号下的每个明日方舟角色每天同步一次，和终末地同在 01:00 这个时段
（有自己的队列和 worker，用同一个 Queue 类）。官方 API 只保留有限时间内的抽卡记录，所以游戏
不再返回的记录，这里保存的仍然在。绑定之后（plugins.skland_bind_sign）立刻排一次同步，结束时通知
sync_listeners；有明日方舟角色但还从未同步过的成员，也会在 SEEN_SECONDS 内排进队列，已有成员在本插件
装上后都靠它同步了一次。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
from apscheduler.triggers.cron import CronTrigger
from nonebot import get_driver, logger, on_command, require
from nonebot.matcher import current_matcher
from nonebot.adapters import Bot, Message
from nonebot.exception import MatcherException
from playwright.async_api import TimeoutError as PageTimeout
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert

require("nonebot_plugin_skland")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")
require("nonebot_plugin_htmlrender")

from nonebot_plugin_alconna import Args, At, CustomNode, Match, Option, UniMessage, command_manager
from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_htmlrender import html_to_pic
from nonebot_plugin_orm import async_scoped_session, get_session
from nonebot_plugin_skland import render
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.commands import gacha as gacha_command
from nonebot_plugin_skland.commands.selection import check_user_character
from nonebot_plugin_skland.config import config
from nonebot_plugin_skland.db_handler import get_character_gacha_records
from nonebot_plugin_skland.exception import SklandException
from nonebot_plugin_skland.matcher import skland_command
from nonebot_plugin_skland.model import Character, GachaRecord, SkUser
from nonebot_plugin_skland.player_data import get_ark_card
from nonebot_plugin_skland.services.gacha import get_all_gacha_records, group_gacha_records
from nonebot_plugin_skland.utils.message import send_request_error
from nonebot_plugin_user import UserSession, get_user

from plugins.skland_auto_gacha.core import Queue, TZ
from plugins.quick_buttons import can_show_buttons, plain, rows_fit, send_buttons
from plugins.shared import SLOW_UPLOAD, heybox_hint, upload_timed_out
from plugins.strict_command import strict

__plugin_meta__ = PluginMetadata(
    name="Skland Arknights gacha",
    description="明日方舟抽卡记录：查看已保存的记录、手动更新，以及每天 01:00 自动同步。",
    usage="/mrfz抽卡记录　/mrfz抽卡记录更新",
    type="application",
)

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data/skland_auto_gacha/ark_queue.sqlite3"
TRIGGER = CronTrigger(hour=1, minute=0, second=0, timezone=TZ)
DAILY_ID = "skland_auto_arkgacha_daily"
WORKER_ID = "skland_auto_arkgacha_worker"
SEEN_ID = "skland_auto_arkgacha_seen"
SEEN_SECONDS = 60
WORKERS = 5  # 同时同步的成员数，与竞速榜采集相同
REQUEST_GAP = 2  # 同一个 worker 两个成员之间的秒数
NO_RECORDS = "还没有保存的抽卡记录，请发送 /mrfz抽卡记录更新 拉取数据"
NONE_FETCHED = "刚才没有拉取到明日方舟的抽卡记录：官方接口只提供最近几个月的记录。更早的记录可发送 /mrfz导入小黑盒 小黑盒ID 补全"
SYNC_FAILED = "抽卡记录拉取失败，请稍后再试。如果一直失败，可能是登录凭证过期了，重新发送 /skl绑定 扫码即可"
USAGE = "用法：/mrfz抽卡记录更新，查看另一个角色时加 角色序号（如 角色2）"
SEVERAL_PICTURES = "抽卡记录过多，将以多张图片形式发送"
FETCHED = "已拉取最新记录，新增 {} 条"
NO_TOKEN = "这个角色所属的森空岛账号没有保存登录凭证，无法拉取抽卡记录。请重新发送 /skl绑定 扫码"

_queue: Queue | None = None
_busy = asyncio.Lock()
_active: asyncio.Task | None = None
_stopping = False


# ── 指令 ───────────────────────────────────────────────────────

PAGE_POOLS = 8  # 一张图最多画几个卡池（导入小黑盒的早期记录后，一张图画全部卡池太长）


def _opened(pool) -> datetime:
    """卡池开放的时间；游戏表里还没有的卡池，用它最新一抽的时间。"""
    return datetime.fromtimestamp(pool.openTime or max((group.gacha_ts for group in pool.records), default=0), TZ)


def year_pages(pools: list) -> list[tuple[str, int, int]]:
    """按开放年份分页（最新的在前）：(按钮上的字, 开始, 结束)。一年多于 PAGE_POOLS 个卡池时，
    按月份平均切成几张（9 个是 5 + 4，不是 8 + 1）。"""
    pages, start = [], 0
    while start < len(pools):
        year = _opened(pools[start]).year
        end = next((i for i in range(start, len(pools)) if _opened(pools[i]).year != year), len(pools))
        count = -(-(end - start) // PAGE_POOLS)  # 这一年切成几张
        cuts = [start - (-(end - start) * k // count) for k in range(count + 1)]
        for begin, stop in zip(cuts, cuts[1:]):
            if count == 1:
                label = f"{year}年"
            else:
                newest, oldest = _opened(pools[begin]).month, _opened(pools[stop - 1]).month
                short = year % 100  # 按钮一行放两个，“2025年10-12月”就放不下
                label = f"{short}年{oldest}月" if newest == oldest else f"{short}年{oldest}-{newest}月"
            pages.append((label, begin, stop))
        start = end
    return pages


def page_menu(labels: list[str], number: int, role: str, fetched: int | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    """图下面的按钮：其余每张图一个（按钮上写年份或月份），外加拉取最新记录；刚拉取过时（`fetched`
    是新增的记录数）不再给拉取的按钮，标题说新增了多少。按标签长短排行（rows_fit），带月份的一行两个，免得字被截掉。"""
    others = [(label, f"/mrfz抽卡记录 第{n}页{role}") for n, label in enumerate(labels, 1) if n != number]
    current = f" 当前：{labels[number - 1]}" if len(labels) > 1 else ""
    if fetched is not None:
        return f"**明日方舟抽卡记录** {FETCHED.format(fetched)}{current}", rows_fit(others)
    return f"**更新明日方舟抽卡记录**{current}", [*rows_fit(others), [("拉取最新记录", f"/mrfz抽卡记录更新{role}")]]


def page_note(labels: list[str], number: int, role: str) -> list[str]:
    """写在图下方的翻页说明。按钮消息没人点会被撤回，图上也要看得出还有别的页、怎么翻。"""
    other = 2 if number == 1 else 1
    return [f"第 {number}/{len(labels)} 页 · {labels[number - 1]}（第 1 页最新）",
            f"看其他页发 /mrfz抽卡记录 第{other}页{role}，数字换成 1 到 {len(labels)}"]


NOTE_HTML = """<html><head><meta charset="utf-8"><style>
body {{ margin: 0; background: rgb{bg}; }}
div {{ padding: 12px 24px 16px; color: #e6e6e6; font-size: 20px; line-height: 1.6; text-align: center; font-family: sans-serif; }}
</style></head><body><div>{lines}</div></body></html>"""
NOTE_WIDTH = 720  # 和上游抽卡记录页面的宽度相同（截图时再按同样的缩放放大）


async def _with_note(picture: bytes, lines: list[str]) -> bytes:
    """在图的下方接一条同色的说明栏；做不到时原样返回（成员要的是记录本身）。"""
    try:
        with Image.open(BytesIO(picture)) as im:
            width, colour = im.width, im.convert("RGB").getpixel((2, im.height - 2))
        bar = await html_to_pic(NOTE_HTML.format(bg=tuple(colour), lines="<br>".join(escape(line) for line in lines)),
                                viewport={"width": NOTE_WIDTH, "height": 10}, device_scale_factor=width / NOTE_WIDTH)
        return await asyncio.to_thread(_stack, picture, bar)
    except Exception as e:
        logger.warning(f"Arknights gacha page note not added: {type(e).__name__}")
        return picture


def _stack(picture: bytes, bar: bytes) -> bytes:
    with Image.open(BytesIO(picture)) as top, Image.open(BytesIO(bar)) as bottom:
        kind = top.format or "PNG"
        top, bottom = top.convert("RGB"), bottom.convert("RGB")
        if bottom.width != top.width:
            bottom = bottom.resize((top.width, round(bottom.height * top.width / bottom.width)), Image.LANCZOS)
        sheet = Image.new("RGB", (top.width, top.height + bottom.height))
        sheet.paste(top, (0, 0))
        sheet.paste(bottom, (0, top.height))
    out = BytesIO()
    if kind == "WEBP":  # 和 skland_compact_images 压缩出来的一样
        sheet.save(out, "WEBP", quality=60, method=4)
    else:
        sheet.save(out, kind)
    return out.getvalue()


def _page_requested() -> int | None:
    matcher = current_matcher.get(None)
    result = getattr(matcher.state.get(ALCONNA_RESULT), "result", None) if matcher else None
    value = result.query("gacha.page.page") if result else None
    return int(value) if value else None


async def show_saved(user_session: UserSession, *args, **kwargs) -> None:
    """出图或发送意外出错时，至少告诉成员没成功（不能一声不响）。森空岛的错误照旧交给上游；
    截图超时由 skland_compact_images 说过了，这里不再重复。"""
    try:
        await _show_saved(user_session, *args, **kwargs)
    except (MatcherException, SklandException, PageTimeout):
        raise
    except Exception as e:
        if upload_timed_out(e):  # 图已经做好，只是上传太久
            logger.warning("Arknights gacha page: picture upload timed out")
            await UniMessage.text(SLOW_UPLOAD).send(reply_to=True)
            return
        logger.warning(f"Arknights gacha page failed: {type(e).__name__}")
        gacha_command.send_reaction(user_session, "fail")
        await UniMessage.text("抽卡记录图片生成失败，请稍后再试").send(reply_to=True)


async def _after_page(labels: list[str], number: int, role: str, fetched: int | None, hint: str) -> None:
    """自己的记录的一张图之后：挂上其他各页和拉取最新记录的按钮，标题里写新增条数和小黑盒提示；
    挂不了按钮（没有按钮的机器人、只有一张图又刚拉取过、按钮被拒）时，新增条数和提示改发文字。"""
    title, rows = page_menu(labels, number, role, fetched)
    said = ([FETCHED.format(fetched)] if fetched is not None else []) + ([hint] if hint else [])
    # 无文字兜底：怎么看其他页已写在图下方（page_note），这里另发的只是新增条数和提示，不算菜单
    if not await send_buttons(title + (f"\n{plain(hint)}" if hint else ""), rows) and said:
        await UniMessage.text("\n".join(said)).send()


async def _show_saved(
    user_session: UserSession,
    session: async_scoped_session,
    begin: Match[int],
    limit: Match[int],
    target: Match[At | int],
    bot: Bot,
    *,
    role_index: int | None = None,
    fetched: int | None = None,
) -> None:
    """只用已保存的记录生成上游的页面；页面上的资料（头像、等级）仍然来自角色卡。
    `fetched`：刚由 /mrfz抽卡记录更新 拉取过，新增了多少条。"""
    if target.available:
        target_platform_id = target.result.target if isinstance(target.result, At) else target.result
        target_id = (await get_user(user_session.platform, str(target_platform_id))).id
    else:
        target_id = user_session.user_id
    selected = await check_user_character(target_id, user_session, session, app_code="arknights", role_index=role_index)
    if selected is None:  # 原因已经发给成员了
        return
    user, character = selected
    gacha_command.send_reaction(user_session, "processing")
    records = await get_character_gacha_records(character.id, session)
    if not records:
        await session.rollback()
        await UniMessage.text(NO_RECORDS if fetched is None else NONE_FETCHED).send(reply_to=True)
        gacha_command.send_reaction(user_session, "done")
        return
    character_id = character.id
    earliest, imported = min((r.gacha_ts for r in records), default=None), any((r.pos or 0) < 0 for r in records)  # 会话结束前先取出来
    grouped = group_gacha_records(records)
    grouped.pools.sort(key=_opened, reverse=True)  # 游戏表里还没有的新卡池（开放时间为 0）也按时间排，不落到最后
    nickname, channel = character.nickname, character.channel_master_id
    try:
        card = await get_ark_card(user, character)
    except SklandException as error:
        await session.commit()
        await send_request_error(error)
        return
    await session.commit()  # 保存刷新过的 token
    if not card:
        return
    hint = "" if target.available else heybox_hint(earliest, imported, "arknights", card.status.registerTs, character_id)  # 只对看自己记录的人说
    first = begin.result if begin.available else None
    last = limit.result if limit.available else None
    start, stop, _ = slice(first, last).indices(len(grouped.pools))  # 负数和越界的 -b / -l 按切片的规则换算
    shown = max(0, stop - start)
    asked_page = _page_requested()
    # 成员自己的记录（没用 -b / -l）一次显示一张图，不管能不能挂按钮。
    by_year = not target.available and first is None and last is None
    if asked_page is not None or by_year:
        pages = [(label, start + b, start + e) for label, b, e in year_pages(grouped.pools[start:stop])] or [("", start, stop)]
        number = min(max(asked_page or 1, 1), len(pages))
        labels = [text for text, _, _ in pages]
        _, begin_at, end_at = pages[number - 1]
        page = await render.render_gacha_history(grouped, card.status, nickname=nickname, channel_master_id=channel, begin=begin_at, limit=end_at)
        role = f" 角色{role_index}" if role_index is not None else ""
        if len(pages) > 1 and not target.available:
            page = await _with_note(page, page_note(labels, number, role))
        await UniMessage.image(raw=page).send(at_sender=by_year and not can_show_buttons())  # 官方机器人上由桥接点名
        if by_year:
            await _after_page(labels, number, role, fetched, hint)
        elif hint:
            await UniMessage.text(hint).send()
        gacha_command.send_reaction(user_session, "done")
        return
    if shown <= config.gacha_render_max:
        page = await render.render_gacha_history(grouped, card.status, nickname=nickname, channel_master_id=channel, begin=first, limit=last)
        await UniMessage.image(raw=page).send()
    else:
        await UniMessage.text(SEVERAL_PICTURES).send(reply_to=True)
        gate = asyncio.Semaphore(4)
        step = config.gacha_render_max

        async def draw(index: int) -> bytes:
            async with gate:
                return await render.render_gacha_history(grouped, card.status, nickname=nickname, channel_master_id=channel, begin=index, limit=min(index + step, stop))

        starts = range(start, stop, step)
        pages = await asyncio.gather(*(draw(index) for index in starts))
        await UniMessage.reference(
            *(CustomNode(bot.self_id, f"{nickname} | {index}-{min(index + step, stop)}", UniMessage.image(raw=page)) for index, page in zip(starts, pages))
        ).send()
    if hint:
        await UniMessage.text(hint).send()
    gacha_command.send_reaction(user_session, "done")


gacha_command.gacha_handler = show_saved  # 上游的 `skland gacha` 在运行时才导入它

_gacha = next(option for option in skland_command.options if getattr(option, "name", "") == "gacha")
if not any(getattr(option, "name", "") == "--page" for option in _gacha.options):
    with command_manager.update(skland_command):
        _gacha.options.append(Option("-p|--page|page", Args["page", int], help_text="记录分成多张图时，只看第几张"))

update_command = on_command("mrfz抽卡记录更新", aliases={"方舟抽卡记录更新", "明日方舟抽卡记录更新"}, rule=strict, priority=5, block=True)


@update_command.handle()
async def _(user_session: UserSession, session: async_scoped_session, bot: Bot, arg: Message = CommandArg()) -> None:
    role = re.fullmatch(r"(?:(?:-r|--role)\s*(\d+))?", arg.extract_plain_text().strip())
    if role is None:
        await update_command.finish(USAGE)
    role_index = int(role[1]) if role[1] else None
    try:
        fetched = await _update(user_session, session, role_index)
    except MatcherException:
        raise
    except (SklandException, httpx.HTTPError) as e:
        await session.rollback()
        logger.warning(f"Arknights gacha update failed: {type(e).__name__}")
        gacha_command.send_reaction(user_session, "fail")
        await UniMessage.text(SYNC_FAILED).send(at_sender=True)
        return
    if fetched is not None:
        await session.refresh(user_session.user)  # _update 回滚过：成员对象已过期，不重新读一次，出图时一读成员编号就 MissingGreenlet
        nothing = Match(None, False)
        await show_saved(user_session, session, nothing, nothing, nothing, bot, role_index=role_index, fetched=fetched)


async def _update(user_session: UserSession, session: async_scoped_session, role_index: int | None) -> int | None:
    """拉取并保存所选角色的新记录，返回新增的条数；拉取不了时（原因已经告诉成员）返回 None。"""
    selected = await check_user_character(user_session.user_id, user_session, session, app_code="arknights", role_index=role_index)
    if selected is None:
        return None
    user, character = selected
    owner_id = user.owner_id
    role = SimpleNamespace(id=character.id, account_id=character.account_id, uid=character.uid, access_token=user.access_token)
    await session.rollback()  # 请求期间不持有事务
    if not role.access_token:
        await UniMessage.text(NO_TOKEN).send(at_sender=True)
        return None
    gacha_command.send_reaction(user_session, "processing")
    added = await _save(owner_id, role, await _pull(role, await SklandLoginAPI.get_grant_code(role.access_token, 1)))
    if isinstance(added, str):  # 拉取期间解绑了，或者换了账号
        gacha_command.send_reaction(user_session, "fail")
        await UniMessage.text(SYNC_FAILED).send(at_sender=True)
        return None
    logger.info(f"Arknights gacha update: {added} new records")
    await _tell_listeners(owner_id, "success", added, True)  # 和排队的同步一样（例如小黑盒导入据此核对）
    return added


# ── the sync ───────────────────────────────────────────────────────────


async def _pull(role, grant: str) -> dict[tuple[int, int], dict]:
    """向游戏要一个明日方舟角色的抽卡记录（官方只给最近一段时间的），按（时间，位置）去重。"""
    role_token = await SklandLoginAPI.get_role_token_by_uid(role.uid, grant)
    cookie = await SklandLoginAPI.get_ak_cookie(role_token)
    records = {}
    for category in await SklandAPI.get_gacha_categories(role.uid, role_token, role.access_token, cookie):
        async for record in get_all_gacha_records(role, category, role.access_token, role_token, cookie):
            records[(record.gacha_ts_sec, record.pos)] = dict(
                character_id=role.id, pool_id=record.poolId, pool_name=record.poolName, char_id=record.charId,
                char_name=record.charName, rarity=record.rarity, is_new=record.isNew, gacha_ts=record.gacha_ts_sec, pos=record.pos,
            )
        await asyncio.sleep(0.5)
    return records


async def _save(owner_id: int, role, records: dict) -> int | str:
    """写入还没有的记录，返回新增的条数；拉取期间解绑或换了账号时什么都不写，返回原因。"""
    async with get_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))  # 防止与另一次同步、解绑和重新绑定冲突
        current = (await session.execute(
            select(SkUser.owner_id, SkUser.access_token, Character.app_code).join(Character.account).where(Character.id == role.id))).first()
        if current is None:
            return "unbound"
        if tuple(current) != (owner_id, role.access_token, "arknights"):
            return "binding_changed"
        added = await insert_new(session, role.id, records.values())
        await session.commit()
    return added


async def insert_new(session, character_id: int, rows: Iterable[dict]) -> int:
    """把这个角色还没有的记录一次批量插入，返回插入的条数。

    每晚拉到的几乎全是已经存过的记录：先查出已有的（时间，位置），只插新的。插入时仍然遇到重复就跳过，以防万一。
    """
    have = set((await session.execute(select(GachaRecord.gacha_ts, GachaRecord.pos).where(GachaRecord.character_id == character_id))).tuples())
    new = [row for row in rows if (row["gacha_ts"], row["pos"]) not in have]
    if not new:
        return 0
    # 用表而不是 ORM 实体插入：批量执行时才有 rowcount
    result = await session.execute(insert(GachaRecord.__table__).on_conflict_do_nothing(index_elements=["character_id", "gacha_ts", "pos"]), new)
    return max(result.rowcount, 0)


async def sync_user(owner_id: int) -> tuple[str, int]:
    """同步成员的每个明日方舟角色：返回（"success" | "partial" | 原因，新增行数）；全部角色失败时抛异常，让队列重试。"""
    async with get_session() as session:  # 请求期间不持有事务
        rows = (await session.execute(
            select(Character.id, Character.account_id, Character.uid, SkUser.access_token)
            .join(Character.account).where(SkUser.owner_id == owner_id, Character.app_code == "arknights")
            .order_by(Character.account_id, Character.id))).all()
    if not rows:
        return "no_arknights_character", 0
    roles = [role for role in rows if role.access_token]
    if not roles:
        return "no_access_token", 0
    total, synced, errors, grants = 0, 0, [], {}
    for role in roles:
        try:
            if role.account_id not in grants:
                grants[role.account_id] = await SklandLoginAPI.get_grant_code(role.access_token, 1)
            records = await _pull(role, grants[role.account_id])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(exc)
            logger.warning("auto-arkgacha role_failed error_type={} said={}", type(exc).__name__, re.sub(r"\d{5,}", "<id>", str(exc))[:80])  # 号码不进日志
            continue
        added = await _save(owner_id, role, records)
        if isinstance(added, str):
            return added, total
        total += added
        synced += 1
    if not synced:
        raise errors[0]
    return ("partial" if errors else "success"), total


async def _owners() -> list[int]:
    async with get_session() as session:
        return list(await session.scalars(select(SkUser.owner_id).join(Character, Character.account_id == SkUser.id).where(Character.app_code == "arknights").distinct()))


async def queue_daily() -> None:
    if _queue is None or _stopping:
        return
    now = datetime.now(timezone.utc)
    if _queue.day_due(now):
        logger.info("auto-arkgacha daily_queued users={}", _queue.enqueue_day(now, await _owners()))


async def queue_unseen() -> None:
    """有明日方舟角色却从未同步过的成员：新绑定的，以及刚开始时的所有人（一次）。"""
    if _queue is None or _stopping:
        return
    with _queue.connection() as db:
        known = {row[0] for row in db.execute("SELECT user_id FROM results UNION SELECT user_id FROM pending")}
    for owner in set(await _owners()) - known:
        _queue.enqueue(owner)


async def poll_queue() -> None:
    global _active
    if not _stopping and (_active is None or _active.done()):
        _active = asyncio.create_task(drain_queue(), name="skland-auto-arkgacha")


def queue_after_binding(owner_id: int) -> bool:
    """绑定（第一个账号、另一个账号、重新登录）之后立刻排一次同步，不等 queue_unseen。排上了返回 True。"""
    if _queue is None or _stopping:
        return False
    _queue.enqueue(owner_id)
    return True


def bind_sync_pending(owner_id: int) -> bool:
    """绑定时排入的同步，第一次尝试还没结束。"""
    return _queue is not None and _queue.waiting(owner_id)


# 排队的同步结束时通知其他插件，与 skland_auto_gacha 的 sync_listeners 相同：
#   await listener(user_id, status, new_records, final)   （final=False：已安排重试）
sync_listeners: list = []


async def _tell_listeners(uid: int, status: str, records: int, final: bool) -> None:
    for listener in list(sync_listeners):
        try:
            await listener(uid, status, records, final)
        except Exception as exc:  # 监听者绝不能卡住队列
            logger.warning("auto-arkgacha listener_failed error_type={}", type(exc).__name__)


_working: set[int] = set()  # 正在被某个 worker 同步的成员


async def _work() -> None:
    """一个 worker：反复取下一个没人在处理的成员来同步，直到队列里没有到期的任务项。"""
    while not _stopping and (item := _queue.next(_working)) is not None:
        _working.add(item[0])
        try:
            try:
                async with asyncio.timeout(600):
                    status, records = await sync_user(item[0])
            except asyncio.CancelledError:
                raise  # 这一项留在队列里，下次启动再处理
            except Exception as exc:
                _queue.finish(item, "failed", retry=True)
                logger.warning("auto-arkgacha update_failed reason={} attempt={} error_type={}", item[2], item[3] + 1, type(exc).__name__)
                await _tell_listeners(item[0], "failed", 0, item[3] >= 2)
            else:
                _queue.finish(item, status, records)
                logger.info("auto-arkgacha update_done reason={} status={} new_records={}", item[2], status, records)
                await _tell_listeners(item[0], status, records, True)
        finally:
            _working.discard(item[0])
        await asyncio.sleep(REQUEST_GAP)


async def drain_queue() -> None:
    global _active
    if _queue is None or _busy.locked() or _stopping:
        return
    async with _busy:
        _active = asyncio.current_task()
        try:
            await queue_daily()  # 也补上机器人停机时错过的时段
            async with asyncio.TaskGroup() as group:
                for _ in range(WORKERS):
                    group.create_task(_work())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("auto-arkgacha worker_failed error_type={}", type(exc).__name__)
        finally:
            _active = None


async def _daily_tick() -> None:
    try:
        await queue_daily()
    except Exception as exc:
        logger.error("auto-arkgacha daily_queue_failed error_type={}", type(exc).__name__)


async def _unseen_tick() -> None:
    try:
        await queue_unseen()
    except Exception as exc:
        logger.error("auto-arkgacha unseen_queue_failed error_type={}", type(exc).__name__)


@get_driver().on_startup
async def _start() -> None:
    global _queue, _stopping
    _queue = Queue(STATE)
    _queue.initialize_day(datetime.now(timezone.utc))
    _stopping = False
    scheduler.add_job(_daily_tick, TRIGGER, id=DAILY_ID, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=86400)
    scheduler.add_job(poll_queue, "interval", seconds=10, id=WORKER_ID, replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(_unseen_tick, "interval", seconds=SEEN_SECONDS, id=SEEN_ID, replace_existing=True, max_instances=1, coalesce=True)


@get_driver().on_shutdown
async def _stop() -> None:
    global _stopping
    _stopping = True
    if _active is not None and _active is not asyncio.current_task():
        _active.cancel()
        await asyncio.gather(_active, return_exceptions=True)
