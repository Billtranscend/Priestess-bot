"""Local copies of the remote pictures in upstream's Arknights pages (/方舟干员, the /sk card).

A screenshot waits for every image of the page. Upstream points several of them at outside hosts: the
rarity decorations and module icons at the PRTS wiki, skin portraits and skill icons that are not cached
yet at Hypergryph's CDN. When one of those hosts was slow, every page of /方舟干员 ran into the 30 s limit
(2026-10-26 and 10-27), queue or not.

The URL helpers are replaced in every upstream module that imported them, and now only ever return local
files: what is cached is used; what is not yet cached is fetched in the background into the same cache
upstream uses, and the page is drawn without waiting for it (a skin portrait falls back to the
operator's default portrait, a module or skill icon is left out once). The rarity decorations and the
module icons of the operator catalog are fetched at startup.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import quote

import httpx
from nonebot import get_driver, logger, require

require("nonebot_plugin_skland")

from nonebot_plugin_skland import filters
from nonebot_plugin_skland.config import CACHE_DIR
from nonebot_plugin_skland.schemas.arknights import card as card_module
from nonebot_plugin_skland.schemas.arknights import operators
from nonebot_plugin_skland.schemas.arknights.models import assist_chars, chars

ASSETS = CACHE_DIR / "ark_assets"
UNIEQUIP_URL = "https://torappu.prts.wiki/assets/uniequip_direction/{}.png"  # as upstream's ark_uniequip_icon_url
PORTRAIT_URL = "https://web.hycdn.cn/arknights/game/assets/char_skin/portrait/{}.png"  # as ark_skin_portrait_url
SKILL_URL = "https://web.hycdn.cn/arknights/game/assets/char_skill/{}.png"  # as ark_skill_icon_url
TIMEOUT = 20
MAX_BYTES = 10 * 1024 * 1024
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}

_ORIGINAL = {
    name: getattr(filters, name)
    for name in ("ark_skin_portrait_url", "ark_skill_icon_url", "ark_uniequip_icon_url")
}
_DECO_SOURCES = {**{("lh", r): u for r, u in filters.ARK_ROSTER_LH_URLS.items()},
                 **{("light", r): u for r, u in filters.ARK_ROSTER_LIGHT_URLS.items()}}
_pending: set[Path] = set()
_tasks: set[asyncio.Task] = set()


def _uri(path: Path) -> str:
    return path.as_uri() if path.is_file() else ""


async def _download(url: str, path: Path) -> bool:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return False
    data = response.content
    if response.status_code != 200 or not data.startswith(b"\x89PNG") or len(data) > MAX_BYTES:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


def _later(url: str, path: Path) -> None:
    """Fetch into the cache in the background, once per file and process; nothing waits for it."""
    if path in _pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _pending.add(path)
    task = loop.create_task(_download(url, path))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _portrait_id(skin_id: str) -> str:
    return next((skin_id.replace(symbol, "_", 1) for symbol in ("@", "#") if symbol in skin_id), skin_id)


def ark_skin_portrait_url(skin_id: str) -> str:
    path = CACHE_DIR / "portrait" / f"{_portrait_id(skin_id)}.png"
    if path.is_file():
        return path.as_uri()
    _later(PORTRAIT_URL.format(quote(skin_id, safe="")), path)
    char_id = skin_id.split("@", 1)[0].split("#", 1)[0]
    return _uri(CACHE_DIR / "portrait" / f"{char_id}_1.png") or _uri(CACHE_DIR / "portrait" / f"{char_id}_2.png")


def ark_skill_icon_url(skill_id: str) -> str:
    path = CACHE_DIR / "skill" / f"skill_icon_{skill_id}.png"
    if path.is_file():
        return path.as_uri()
    _later(SKILL_URL.format(quote(skill_id, safe="")), path)
    return ""


def _uniequip_path(type_icon: str) -> Path:
    return ASSETS / "uniequip" / f"{type_icon.lower()}.png"


def ark_uniequip_icon_url(type_icon: str | None) -> str:
    if not type_icon:
        return ""
    path = _uniequip_path(type_icon)
    if path.is_file():
        return path.as_uri()
    _later(UNIEQUIP_URL.format(quote(type_icon.lower(), safe="")), path)
    return ""


def _deco_path(url: str) -> Path:
    return ASSETS / "deco" / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.png"


def _point_deco_at_cache() -> None:
    """The decoration tables hold local files only: blank until the startup fetch has them."""
    for (kind, rarity), url in _DECO_SOURCES.items():
        table = filters.ARK_ROSTER_LH_URLS if kind == "lh" else filters.ARK_ROSTER_LIGHT_URLS
        table[rarity] = _uri(_deco_path(url))


REPLACEMENTS = {
    "ark_skin_portrait_url": ark_skin_portrait_url,
    "ark_skill_icon_url": ark_skill_icon_url,
    "ark_uniequip_icon_url": ark_uniequip_icon_url,
}
MODULES = (filters, operators, card_module, assist_chars, chars)
for _module in MODULES:
    for _name, _replacement in REPLACEMENTS.items():
        if getattr(_module, _name, None) is _ORIGINAL[_name]:
            setattr(_module, _name, _replacement)
_point_deco_at_cache()


@get_driver().on_startup
async def _verify_and_prefetch() -> None:
    for module in MODULES:
        for name, replacement in REPLACEMENTS.items():
            if hasattr(module, name) and getattr(module, name) is not replacement:
                raise RuntimeError(f"Skland Arknights asset hook missing: {module.__name__}.{name}")

    async def prefetch() -> None:
        jobs = {_deco_path(url): url for url in set(_DECO_SOURCES.values()) if not _deco_path(url).is_file()}
        with_catalog = 0
        try:
            from nonebot_plugin_skland.data_source import gacha_table_data

            catalog = gacha_table_data.operator_catalog
            if not catalog.entries:
                catalog = gacha_table_data.load_operator_catalog()
            types = {m.type_icon.lower() for e in catalog.entries for m in e.modules if m.type_icon and m.type_icon.lower() != "original"}
            for t in types:
                if not _uniequip_path(t).is_file():
                    jobs[_uniequip_path(t)] = UNIEQUIP_URL.format(quote(t, safe=""))
            with_catalog = len(types)
        except Exception as e:  # the icons are then fetched one by one as pages ask for them
            logger.warning(f"Skland Arknights assets: catalog unavailable ({type(e).__name__})")
        gate = asyncio.Semaphore(4)

        async def one(path: Path, url: str) -> bool:
            async with gate:
                return await _download(url, path)

        got = sum(await asyncio.gather(*(one(p, u) for p, u in jobs.items()))) if jobs else 0
        _point_deco_at_cache()
        logger.info(f"Skland Arknights assets: {got}/{len(jobs)} fetched, {with_catalog} module types known, decorations local")

    task = asyncio.get_running_loop().create_task(prefetch())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
