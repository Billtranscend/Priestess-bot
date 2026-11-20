"""终末地卡池改用 AKEData 的数据，不再用 nonebot-plugin-skland 下载的 GitHub 表。

nonebot-plugin-skland 从 GachaPoolTable.json 读取每个卡池的名称、类型、UP 和阵容，这个表从作者的
GitHub 仓库下载。表是手工维护的，会滞后：比如汤汤的重构寻访开放后第二天表里还没有，
而 AKEData 在开池两天前就已经在 1.6.4 数据里有了。卡池进表之前，抽卡页面每次渲染都要去问
游戏的卡池页面，欧非榜也拿不到它的阵容。

endfield_wiki 本来就跟随 AKEData，并在索引里构建 `gacha_pools`。这里的卡池表就是把它叠加在磁盘上
保存的表之上（同一个文件，所以游戏从自己表里去掉的卡池仍然认识）：名称、类型、UP 和阵容以 AKEData
为准，只有文件里才有的东西（横幅图、额外字段）保留。横幅图不在游戏的表里：没有图的卡池会去游戏的
公开卡池页面查，最多每 ART_RETRY 秒查一次（页面不认识这个卡池、也就是还没开放时为 UNKNOWN_RETRY）。
页面正常回答却没有横幅的（例如武器池只有 up6_banner 那张武器图）连同 up6_banner 存进表里，并记下当时
wiki 索引的版本（CHECKED）：重启后也记得，资料库出了新版本才再问一次，那时真有新图也能补上。
上游的加载函数被替换，所以平时不再请求 GitHub 仓库；「资源更新」指令和每日更新改为执行这里的刷新。
只有全新部署第一次启动时（资料库索引还没建好、磁盘上也没有卡池表）仍用上游的加载：否则卡池表是空的，
连带终末地图标资源也下载失败；之后的刷新再把 AKEData 的数据叠上去。

启动时刷新；之后每 REFRESH_MINUTES 分钟，在 wiki 索引有变化或缺图时刷新。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import httpx
from nonebot import logger, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_skland")

from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_skland.data_source import ef_gacha_pool_data
from nonebot_plugin_skland.exception import RequestException

from plugins import endfield_wiki
from plugins.shared import write_json
from plugins.skland_pools import POOL_CONTENT_URL

__plugin_meta__ = PluginMetadata(
    name="Skland Endfield pools",
    description="终末地卡池表改由 AKEData 解包数据生成，新卡池在数据解包后即可识别。",
    usage="无指令，自动生效。",
    type="application",
)

ART_FIELDS = ("up6_image", "rotate_image")
BANNER = "up6_banner"  # 武器自身的图（透明底），不是横幅
CHECKED = "art_checked"  # 卡池页正常回答但确实没有横幅时，当时 wiki 索引的版本
ART_RETRY = 3600
UNKNOWN_RETRY = 6 * 3600
POOL_UNKNOWN = 40402  # 卡池页面对它不显示的卡池给出的回答
REFRESH_MINUTES = 10

_lock = asyncio.Lock()
_next_ask: dict[str, float] = {}  # 卡池 id -> 什么时候可以再去要它的图
_seen: dict = {"index": None, "version": ""}  # 上次构建卡池表时 wiki 索引的 mtime 和版本


def _read(path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _index_mtime() -> float | None:
    try:
        return endfield_wiki.INDEX_FILE.stat().st_mtime
    except OSError:
        return None


def _without_art(pools: dict, version: str) -> list[str]:
    """该去卡池页要图的卡池：没有横幅、这个索引版本里还没确认过、也没在等下一次重试。"""
    now = time.time()
    return [
        p for p, entry in sorted(pools.items())
        if not any(entry.get(f) for f in ART_FIELDS) and entry.get(CHECKED) != version and _next_ask.get(p, 0) <= now
    ]


async def _add_art(pools: dict, version: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        for pool_id in _without_art(pools, version):
            _next_ask[pool_id] = time.time() + ART_RETRY
            try:
                response = await client.get(POOL_CONTENT_URL, params={"lang": "zh-cn", "pool_id": pool_id, "server_id": "1"})
                answer = response.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.warning(f"Endfield pool art not fetched: {type(e).__name__}")
                continue
            if not isinstance(answer, dict):  # 不是预期的回答：这个池的图下次再取
                continue
            if answer.get("code") == POOL_UNKNOWN:
                _next_ask[pool_id] = time.time() + UNKNOWN_RETRY
            page = (answer.get("data") or {}).get("pool") or {}
            found = {f: page[f] for f in (*ART_FIELDS, BANNER) if isinstance(page.get(f), str) and page[f].startswith("https://")}
            pools[pool_id].update(found)
            if not answer.get("code") and page and not any(f in found for f in ART_FIELDS):
                pools[pool_id][CHECKED] = version  # 卡池页认识它，就是没有横幅：同一版本内不必每小时、每次重启再问


def _write(path, pools: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, pools, ensure_ascii=False, separators=(",", ":"))


async def refresh() -> bool:
    """用 wiki 索引和文件重建卡池表；文件有变化时返回 True。"""
    async with _lock:
        path = ef_gacha_pool_data._file_path
        mtime = _index_mtime()
        index = endfield_wiki._index() or {}  # 和资料库查询共用同一份按修改时间缓存的索引
        version = index.get("version", "")
        built = index.get("gacha_pools") or {}
        old = _read(path)
        pools = {pool_id: dict(entry) for pool_id, entry in old.items() if isinstance(entry, dict)}
        for pool_id, entry in built.items():
            pools[pool_id] = {**pools.get(pool_id, {}), **entry}
        for pool_id, entry in pools.items():  # 没有名字的一行会让上游拒绝整张表：先用 id 代替
            if not entry.get("pool_name"):
                entry["pool_name"] = pool_id
        await _add_art(pools, version)
        parsed = ef_gacha_pool_data._parse(pools)  # 表坏了会抛 ValueError：保留已加载的那份
        changed = pools != old
        if changed:
            _write(path, pools)
            new = sorted(set(pools) - set(old))
            logger.info(f"Endfield pool table: {len(pools)} pools" + (f", new {', '.join(new)}" if new else ""))
        ef_gacha_pool_data.pool_table = parsed
        _seen.update(index=mtime, version=version)
        return changed


_upstream_load = ef_gacha_pool_data.load


async def _load(*, force: bool = False, client=None) -> bool:
    """替代 EfGachaPoolTableData.load（启动、每日更新、资源更新时调用）。"""
    if not (endfield_wiki._index() or {}).get("gacha_pools") and not _read(ef_gacha_pool_data._file_path):
        return await _upstream_load(force=force, client=client)
    try:
        return await refresh()
    except Exception as e:  # 上游调用方只接 RequestException，别的异常会让机器人启动失败
        if not ef_gacha_pool_data.pool_table:  # 还什么都没加载（刚启动）：先用磁盘上的那份，总比空表强
            with contextlib.suppress(Exception):
                ef_gacha_pool_data.pool_table = ef_gacha_pool_data._parse(_read(ef_gacha_pool_data._file_path))
        raise RequestException(f"更新终末地卡池数据失败: {type(e).__name__}") from e


ef_gacha_pool_data.load = _load


@scheduler.scheduled_job("interval", minutes=REFRESH_MINUTES, id="skland_ef_pools_refresh", max_instances=1, coalesce=True)
async def _refresh_when_needed() -> None:
    if _index_mtime() == _seen["index"] and not _without_art(_read(ef_gacha_pool_data._file_path), _seen["version"]):
        return
    try:
        await refresh()
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"Endfield pool table not refreshed: {type(e).__name__}")
