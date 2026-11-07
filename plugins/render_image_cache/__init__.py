"""Rendered pages take outside game pictures from a disk cache instead of downloading them every time.

htmlrender opens a new browser context for every page, so the icons and avatars a /zmd开盒 card or a
gacha page shows (dozens, from hycdn / endfieldtools.dev) were downloaded again on every command. See
cache.py; the pictures served are the same bytes, so the images do not change. Each page logs how many
pictures came from disk and how long the page took, to compare with before (owner, 2026-11-07).
"""
import contextlib
import time

from nonebot import get_driver, logger, require

require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store  # noqa: E402
import nonebot_plugin_htmlrender  # noqa: E402
from nonebot_plugin_htmlrender import browser, data_source  # noqa: E402

from .cache import URL_RE, ImageCache, PageStats  # noqa: E402

_cache = ImageCache(store.get_plugin_cache_dir() / "pictures")
_original = data_source.get_new_page
if getattr(_original, "_image_cache", False):
    raise RuntimeError("render image cache already installed")


@contextlib.asynccontextmanager
async def cached_new_page(*args, **kwargs):
    async with _original(*args, **kwargs) as page:
        stats = PageStats()
        try:
            await page.route(URL_RE, _cache.handler(stats))
        except Exception as e:  # never stop a render over the cache
            logger.warning(f"render image cache not attached: {type(e).__name__}")
        try:
            yield page
        finally:
            if stats.hits or stats.misses or stats.failed:
                logger.info(f"render page {time.monotonic() - stats.started:.1f}s, pictures: {stats.hits} from disk, "
                            f"{stats.misses} downloaded, {stats.passed} passed, {stats.failed} failed")


cached_new_page._image_cache = True
data_source.get_new_page = cached_new_page
browser.get_new_page = cached_new_page
if getattr(nonebot_plugin_htmlrender, "get_new_page", None) is _original:
    nonebot_plugin_htmlrender.get_new_page = cached_new_page


@get_driver().on_startup
async def _prune() -> None:
    removed = _cache.prune()
    logger.info(f"render image cache ready: {len(_cache.index)} pictures on disk, {removed} old ones removed")
