"""Fail-closed compatibility guard for Skland 0.7.1 weapon gacha data."""

from __future__ import annotations

import hashlib
from importlib.metadata import version
from pathlib import Path
from typing import Any

from nonebot import get_driver, logger, require
from nonebot.compat import model_validator
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_skland")

from nonebot_plugin_skland.api import request as request_module
from nonebot_plugin_skland.schemas.endfield.gacha.base import (
    EfWeaponGachaResponse as UpstreamEfWeaponGachaResponse,
)

__plugin_meta__ = PluginMetadata(
    name="Skland Endfield gacha compatibility guard",
    description="Filters explicit non-draw weapon-history metadata on Skland 0.7.1.",
    usage="Loaded automatically.",
    type="application",
)

_EXPECTED_VERSION = "0.7.1"
_EXPECTED_MODEL_SHA256 = (
    "446f030d030c661e4692fc5fdf20e02d3ad0eb371ca78a575d64f98f6bf7a0a6"
)
_EXPECTED_REQUEST_SHA256 = (
    "a041670ed1bfc245cffc094861ba738e0572e4fed06e1fe82dc16b6a93f1d5fb"
)
_REQUIRED_WEAPON_FIELDS = frozenset(
    {
        "poolId",
        "poolName",
        "weaponId",
        "weaponName",
        "weaponType",
        "rarity",
        "isNew",
        "gachaTs",
        "seqId",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_upstream() -> None:
    installed_version = version("nonebot-plugin-skland")
    if installed_version != _EXPECTED_VERSION:
        raise RuntimeError(
            "Skland Endfield gacha compatibility guard only supports "
            f"{_EXPECTED_VERSION}; installed version is {installed_version}"
        )

    package_root = Path(request_module.__file__).parents[2]
    model_path = package_root / Path(
        UpstreamEfWeaponGachaResponse.__module__.replace(".", "/") + ".py"
    )
    request_path = Path(request_module.__file__)
    if _sha256(model_path) != _EXPECTED_MODEL_SHA256:
        raise RuntimeError("Skland weapon gacha model source hash is not approved")
    if _sha256(request_path) != _EXPECTED_REQUEST_SHA256:
        raise RuntimeError("Skland request source hash is not approved")


class EfWeaponGachaResponseCompat(UpstreamEfWeaponGachaResponse):
    """Ignore metadata entries while preserving fail-closed draw validation."""

    @model_validator(mode="before")
    @classmethod
    def filter_explicit_non_draw_metadata(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        records = values.get("list")
        if not isinstance(records, list):
            return values

        filtered: list[Any] = []
        for record in records:
            if not isinstance(record, dict):
                filtered.append(record)
                continue
            missing_fields = _REQUIRED_WEAPON_FIELDS.difference(record)
            if missing_fields and record.get("kind") not in (None, "draw"):
                continue
            filtered.append(record)
        return {**values, "list": filtered}


_verify_upstream()
if request_module.EfWeaponGachaResponse is not UpstreamEfWeaponGachaResponse:
    raise RuntimeError("Skland weapon gacha response parser was already replaced")
request_module.EfWeaponGachaResponse = EfWeaponGachaResponseCompat

driver = get_driver()


@driver.on_startup
async def _verify_compatibility_guard() -> None:
    _verify_upstream()
    if request_module.EfWeaponGachaResponse is not EfWeaponGachaResponseCompat:
        raise RuntimeError("Skland weapon gacha compatibility guard is not active")
    logger.info("Skland Endfield weapon gacha compatibility guard is active")
