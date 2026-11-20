"""pErithacus 管理指令的 SUPERUSER 保护，出错时一律拒绝；机器人不在某个群后删掉该群的关键词。

收到“机器人不在群了”的通知时先按 plugins.shared.left_for_sure 确认（官方机器人直接算数，NapCat 要群列表确认）。
NapCat 掉线时被移出、群解散，都可能没有通知或没确认上：每天按群列表补清一次（groups_left）。
"""

import json

from nonebot import get_driver, logger, on_notice, require
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupDecreaseNoticeEvent
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from sqlalchemy import text

from plugins.shared import bot_left_group, groups_left, left_for_sure

require("nonebot_plugin_perithacus")
require("nonebot_plugin_orm")
require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler  # noqa: E402
from nonebot_plugin_orm import get_session  # noqa: E402
from nonebot_plugin_perithacus.command import pe  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="pErithacus management guard",
    description="Restricts every pErithacus management subcommand to SUPERUSER; drops a group's keywords when the bot leaves it.",
    usage="Loaded automatically.",
    type="application",
)

driver = get_driver()
Row = tuple[int, str, str]  # (id, source, scope)


def _assert_guard() -> None:
    if not driver.config.superusers:
        raise RuntimeError("SUPERUSERS must be non-empty for pErithacus management")
    if pe.permission is not SUPERUSER:
        raise RuntimeError("pErithacus management permission guard is not active")


pe.permission = SUPERUSER
_assert_guard()


@driver.on_startup
async def _verify_guard_on_startup() -> None:
    _assert_guard()


left = on_notice(rule=Rule(bot_left_group), priority=5, block=False)


def _scope(scope: str | None) -> list[str] | None:
    """scope 列里的群列表；不是一列字符串时为 None（这样的词条不碰）。"""
    try:
        groups = json.loads(scope) if scope else []
    except ValueError:
        return None
    return groups if isinstance(groups, list) and all(isinstance(g, str) for g in groups) else None


def purge_plan(rows: list[Row], gone: set[str]) -> tuple[dict[int, list[str]], list[int]]:
    """机器人不在 gone 里这些群（"g<群号>"）了：哪些词条要缩小 scope（id -> 剩下的群），哪些整条删掉。

    按“在哪些群生效”（scope）判断，而不是按在哪个群建的（source）：只在这些群生效的删掉；
    还在别的群生效的只去掉这些群。scope 为空、在这些群建的词条也删掉（它本来就只属于那个群）。"""
    narrowed, removed = {}, []
    for entry_id, source, scope in rows:
        groups = _scope(scope)
        if groups is None or not (gone.intersection(groups) or (source in gone and not groups)):
            continue
        rest = [g for g in groups if g not in gone]
        if rest:
            narrowed[entry_id] = rest
        else:
            removed.append(entry_id)
    return narrowed, removed


def groups_named(rows: list[Row]) -> set[int]:
    """词条里出现的群（生效范围和建词条的地方）的群号；私聊（"u…"）不算。"""
    places = (place for _, source, scope in rows for place in [source, *(_scope(scope) or [])])
    return {int(place[1:]) for place in places if isinstance(place, str) and place.startswith("g") and place[1:].isdigit()}


async def _rows(session) -> list[Row]:
    return [tuple(r) for r in (await session.execute(text("SELECT id, source, scope FROM nonebot_plugin_perithacus_index"))).all()]


async def _purge(gone: set[str]) -> None:
    """这些群的词条按 purge_plan 处理。"""
    async with get_session() as session:
        narrowed, removed = purge_plan(await _rows(session), gone)
        if not (narrowed or removed):
            return
        for entry_id, rest in narrowed.items():
            await session.execute(text("UPDATE nonebot_plugin_perithacus_index SET scope = :scope WHERE id = :id"), {"scope": json.dumps(rest), "id": entry_id})
        for entry_id in removed:
            await session.execute(text("DELETE FROM nonebot_plugin_perithacus_content WHERE entry_id = :id"), {"id": entry_id})
            await session.execute(text("DELETE FROM nonebot_plugin_perithacus_index WHERE id = :id"), {"id": entry_id})
        await session.commit()
    logger.info(f"pErithacus guard: the bot is no longer in {len(gone)} group(s); keyword entries removed {len(removed)}, kept for other groups {len(narrowed)}")


@left.handle()
async def _(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """机器人不在这个群了：确认之后，这个群的词条按 purge_plan 处理。"""
    if await left_for_sure(bot, event.group_id):
        await _purge({f"g{event.group_id}"})


@scheduler.scheduled_job("cron", hour=4, minute=50, timezone="Asia/Shanghai", id="perithacus_guard_recheck", misfire_grace_time=3600)
async def recheck_groups() -> None:
    """每天补清一次：词条里出现、机器人已经不在的群。"""
    async with get_session() as session:
        named = groups_named(await _rows(session))
    if gone := await groups_left(named):
        await _purge({f"g{group}" for group in gone})
