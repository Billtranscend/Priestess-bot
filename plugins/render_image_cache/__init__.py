"""渲染页面用到的外部游戏图片改从磁盘缓存读取，不再每次都下载。

htmlrender 每渲染一页都开一个新的浏览器上下文，所以 /zmd开盒 卡片或抽卡页面上的图标和头像
（几十张，来自 hycdn / endfieldtools.dev）每次指令都要重新下载。详见 cache.py；提供的图片字节完全相同，
所以出图不会变。每页都会记录有多少张图片来自磁盘、整页花了多久，以便和以前对比。
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
        except Exception as e:  # 缓存出问题也绝不能让渲染中断
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
