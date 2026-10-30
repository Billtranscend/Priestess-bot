"""Endfield gacha pools from AKEData instead of the GitHub table nonebot-plugin-skland downloads.

nonebot-plugin-skland takes each pool's name, type, UP and line-up from GachaPoolTable.json, which
it downloads from its author's GitHub repository. That table is kept by hand and lags: the
重构寻访 of 汤汤 opened on 2026-10-29 12:00 and was still missing the next day, while AKEData had it
in its 1.6.4 data two days before the pool opened. Until a pool is in the table the gacha page asks
the game's pool page on every render, and the 欧非榜 has no line-up for it.

endfield_wiki already follows AKEData and builds `gacha_pools` into its index. The pool table here is
that, laid over the table kept on disk (the same file, so a pool the game drops from its own tables
stays known): AKEData's name, type, UP and line-up win, and whatever only the file has (banner art,
extra fields) is kept. Banner art is not in the game tables: a pool without it is looked up on the
game's public pool page, at most every ART_RETRY seconds (UNKNOWN_RETRY when the page does not know
the pool, i.e. it is not open). The upstream loader is replaced, so the GitHub repository is not
asked any more; the 「资源更新」 command and the daily update run this refresh instead.

Refreshed at startup and, every REFRESH_MINUTES, when the wiki index has changed or art is missing.
"""

from __future__ import annotations

import asyncio
import json
import os
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

__plugin_meta__ = PluginMetadata(
    name="Skland Endfield pools",
    description="终末地卡池表改由 AKEData 解包数据生成，新卡池在数据解包后即可识别。",
    usage="无指令，自动生效。",
    type="application",
)

POOL_CONTENT_URL = "https://ef-webview.hypergryph.com/api/content"  # the game's own pool page; public
ART_FIELDS = ("up6_image", "rotate_image")
ART_RETRY = 3600
UNKNOWN_RETRY = 6 * 3600
POOL_UNKNOWN = 40402  # the pool page's answer for a pool it does not show
REFRESH_MINUTES = 10

_lock = asyncio.Lock()
_next_ask: dict[str, float] = {}  # pool id -> when its art may be asked for again
_seen = {"index": None}  # mtime of the wiki index the table was last built from


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


def _without_art(pools: dict) -> list[str]:
    now = time.time()
    return [p for p, entry in sorted(pools.items()) if not any(entry.get(f) for f in ART_FIELDS) and _next_ask.get(p, 0) <= now]


async def _add_art(pools: dict) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        for pool_id in _without_art(pools):
            _next_ask[pool_id] = time.time() + ART_RETRY
            try:
                response = await client.get(POOL_CONTENT_URL, params={"lang": "zh-cn", "pool_id": pool_id, "server_id": "1"})
                answer = response.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.warning(f"Endfield pool art not fetched: {type(e).__name__}")
                continue
            if answer.get("code") == POOL_UNKNOWN:
                _next_ask[pool_id] = time.time() + UNKNOWN_RETRY
            page = (answer.get("data") or {}).get("pool") or {}
            pools[pool_id].update({f: page[f] for f in ART_FIELDS if isinstance(page.get(f), str) and page[f].startswith("https://")})


def _write(path, pools: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(pools, ensure_ascii=False, separators=(",", ":")), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


async def refresh() -> bool:
    """Rebuild the pool table from the wiki index and the file; True when the file changed."""
    async with _lock:
        path = ef_gacha_pool_data._file_path
        mtime = _index_mtime()
        built = _read(endfield_wiki.INDEX_FILE).get("gacha_pools") or {}
        old = _read(path)
        pools = {pool_id: dict(entry) for pool_id, entry in old.items() if isinstance(entry, dict)}
        for pool_id, entry in built.items():
            pools[pool_id] = {**pools.get(pool_id, {}), **entry}
        await _add_art(pools)
        parsed = ef_gacha_pool_data._parse(pools)  # ValueError for a broken table: the loaded one stays
        changed = pools != old
        if changed:
            _write(path, pools)
            new = sorted(set(pools) - set(old))
            logger.info(f"Endfield pool table: {len(pools)} pools" + (f", new {', '.join(new)}" if new else ""))
        ef_gacha_pool_data.pool_table = parsed
        _seen["index"] = mtime
        return changed


async def _load(*, force: bool = False, client=None) -> bool:
    """Stands in for EfGachaPoolTableData.load (startup, daily update, 资源更新)."""
    try:
        return await refresh()
    except (OSError, ValueError, TypeError) as e:
        raise RequestException(f"更新终末地卡池数据失败: {type(e).__name__}") from e


ef_gacha_pool_data.load = _load


@scheduler.scheduled_job("interval", minutes=REFRESH_MINUTES, id="skland_ef_pools_refresh", max_instances=1, coalesce=True)
async def _refresh_when_needed() -> None:
    if _index_mtime() == _seen["index"] and not _without_art(_read(ef_gacha_pool_data._file_path)):
        return
    try:
        await refresh()
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"Endfield pool table not refreshed: {type(e).__name__}")
