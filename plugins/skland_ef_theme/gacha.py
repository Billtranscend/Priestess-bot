"""/zmd抽卡记录 and /zmd抽卡记录更新: this project's Endfield gacha page.

nonebot-plugin-skland 0.7.2 replaced its page with one that syncs on every query and is cut
into fixed-height pages in the browser. This project keeps the behaviour its members know:

  /zmd抽卡记录        shows what is stored (fast, no request to the game's servers);
  /zmd抽卡记录更新    fetches the official records first, then shows them;

drawn with the local Endfield template (gifts at pull milestones, free ten-pulls, the date the
records start). Only the data access follows 0.7.2: records belong to a role (`character_id`),
and the role is the member's default one or the one picked with -r.

One picture holds one kind of pool:
  限定池 and 武器池   each its own picture, one column of pools while that fits; too tall, the
                      pools go into two columns, then three, and after that into further
                      pictures (限定池 1/2, 2/2 ...);
  其他卡池            常驻池, 联合寻访, 新手池 and whatever kind comes later, one column per
                      kind, three kinds to a picture.
"Fits" is a height in pixels of the finished picture (MAX_HEIGHT_PX). The pools' heights are
measured in the browser first, with the page laid out but not drawn.

The upstream handler is replaced as a module attribute (upstream looks it up on every call),
and its `efgacha` subcommand gets back the `-u` option that 0.7.2 dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from nonebot import logger
from nonebot.adapters import Bot
from nonebot.matcher import current_matcher
from nonebot_plugin_alconna import At, CustomNode, Match, Option, UniMessage, command_manager
from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_orm import async_scoped_session
from nonebot_plugin_skland import render
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.commands import endfield as upstream_endfield
from nonebot_plugin_skland.commands.selection import check_user_character
from nonebot_plugin_skland.compact import open_html_page, template_to_html
from nonebot_plugin_skland.config import TEMPLATES_DIR, config
from nonebot_plugin_skland.data_source import ef_gacha_pool_data
from nonebot_plugin_skland.db_handler import get_character_gacha_records
from nonebot_plugin_skland.exception import SklandException
from nonebot_plugin_skland.filters import ef_charId_to_avatarUrl, format_timestamp_md
from nonebot_plugin_skland.matcher import skland_command
from nonebot_plugin_skland.model import SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.services.auth import CredentialState, refresh_credentials
from nonebot_plugin_skland.services.gacha import group_ef_gacha_records, sync_ef_gacha_records
from nonebot_plugin_skland.utils.message import send_reaction
from nonebot_plugin_user import UserSession, get_user
from sqlalchemy import update as sql_update

from plugins import ef_theme

LOCAL_DIR = Path(__file__).with_name("templates")
TEMPLATE = "ef_gacha.html.jinja2"
COLUMN_WIDTH, COLUMN_GAP, PAGE_PADDING = 250, 10, 18  # CSS px; a pool card is always this wide
MAX_COLUMNS = 3
# Device scale per column count: a narrow picture is drawn larger so that it stays sharp on a phone.
SCALE = {1: 2.5, 2: 2.0, 3: 1.5}
# The tallest picture that is sent, in pixels as drawn. WebP ends at 16383 px a side; the pages of
# members with many pools have been about this tall for weeks and reach QQ fine.
MAX_HEIGHT_PX = 15000
OTHER_KINDS = (("standard", "常驻池"), ("joint", "联合寻访"), ("beginner", "新手池"))  # anything else gets its pool name
CN = timezone(timedelta(hours=8))
SEVERAL_PICTURES = "抽卡记录将按卡池类型分成多张图片发送"  # plugins.forward_reply_owner recognises this line
NO_RECORDS = "暂无抽卡记录，请先发送 /zmd抽卡记录更新 拉取数据"
NO_TOKEN = "这个角色所属的森空岛账号没有保存登录凭证，无法拉取抽卡记录。请重新发送 /skl绑定 扫码"
SYNC_FAILED = "抽卡记录拉取失败，请稍后再试。如果一直失败，可能是登录凭证过期了，重新发送 /skl绑定 扫码即可"


# ── template values upstream has no data for ───────────────────────────


def _gift_rules() -> dict:
    """Pool id -> milestone gifts, from the wiki index (plugins.endfield_wiki.data.build_gacha_gifts)."""
    with contextlib.suppress(Exception):
        from plugins import endfield_wiki

        loaded = endfield_wiki._resolver()
        return ((loaded[0] if loaded else {}).get("calendar") or {}).get("gifts") or {}
    return {}


def gacha_extras(record) -> dict:
    """Gifts per pool, and where the records start.

    ef_gifts: {pool id: {n: [gift]}}: the gifts triggered by the n-th paid pull of that pool.
    """
    rules = _gift_rules()
    gifts: dict[str, dict[int, list]] = {}
    first, imported = None, 0
    for pool in record.all_pools:
        for group in pool.records:
            first = group.gacha_ts if first is None else min(first, group.gacha_ts)
            imported += sum(1 for pull in group.pulls if pull.seq_id < 0)  # rows rebuilt by heybox_import
        paid = pool.paid_pulls
        for rule in rules.get(pool.pool_id, []):
            at, n = rule["at"], 0
            while at <= paid:
                gifts.setdefault(pool.pool_id, {}).setdefault(at, []).append({**rule["items"][n % len(rule["items"])], "at": at})
                if not rule["every"]:
                    break
                at, n = at + rule["every"], n + 1
    return {
        "ef_gifts": gifts,
        "ef_since": datetime.fromtimestamp(first, CN).strftime("%Y-%m-%d") if first else "",
        "ef_imported": imported,
    }


# ── the pictures ───────────────────────────────────────────────────────


@dataclass
class Column:
    title: str
    pools: list


@dataclass
class Sheet:
    """One picture."""

    kind: str  # "special" | "weapon" | "other"
    title: str
    columns: list[Column]
    tiles: tuple[str, ...] = ()
    part: str = ""  # "1/2" when a kind needs several pictures
    scale: float = 0.0  # device scale; by column count unless given (the pictures of one set share theirs)
    label: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.label = f"{self.title} {self.part}".strip()
        self.scale = self.scale or SCALE[min(len(self.columns), MAX_COLUMNS)]

    @property
    def width(self) -> int:
        return len(self.columns) * COLUMN_WIDTH + (len(self.columns) - 1) * COLUMN_GAP

    @property
    def page_width(self) -> int:
        return self.width + 2 * PAGE_PADDING

    @property
    def col_width(self) -> int:
        return COLUMN_WIDTH

    @property
    def padding(self) -> int:
        return PAGE_PADDING


def _kind(pool) -> str:
    """Weapons by what was pulled: a rerun weapon pool ("rerun_wpn_...") is not named like the others."""
    return "weapon" if pool.pool_type == "weapon" else pool.pool_category


def _split(heights: list[float], columns: int) -> list[list[int]]:
    """Consecutive pools into `columns` runs so that the tallest run is as short as possible."""
    def runs(cap: float) -> list[list[int]]:
        out, total = [[]], 0.0
        for i, h in enumerate(heights):
            if out[-1] and total + COLUMN_GAP + h > cap:
                out.append([])
                total = 0.0
            total += h + (COLUMN_GAP if len(out[-1]) else 0)
            out[-1].append(i)
        return out

    low, high = max(heights), sum(heights) + COLUMN_GAP * len(heights)
    for _ in range(40):
        middle = (low + high) / 2
        low, high = (low, middle) if len(runs(middle)) <= columns else (middle, high)
    return runs(high)


def arrange(heights: list[float], chrome: float) -> list[list[list[int]]]:
    """Pictures -> columns -> pool numbers, for one kind of pool.

    `heights`: the pool cards in CSS px, in display order; `chrome`: everything else on the page.
    One column if the picture stays under MAX_HEIGHT_PX, else two, else three; beyond that the
    pools continue on further three-column pictures. A single pool taller than a whole picture
    still gets its own column.
    """
    for columns in range(1, MAX_COLUMNS + 1):
        runs = _split(heights, columns)
        tallest = max(sum(heights[i] for i in run) + COLUMN_GAP * (len(run) - 1) for run in runs)
        if (chrome + tallest) * SCALE[len(runs)] <= MAX_HEIGHT_PX or len(heights) == 1:
            return [runs]
    room = MAX_HEIGHT_PX / SCALE[MAX_COLUMNS] - chrome
    runs, total = [[]], 0.0
    for i, h in enumerate(heights):
        if runs[-1] and total + COLUMN_GAP + h > room:
            runs.append([])
            total = 0.0
        total += h + (COLUMN_GAP if runs[-1] else 0)
        runs[-1].append(i)
    return [runs[i : i + MAX_COLUMNS] for i in range(0, len(runs), MAX_COLUMNS)]


def _templates(record, sheet: Sheet, *, avatar_url: str, nickname: str, role_id: str, extras: dict) -> dict:
    return {
        "avatar_url": avatar_url,
        "record": record,
        "character": SimpleNamespace(nickname=nickname, role_id=role_id),
        "view": sheet,
        "ef_fonts": ef_theme.fonts_css(),
        **extras,
    }


FILTERS = {"format_timestamp_md": format_timestamp_md, "ef_charId_to_avatarUrl": ef_charId_to_avatarUrl}
_MEASURE = """async () => {
  await document.fonts.ready;
  const pools = [...document.querySelectorAll('.pool')].map(e => e.getBoundingClientRect().height);
  return {total: document.documentElement.scrollHeight, pools};
}"""


async def measure(record, sheet: Sheet, **context) -> tuple[list[float], float]:
    """(height of every pool card, height of the rest of the page) with all pools of the sheet in one column."""
    html = await template_to_html(str(LOCAL_DIR), TEMPLATE, filters=FILTERS, **_templates(record, sheet, **context))
    viewport = {"width": sheet.page_width, "height": 1}
    async with open_html_page(html, template_path=LOCAL_DIR.as_uri(), wait_until="domcontentloaded", device_scale_factor=1, viewport=viewport, base_url=TEMPLATES_DIR.as_uri()) as page:
        sizes = await page.evaluate(_MEASURE)
    heights = [float(h) for h in sizes["pools"]]
    return heights, float(sizes["total"]) - sum(heights) - COLUMN_GAP * max(0, len(heights) - 1)


async def plan(record, *, begin: int | None = None, limit: int | None = None, **context) -> list[Sheet]:
    """The pictures for this record, in the order they are sent."""
    by_kind: dict[str, list] = {}
    for pool in record.all_pools:
        by_kind.setdefault(_kind(pool), []).append(pool)
    if begin is not None or limit is not None:  # -b / -l still count pools per kind
        by_kind = {kind: pools[begin or 0 : limit] for kind, pools in by_kind.items()}
    sheets: list[Sheet] = []
    for kind, title, tiles in (("special", "限定池", ("special",)), ("weapon", "武器池", ("weapon", "arsenal"))):
        pools = by_kind.pop(kind, [])
        if not pools:
            continue
        try:
            heights, chrome = await measure(record, Sheet(kind, title, [Column(title, pools)], tiles), **context)
            pictures = arrange(heights, chrome) if len(heights) == len(pools) else [[list(range(len(pools)))]]
        except Exception as e:  # one tall column is still a correct picture
            logger.warning(f"Endfield gacha page: layout not measured ({type(e).__name__}: {e})")
            pictures = [[list(range(len(pools)))]]
        for n, runs in enumerate(pictures, 1):
            columns = [Column(title if not i else f"{title} 续", [pools[j] for j in run]) for i, run in enumerate(runs)]
            several = len(pictures) > 1  # then every picture was sized for three columns, the last one too
            sheets.append(Sheet(kind, title, columns, tiles if n == 1 else (), f"{n}/{len(pictures)}" if several else "", SCALE[MAX_COLUMNS] if several else 0.0))
    names = dict(OTHER_KINDS)
    others = [(kind, names[kind], by_kind.pop(kind)) for kind, _ in OTHER_KINDS if by_kind.get(kind)]
    others += [(kind, pools[0].pool_name, pools) for kind, pools in by_kind.items() if pools]
    groups = [others[i : i + MAX_COLUMNS] for i in range(0, len(others), MAX_COLUMNS)]
    for n, group in enumerate(groups, 1):
        tiles = tuple(kind for kind, _, _ in group if kind in names)
        sheets.append(Sheet("other", "其他卡池", [Column(title, pools) for _, title, pools in group], tiles, f"{n}/{len(groups)}" if len(groups) > 1 else ""))
    return sheets


async def render_sheet(record, sheet: Sheet, **context) -> bytes:
    # render.template_to_pic is looked up at call time: skland_compact_images turns the picture into WebP.
    return await render.template_to_pic(
        template_path=str(LOCAL_DIR),
        template_name=TEMPLATE,
        templates=_templates(record, sheet, **context),
        filters=FILTERS,
        pages={"viewport": {"width": sheet.page_width, "height": 1}, "base_url": TEMPLATES_DIR.as_uri()},
        device_scale_factor=sheet.scale,
        screenshot_timeout=config.render_timeout,
    )


async def render_pages(record, *, avatar_url: str, nickname: str, role_id: str, begin: int | None = None, limit: int | None = None) -> list[tuple[Sheet, bytes]]:
    """Every picture of the record with its sheet."""
    try:
        extras = gacha_extras(record)
    except Exception as e:  # the page still renders, only without the marks
        logger.warning(f"Endfield gacha page: extras unavailable: {type(e).__name__}: {e}")
        extras = {"ef_gifts": {}, "ef_since": "", "ef_imported": 0}
    context = {"avatar_url": avatar_url, "nickname": nickname, "role_id": role_id, "extras": extras}
    sheets = await plan(record, begin=begin, limit=limit, **context)
    limiter = asyncio.Semaphore(3)

    async def bounded(sheet: Sheet) -> bytes:
        async with limiter:
            return await render_sheet(record, sheet, **context)

    return list(zip(sheets, await asyncio.gather(*(bounded(sheet) for sheet in sheets))))


# ── the command ────────────────────────────────────────────────────────


def _update_requested() -> bool:
    matcher = current_matcher.get(None)
    result = getattr(matcher.state.get(ALCONNA_RESULT), "result", None) if matcher else None
    return bool(result and result.find("efgacha.update"))


async def _pool_marks(gacha_data, server_id: str) -> None:
    """UP operators / weapons of the pools that have them: local table first, the game's API otherwise."""
    for pool in gacha_data.special_pools + gacha_data.joint_pools + gacha_data.weapon_pools:
        local_pool = ef_gacha_pool_data.get_pool(pool.pool_id)
        if local_pool:
            pool.up_six_chars = local_pool.up_six_char_ids
            pool.up6_img = local_pool.up6_image or local_pool.rotate_image
            pool.up6_name = local_pool.up_six_display_name
            continue
        try:
            content = await SklandAPI.get_ef_gacha_content(pool.pool_id, server_id)
            pool.up_six_chars = content.pool.up_six_char_ids
            pool.up6_img = content.pool.up6_image or content.pool.rotate_image
            pool.up6_name = content.pool.up_six_display_name
        except Exception as e:
            logger.warning(f"Endfield pool metadata unavailable: {type(e).__name__}")


async def ef_gacha_history_handler(
    user_session: UserSession,
    session: async_scoped_session,
    begin: Match[int],
    limit: Match[int],
    target: Match[At | int],
    bot: Bot,
    *,
    role_index: int | None = None,
) -> None:
    update = _update_requested()
    if target.available:
        target_platform_id = target.result.target if isinstance(target.result, At) else target.result
        target_id = (await get_user(user_session.platform, str(target_platform_id))).id
    else:
        target_id = user_session.user_id
    selected = await check_user_character(target_id, user_session, session, app_code="endfield", role_index=role_index)
    if selected is None:  # the reason has been sent
        return
    user, character = selected
    # Plain values only from here on: the read transaction ends before any request is made.
    account_id, owner_id, remote_user_id = user.id, user.owner_id, user.skland_user_id
    credentials = CredentialState(user.access_token, user.cred, user.cred_token)
    original_credentials = (credentials.cred, credentials.cred_token)
    character_id, uid = character.id, character.uid
    role_id, server_id, nickname = character.role_id, character.channel_master_id, character.nickname
    await session.rollback()
    send_reaction(user_session, "processing")

    new_count = 0
    if update:
        if not credentials.access_token:
            send_reaction(user_session, "fail")
            await UniMessage(NO_TOKEN).send(at_sender=True)
            return
        try:
            gacha_data, new_count = await sync_ef_gacha_records(
                session, character_id=character_id, uid=uid, server_id=server_id, access_token=credentials.access_token
            )
        except SklandException as e:
            await session.rollback()
            logger.warning(f"Endfield gacha update failed: {type(e).__name__}")
            send_reaction(user_session, "fail")
            await UniMessage(SYNC_FAILED).send(at_sender=True)
            return
    else:
        gacha_data = group_ef_gacha_records(await get_character_gacha_records(character_id, session))
        await session.rollback()
    if not gacha_data.total_pulls:
        await UniMessage.text("已拉取，暂时没有抽卡记录" if update else NO_RECORDS).send(reply_to=True)
        return
    await _pool_marks(gacha_data, server_id)

    @refresh_credentials
    async def profile(state: CredentialState):
        return await SklandAPI.endfield_card(CRED(cred=state.cred, token=state.cred_token), user_id=remote_user_id, role_id=role_id, server_id=server_id)

    avatar_url = ""
    try:
        if remote_user_id and role_id:
            avatar_url = (await profile(credentials)).base.avatarUrl
    except Exception as e:  # the avatar is decoration; the records are what was asked for
        logger.warning(f"Endfield profile unavailable: {type(e).__name__}")
    finally:
        if (credentials.cred, credentials.cred_token) != original_credentials:
            await session.execute(
                sql_update(SkUser)
                .where(SkUser.id == account_id, SkUser.owner_id == owner_id)
                .values(cred=credentials.cred, cred_token=credentials.cred_token)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

    pages = await render_pages(
        gacha_data, avatar_url=avatar_url, nickname=nickname, role_id=role_id,
        begin=begin.result if begin.available else None, limit=limit.result if limit.available else None,
    )
    if not pages:
        await UniMessage.text(NO_RECORDS).send(reply_to=True)
        return
    if len(pages) == 1:
        await UniMessage.image(raw=pages[0][1]).send(at_sender=True)
    elif user_session.platform == "QQClient":
        await UniMessage.text(SEVERAL_PICTURES).send(reply_to=True)
        await UniMessage.reference(*(CustomNode(bot.self_id, f"{nickname} | {sheet.label}", UniMessage.image(raw=image)) for sheet, image in pages)).send()
    else:
        for _, image in pages:
            await UniMessage.image(raw=image).send()
    logger.info(
        f"Endfield gacha page sent: {gacha_data.total_pulls} pulls "
        f"(characters {gacha_data.char_total_pulls} + weapons {gacha_data.weapon_total_pulls}), {new_count} new, "
        f"pictures {[f'{sheet.label} x{len(sheet.columns)}' for sheet, _ in pages]}"
    )
    send_reaction(user_session, "done")


# ── installation ───────────────────────────────────────────────────────

_upstream_handler = upstream_endfield.ef_gacha_history_handler
if getattr(_upstream_handler, "_local_gacha_page", False):
    raise RuntimeError("Endfield gacha page already installed")
ef_gacha_history_handler._local_gacha_page = True
upstream_endfield.ef_gacha_history_handler = ef_gacha_history_handler

_efgacha = next(option for option in skland_command.options if getattr(option, "name", "") == "efgacha")
if not any("update" in getattr(option, "aliases", ()) or getattr(option, "name", "") == "--update" for option in _efgacha.options):
    with command_manager.update(skland_command):
        _efgacha.options.append(Option("-u|--update|update", help_text="先从官方接口拉取最新记录再展示"))


def verify() -> None:
    """The replacement and the -u option are still in place (called at startup)."""
    if upstream_endfield.ef_gacha_history_handler is not ef_gacha_history_handler:
        raise RuntimeError("Endfield gacha page handler was replaced")
    prefix = next(iter(skland_command.prefixes), "")
    if not skland_command.parse(f"{prefix}skland efgacha -u").find("efgacha.update"):
        raise RuntimeError("Endfield gacha -u option is not active")
