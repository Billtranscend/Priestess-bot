"""Idempotent project-local Skland shortcuts with protected administration."""

from __future__ import annotations

import os
import re
import shelve
from dataclasses import dataclass
from time import time_ns
from typing import Any

from nonebot import get_driver, logger, require
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


CUSTOM_SHORTCUTS = (
    ShortcutSpec(r"(森空岛|skl|skd)绑定", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)扫码", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)token绑定", "skland bind", fuzzy=True),
    ShortcutSpec(r"(森空岛|skl|skd)解绑", "skland unbind"),
    ShortcutSpec(r"(森空岛|skl|skd)全体角色更新", "skland char update --all"),
    ShortcutSpec(r"(森空岛|skl|skd)角色更新", "skland char update"),
    ShortcutSpec(r"(终末地|zmd|ef)全体签到详情", "skland efsign status --all"),
    ShortcutSpec(r"(终末地|zmd|ef)全体签到", "skland efsign all"),
    ShortcutSpec(r"(终末地|zmd|ef)签到详情", "skland efsign status"),
    ShortcutSpec(r"(终末地|zmd|ef)签到", "skland efsign sign --all"),
    ShortcutSpec(r"(终末地|zmd|ef)抽卡记录更新", "skland efgacha -u"),
    ShortcutSpec(r"(终末地|zmd|ef)抽卡记录", "skland efgacha"),
    ShortcutSpec(r"(终末地|zmd|ef)卡片", "skland efcard"),
    ShortcutSpec(r"(终末地|zmd|ef)查询", "skland efcard"),
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
    ShortcutSpec(r"(明日方舟|方舟|mrfz)签到详情", "skland arksign status"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)签到", "skland arksign sign --all"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)抽卡记录", "skland gacha"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)卡片", "skland"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)查询", "skland"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)开盒", "skland"),
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
)

_SHORTCUT_EXTENSION_ID = "builtins.extensions.shortcut:SuperUserShortcutExtension"
driver = get_driver()


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
    logger.info("Configured %d reviewed Skland shortcuts", len(CUSTOM_SHORTCUTS))
