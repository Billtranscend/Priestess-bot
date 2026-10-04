"""Bilibili video search for stage guides (anonymous, cached, rate limited).

The plain search API answers HTTP 412 to cookieless requests from this server,
but works with the guest buvid3/b_nut cookies the homepage hands out. Results are
cached per query; on failure the last cached result is returned.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path

import httpx
from nonebot import logger

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
CACHE_TTL = 8 * 3600
MIN_INTERVAL = 3.0
MAX_RESULTS = 5


@dataclass
class Video:
    bvid: str
    title: str
    author: str
    play: int
    like: int
    pubdate: int
    duration: str


class BiliSearch:
    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file
        self.cookies: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    def _cache(self) -> dict:
        try:
            return json.loads(self.cache_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_cache(self, cache: dict) -> None:
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.cache_file)

    async def _throttle(self) -> None:
        wait = self.last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_request = time.monotonic()

    async def _refresh_cookies(self, client: httpx.AsyncClient) -> None:
        await self._throttle()
        response = await client.get("https://www.bilibili.com/", headers={"User-Agent": UA})
        self.cookies = {k: v for k, v in response.cookies.items() if k in ("buvid3", "b_nut")}

    async def _search(self, client: httpx.AsyncClient, keyword: str, order: str) -> list[dict]:
        for attempt in range(2):
            if not self.cookies:
                await self._refresh_cookies(client)
            await self._throttle()
            response = await client.get(
                SEARCH_URL,
                params={"search_type": "video", "keyword": keyword, "order": order, "page": 1},
                headers={"User-Agent": UA, "Referer": "https://search.bilibili.com/"},
                cookies=self.cookies,
            )
            data = response.json() if response.status_code == 200 else {}
            if data.get("code") == 0 and "result" in data.get("data", {}):
                return data["data"]["result"] or []
            self.cookies = {}  # risk control or expired guest cookie: fetch a fresh one and retry once
            logger.warning(f"Bilibili search refused (HTTP {response.status_code}, code {data.get('code')}), attempt {attempt + 1}")
        raise RuntimeError("bilibili search unavailable")

    async def videos(self, stage_name: str, boost_words: tuple[str, ...] = ()) -> tuple[list[Video], bool]:
        """Return (videos, from_cache_after_failure)."""
        key = f"{stage_name}|{','.join(boost_words)}"
        async with self.lock:
            cache = self._cache()
            hit = cache.get(key)
            if hit and time.time() - hit["ts"] < CACHE_TTL:
                return [Video(**v) for v in hit["items"]], False
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    raw = []
                    for order in ("pubdate", "click"):
                        raw += await self._search(client, f"终末地 {stage_name}", order)
            except Exception as e:
                logger.warning(f"Bilibili search failed: {type(e).__name__}")
                return ([Video(**v) for v in hit["items"]] if hit else []), True
            items = _rank(raw, stage_name, boost_words)
            cache[key] = {"ts": time.time(), "items": [asdict(v) for v in items]}
            self._save_cache(cache)
            return items, False


def _clean(title: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", title)).strip()


def _rank(raw: list[dict], stage_name: str, boost_words: tuple[str, ...]) -> list[Video]:
    seen, scored = set(), []
    now = time.time()
    for item in raw:
        title = _clean(item.get("title", ""))
        bvid = item.get("bvid", "")
        if not bvid or bvid in seen or stage_name not in title:
            continue
        seen.add(bvid)
        play = int(item.get("play") or 0)
        age_days = max(0.0, (now - int(item.get("pubdate") or now)) / 86400)
        score = math.log10(play + 10) / (1 + age_days / 14)
        if any(word in title for word in boost_words):
            score *= 1.5
        scored.append(
            (
                score,
                Video(
                    bvid=bvid,
                    title=title,
                    author=item.get("author", ""),
                    play=play,
                    like=int(item.get("like") or 0),
                    pubdate=int(item.get("pubdate") or 0),
                    duration=str(item.get("duration", "")),
                ),
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [video for _, video in scored[:MAX_RESULTS]]
