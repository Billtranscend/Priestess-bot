"""给 Skland 拉取 GitHub 资源文件列表的请求加上重试，并共享结果。

Skland（0.7.1 和 0.7.2 都一样）下载图片资源时，每个资源路径都要拉一次完整的递归 GitHub 目录树
（好几 MB），用的是 httpx 默认的 5 秒超时、没有重试，所以偶尔会报 ``获取文件列表失败: ReadTimeout``。
这条路径只有 ``/skland sync`` 和启动时会走；而启动时的资源下载只在 check_res_update 打开时运行
（默认 False，本机没有设置），所以这个补丁现在主要对 ``/skland sync`` 起作用。
本插件给 ``GameResourceDownloader.fetch_file_list`` 套上更长的超时、最多三次尝试，
以及按 URL 的短时目录树缓存，免得每个路径都把同一份列表重新下载一遍。

如果上游版本或源码哈希对不上，补丁会跳过并给出警告，不会卡住启动。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from httpx import AsyncClient, HTTPError, Timeout
from nonebot import logger, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_skland")

from nonebot_plugin_skland import download as skland_download
from nonebot_plugin_skland.download import File, GameResourceDownloader
from nonebot_plugin_skland.exception import RequestException

__plugin_meta__ = PluginMetadata(
    name="Skland resource list resilience",
    description="Longer timeout, retries and caching for Skland GitHub file lists.",
    usage="Loaded automatically.",
    type="application",
)

_EXPECTED_VERSION = "0.7.4"
_EXPECTED_DOWNLOAD_SHA256 = (
    "d80bdb4a8fe9b26259b93817296d7be7508207b2d13aa8058e7f776590e0be74"
)
_TIMEOUT = Timeout(60.0, connect=15.0)
_ATTEMPTS = 3
_BACKOFF_SECONDS = (3, 10)
_CACHE_TTL_SECONDS = 600

_tree_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_tree_locks: dict[str, asyncio.Lock] = {}


def _upstream_matches() -> bool:
    installed = version("nonebot-plugin-skland")
    if installed != _EXPECTED_VERSION:
        logger.warning(f"Skland {installed} is not {_EXPECTED_VERSION}; resource list patch skipped")
        return False
    digest = hashlib.sha256(Path(skland_download.__file__).read_bytes()).hexdigest()
    if digest != _EXPECTED_DOWNLOAD_SHA256:
        logger.warning("Skland download.py hash changed; resource list patch skipped")
        return False
    return True


async def _fetch_tree(url: str) -> dict[str, Any]:
    lock = _tree_locks.setdefault(url, asyncio.Lock())
    async with lock:
        cached = _tree_cache.get(url)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

        from nonebot_plugin_skland.config import config

        headers = {"Authorization": f"{config.github_token}"} if config.github_token else {}
        last_error: HTTPError | None = None
        for attempt in range(_ATTEMPTS):
            try:
                async with AsyncClient(timeout=_TIMEOUT) as client:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                _tree_cache[url] = (time.monotonic(), data)
                return data
            except HTTPError as e:
                last_error = e
                if attempt < _ATTEMPTS - 1:
                    delay = _BACKOFF_SECONDS[attempt]
                    logger.warning(
                        f"Skland file list attempt {attempt + 1}/{_ATTEMPTS} failed "
                        f"({type(e).__name__}); retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
        raise RequestException(f"获取文件列表失败: {type(last_error).__name__}: {last_error}")


async def fetch_file_list(cls, url: str, dl_url: str, route: str) -> list[File]:
    data = await _fetch_tree(url)
    if "." in route.split("/")[-1]:

        def path_filter(path: str) -> bool:
            return path == route

    else:
        dir_route = route.rstrip("/") + "/"

        def path_filter(path: str) -> bool:
            return path.startswith(dir_route)

    return [
        File(name=item["path"].split("/")[-1], download_url=f"{dl_url}{item['path']}")
        for item in data.get("tree", [])
        if item["type"] == "blob" and path_filter(item["path"])
    ]


if _upstream_matches():
    GameResourceDownloader.fetch_file_list = classmethod(fetch_file_list)
    logger.info("Skland resource list resilience patch active")
