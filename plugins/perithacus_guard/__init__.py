"""Fail-closed SUPERUSER protection for pErithacus management commands."""

from nonebot import get_driver, require
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_perithacus")

from nonebot_plugin_perithacus.command import pe

__plugin_meta__ = PluginMetadata(
    name="pErithacus management guard",
    description="Restricts every pErithacus management subcommand to SUPERUSER.",
    usage="Loaded automatically.",
    type="application",
)

driver = get_driver()


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
