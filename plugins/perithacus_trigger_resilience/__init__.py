"""Tolerate slow QQ media downloads in the pErithacus keyword trigger.

pErithacus 1.4.4 downloads every media segment of every incoming message
(default httpx 5 s timeout, no error handling) only to hash it for keyword
matching. Timeouts to the QQ image CDN made the trigger matcher fail hundreds
of times a week. This plugin swaps the trigger's ``dump_msg`` for a version
with a longer timeout that falls back to the un-downloaded dump on network
errors; such a message then simply matches no media keyword.

Only the trigger module's name binding is replaced, so ``/pe add``/``edit``
keep the upstream behaviour. If the upstream version or source hashes differ,
the patch is skipped with a warning instead of blocking startup.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import version
from pathlib import Path

import httpx
from nonebot import logger, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_perithacus")

from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_alconna.uniseg.segment import Media
from nonebot_plugin_perithacus import lib as pe_lib
from nonebot_plugin_perithacus import trigger as pe_trigger

__plugin_meta__ = PluginMetadata(
    name="pErithacus trigger resilience",
    description="Longer timeout and graceful fallback for trigger media downloads.",
    usage="Loaded automatically.",
    type="application",
)

_EXPECTED_VERSION = "1.4.4"
_EXPECTED_TRIGGER_SHA256 = (
    "94039f924950ca199acb089f5b645533bf7e27a6b5c96ac6c053d39012e26b10"
)
_EXPECTED_LIB_SHA256 = (
    "25ecd7d3130fa3c41ce1a5bab836f8f5f941cca3b792e6e933245836e9f0812d"
)
_DOWNLOAD_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_upstream_dump_msg = pe_lib.dump_msg


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _upstream_matches() -> bool:
    installed = version("nonebot-plugin-pErithacus")
    if installed != _EXPECTED_VERSION:
        logger.warning(f"pErithacus {installed} is not {_EXPECTED_VERSION}; trigger patch skipped")
        return False
    if _sha256(pe_trigger.__file__) != _EXPECTED_TRIGGER_SHA256:
        logger.warning("pErithacus trigger.py hash changed; trigger patch skipped")
        return False
    if _sha256(pe_lib.__file__) != _EXPECTED_LIB_SHA256:
        logger.warning("pErithacus lib.py hash changed; trigger patch skipped")
        return False
    if pe_trigger.dump_msg is not _upstream_dump_msg:
        logger.warning("pErithacus trigger.dump_msg already replaced; trigger patch skipped")
        return False
    return True


async def resilient_trigger_dump_msg(msg, *, media_save_dir=False) -> str:
    if not isinstance(msg, UniMessage):
        msg = UniMessage(msg)
    if msg.has(Media):
        try:
            await pe_lib.pe_download(msg, timeout=_DOWNLOAD_TIMEOUT)
        except httpx.HTTPError as e:
            logger.debug(f"pErithacus trigger media download skipped: {type(e).__name__}")
    return pe_lib.pe_uni_dump(msg, media_save_dir=media_save_dir, json=True)


if _upstream_matches():
    pe_trigger.dump_msg = resilient_trigger_dump_msg
    logger.info("pErithacus trigger resilience patch active")
