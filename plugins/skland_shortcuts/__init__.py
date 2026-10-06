"""Idempotent project-local Skland shortcuts with protected administration."""

from __future__ import annotations

import os
import re
import shelve
from dataclasses import dataclass
from time import time_ns
from typing import Any

from nonebot import get_driver, logger, require
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_skland")

from nonebot_plugin_alconna import add_global_extension, command_manager
from nonebot_plugin_alconna.builtins.extensions.shortcut import (
    SuperUserShortcutExtension,
)
from nonebot_plugin_skland.config import CACHE_DIR
from nonebot_plugin_skland.matcher import skland, skland_command

__plugin_meta__ = PluginMetadata(
    name="Skland project shortcuts",
    description="Registers reviewed Skland aliases idempotently and persists them.",
    usage="Loaded automatically.",
    type="application",
)


@dataclass(frozen=True)
class ShortcutSpec:
    pattern: str
    command: str
    fuzzy: bool = False
    prefix: bool = True


ROLE = r"\s+(?:-r|--role)\s*(\d+)"


def _query(pattern: str, command: str) -> tuple[ShortcutSpec, ShortcutSpec]:
    """A query and its `-r 序号` form (another role of the member, see /skl角色); nothing else may follow."""
    return ShortcutSpec(pattern, command), ShortcutSpec(pattern + ROLE, f"{command} -r {{1}}")


CUSTOM_SHORTCUTS = (
    ShortcutSpec(r"(森空岛|skl|skd)绑定", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)扫码", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)添加账号", "skland qrcode --add"),
    ShortcutSpec(r"(森空岛|skl|skd)token绑定", "skland bind", fuzzy=True),
    ShortcutSpec(r"(森空岛|skl|skd)解绑", "skland unbind"),
    ShortcutSpec(r"(森空岛|skl|skd)全体角色更新", "skland char update --all"),
    ShortcutSpec(r"(森空岛|skl|skd)角色更新", "skland char update"),
    ShortcutSpec(r"(森空岛|skl|skd)角色", "skland char"),
    ShortcutSpec(r"(森空岛|skl|skd)切换(终末地|zmd|ef)角色", "skland char set ef", fuzzy=True),
    ShortcutSpec(r"(森空岛|skl|skd)切换(明日方舟|方舟|mrfz)角色", "skland char set ark", fuzzy=True),
    ShortcutSpec(r"(终末地|zmd|ef)全体签到详情", "skland efsign status --all"),
    ShortcutSpec(r"(终末地|zmd|ef)全体签到", "skland efsign all"),
    *_query(r"(终末地|zmd|ef)签到详情", "skland efsign status"),
    ShortcutSpec(r"(终末地|zmd|ef)签到", "skland efsign sign --all"),
    *_query(r"(终末地|zmd|ef)抽卡记录更新", "skland efgacha -u"),
    *_query(r"(终末地|zmd|ef)抽卡记录", "skland efgacha"),
    *_query(r"(终末地|zmd|ef)卡片", "skland efcard"),
    *_query(r"(终末地|zmd|ef)查询", "skland efcard"),
    ShortcutSpec(
        r"(终末地|zmd|ef)开盒 all",
        "skland efcard -a",
        fuzzy=True,
        prefix=True,
    ),
    ShortcutSpec(
        r"(终末地|zmd|ef)开盒",
        "skland efcard",
        fuzzy=True,
        prefix=True,
    ),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)全体签到详情", "skland arksign status --all"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)全体签到", "skland arksign all"),
    *_query(r"(明日方舟|方舟|mrfz)签到详情", "skland arksign status"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)签到", "skland arksign sign --all"),
    *_query(r"(明日方舟|方舟|mrfz)抽卡记录", "skland gacha"),
    *_query(r"(明日方舟|方舟|mrfz)卡片", "skland"),
    *_query(r"(明日方舟|方舟|mrfz)查询", "skland"),
    *_query(r"(明日方舟|方舟|mrfz)开盒", "skland"),
)

if any(not spec.prefix for spec in CUSTOM_SHORTCUTS):
    raise RuntimeError("All reviewed Skland shortcuts must require a command prefix")

CONFLICTING_BUILTINS = (
    "森空岛绑定",
    "森空岛解绑",
    "明日方舟签到",
    "方舟抽卡记录",
    "终末地签到",
    "终末地签到详情",
    "终末地全体签到",
    "终末地全体签到详情",
    "终末地抽卡记录",
    r"(ef|zmd)",
    "战争回响",  # 0.7.2 maps it to its own record card; here /战争回响 is the rotation page of endfield_guide
    "森空岛角色",
)

_SHORTCUT_EXTENSION_ID = "builtins.extensions.shortcut:SuperUserShortcutExtension"
driver = get_driver()


# Upstream hints name raw option flags that users of the shortcuts never type; say the shortcut instead.
HINT_REWRITES = {
    "请先使用 -u 参数从接口拉取数据": "请先发送 /zmd抽卡记录更新 拉取数据",
    "临时选角: 查询、签到、状态及抽卡导入命令可追加 -r <序号>": "临时用别的角色：在指令后加 -r 序号，例如 /zmd开盒 -r 2",
    "切换默认角色: sk char set ark <序号> / sk char set ef <序号>": "切换默认角色：/skl切换方舟角色 序号、/skl切换终末地角色 序号",
    "请执行 sk char set ark <序号>": "请发送 /skl切换方舟角色 序号",
    "请执行 sk char set ef <序号>": "请发送 /skl切换终末地角色 序号",
    "请以最新 sk char 卡片为准": "请以 /skl角色 的最新卡片为准",
    "请先执行 sk char update": "请先发送 /skl角色更新",
    "请通过 sk unbind 移除异常项": "请发送 /skl解绑 移除异常的账号",
    "请使用 sk bind -u 更新": "请发送 /skl绑定 重新扫码更新",
    # every member with roles has a default here (skland_health.ensure_defaults), so this means "not bound"
    "目标用户尚未设置明日方舟默认角色": "对方还没有绑定明日方舟角色",
    "目标用户尚未设置终末地默认角色": "对方还没有绑定终末地角色",
    "二维码绑定将由本次命令发起者在角色列表中确认,有效时间约两分钟": "二维码只能由发指令的本人扫码，有效时间约两分钟",
    # upstream decorates its resource-update replies with emoji (written as escapes: no emoji in this repository)
    "\u2705 ": "",
    "\U0001f4e6 ": "",
    "\u274c ": "",
}


@OneBotV11Bot.on_calling_api
async def _rewrite_upstream_hints(bot, api: str, data: dict[str, Any]) -> None:
    if api not in ("send_msg", "send_group_msg", "send_private_msg"):
        return
    message = data.get("message")
    if isinstance(message, str):
        for hint, replacement in HINT_REWRITES.items():
            message = message.replace(hint, replacement)
        data["message"] = message
        return
    segments = [message] if hasattr(message, "type") and hasattr(message, "data") else message or ()  # a lone MessageSegment is not iterable
    for segment in segments:
        text = getattr(segment, "data", {}).get("text") if getattr(segment, "type", "") == "text" else None
        if text:
            for hint, replacement in HINT_REWRITES.items():
                text = text.replace(hint, replacement)
            segment.data["text"] = text


def _shortcut_admin_is_protected() -> bool:
    return any(extension.id == _SHORTCUT_EXTENSION_ID for extension in skland.executor.extensions)


if not _shortcut_admin_is_protected():
    add_global_extension(SuperUserShortcutExtension)
if not _shortcut_admin_is_protected():
    raise RuntimeError("Skland shortcut administration protection could not be attached")


def _delete_if_present(key: str) -> None:
    if key in command_manager.get_shortcut(skland_command):
        skland_command.shortcut(key, delete=True)


def _secure_shortcut_cache() -> None:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CACHE_DIR, 0o700)
    for path in CACHE_DIR.glob("shortcut.db*"):
        if path.is_file():
            os.chmod(path, 0o600)


def _all_serialized_shortcuts_require_prefix(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("prefix") is False:
            return False
        return all(_all_serialized_shortcuts_require_prefix(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_all_serialized_shortcuts_require_prefix(item) for item in value)
    return True


def _persist_shortcut_cache() -> None:
    _secure_shortcut_cache()
    cache_base = CACHE_DIR / "shortcut.db"
    temporary_base = CACHE_DIR / f".shortcut.db.{os.getpid()}.{time_ns()}"
    try:
        command_manager.dump_cache(temporary_base, command=skland_command)
        with shelve.open(temporary_base.as_posix(), flag="r") as database:
            payload = database.get("shortcuts")
            if payload is None or not _all_serialized_shortcuts_require_prefix(payload):
                raise RuntimeError("Shortcut cache contains a command without the required prefix")

        temporary_files = sorted(CACHE_DIR.glob(f"{temporary_base.name}*"))
        if len(temporary_files) != 1:
            raise RuntimeError("Shortcut cache backend is not safely replaceable as one file")

        temporary_file = temporary_files[0]
        suffix = temporary_file.name.removeprefix(temporary_base.name)
        target_file = CACHE_DIR / f"{cache_base.name}{suffix}"
        os.chmod(temporary_file, 0o600)
        os.replace(temporary_file, target_file)
        for stale_file in CACHE_DIR.glob(f"{cache_base.name}*"):
            if stale_file.is_file() and stale_file != target_file:
                stale_file.unlink()
    finally:
        for temporary_file in CACHE_DIR.glob(f"{temporary_base.name}*"):
            if temporary_file.is_file():
                temporary_file.unlink()
    _secure_shortcut_cache()


@driver.on_startup
async def _configure_shortcuts() -> None:
    if not _shortcut_admin_is_protected():
        raise RuntimeError("Skland shortcut administration protection is not active")

    for spec in CUSTOM_SHORTCUTS:
        _delete_if_present(spec.pattern)
    for key in CONFLICTING_BUILTINS:
        _delete_if_present(key)

    for spec in CUSTOM_SHORTCUTS:
        skland.shortcut(
            re.compile(spec.pattern),
            command=spec.command,
            fuzzy=spec.fuzzy,
            prefix=spec.prefix,
            humanized=spec.pattern,
        )

    current = command_manager.get_shortcut(skland_command)
    missing = [spec.pattern for spec in CUSTOM_SHORTCUTS if spec.pattern not in current]
    if missing:
        raise RuntimeError(f"Skland shortcut registration incomplete: {len(missing)} missing")

    _persist_shortcut_cache()
    logger.info(f"Configured {len(CUSTOM_SHORTCUTS)} reviewed Skland shortcuts")
