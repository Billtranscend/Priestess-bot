"""/zmd抽卡记录 and /zmd抽卡记录更新: this project's Endfield gacha page.

nonebot-plugin-skland 0.7.2 replaced its page with one that syncs on every query and is cut
into fixed-height pages in the browser. This project keeps the behaviour its members know:

  /zmd抽卡记录        shows what is stored (fast, no request to the game's servers);
  /zmd抽卡记录更新    fetches the official records first, then shows them;

paged by pool count, drawn with the local Endfield template (gifts at pull milestones, free
ten-pulls, the date the records start). Only the data access follows 0.7.2: records belong to
a role (`character_id`), and the role is the member's default one or the one picked with -r.

The upstream handler is replaced as a module attribute (upstream looks it up on every call),
and its `efgacha` subcommand gets back the `-u` option that 0.7.2 dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
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
BASE_MIN_WIDTH, JOINT_MIN_WIDTH, VIEWPORT_PADDING = 680, 900, 120  # the page is wider with 联合寻访 pools
CN = timezone(timedelta(hours=8))
MANY_POOLS = "抽卡记录过多，将以多张图片形式发送"  # plugins.forward_reply_owner recognises this line
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


# ── the picture ────────────────────────────────────────────────────────


async def render_page(record, *, avatar_url: str, nickname: str, role_id: str, begin: int | None = None, limit: int | None = None) -> bytes:
    """One picture with the pools `begin`..`limit` of every category (all of them when both are None)."""
    min_width = JOINT_MIN_WIDTH if record.joint_pools else BASE_MIN_WIDTH
    templates = {
        "avatar_url": avatar_url,
        "record": record,
        "character": SimpleNamespace(nickname=nickname, role_id=role_id),
        "ef_gacha_min_width": min_width,
        "start_index": begin,
        "end_index": limit,
        "ef_fonts": ef_theme.fonts_css(),
    }
    try:
        templates.update(gacha_extras(record))
    except Exception as e:  # the page still renders, only without the marks
        logger.warning(f"Endfield gacha page: extras unavailable: {type(e).__name__}: {e}")
        templates.update({"ef_gifts": {}, "ef_since": "", "ef_imported": 0})
    # render.template_to_pic is looked up at call time: skland_compact_images turns the picture into WebP.
    return await render.template_to_pic(
        template_path=str(LOCAL_DIR),
        template_name=TEMPLATE,
        templates=templates,
        filters={"format_timestamp_md": format_timestamp_md, "ef_charId_to_avatarUrl": ef_charId_to_avatarUrl},
        pages={"viewport": {"width": min_width + VIEWPORT_PADDING, "height": 1}, "base_url": TEMPLATES_DIR.as_uri()},
        device_scale_factor=1.5,
        screenshot_timeout=config.render_timeout,
    )


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

    first = begin.result if begin.available else None
    last = limit.result if limit.available else None
    render_max = config.ef_gacha_render_max
    # -b / -l count pools per category; the longest category decides the paging.
    start = first if first is not None else 0
    longest = max(len(pools) for pools in (gacha_data.special_pools, gacha_data.weapon_pools, gacha_data.joint_pools, gacha_data.standard_pools, gacha_data.beginner_pools))
    end = min(last, longest) if last is not None else longest

    async def page(a: int | None, b: int | None) -> bytes:
        return await render_page(gacha_data, avatar_url=avatar_url, nickname=nickname, role_id=role_id, begin=a, limit=b)

    if max(0, end - start) > render_max:
        await UniMessage.text(MANY_POOLS).send(reply_to=True)
        spans = [(i, min(i + render_max, end)) for i in range(start, end, render_max)]
        limiter = asyncio.Semaphore(4)

        async def bounded(a: int, b: int) -> bytes:
            async with limiter:
                return await page(a, b)

        images = await asyncio.gather(*(bounded(a, b) for a, b in spans))
        if user_session.platform == "QQClient":
            nodes = [CustomNode(bot.self_id, f"{nickname} | 卡池 {a + 1}-{b}", UniMessage.image(raw=image)) for (a, b), image in zip(spans, images)]
            await UniMessage.reference(*nodes).send()
        else:
            for image in images:
                await UniMessage.image(raw=image).send()
    else:
        await UniMessage.image(raw=await page(first, last)).send(at_sender=True)
    logger.info(
        f"Endfield gacha page sent: {gacha_data.total_pulls} pulls "
        f"(characters {gacha_data.char_total_pulls} + weapons {gacha_data.weapon_total_pulls}), {new_count} new"
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
