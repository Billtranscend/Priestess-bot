"""Disk cache for the outside game pictures that rendered pages load (no NoneBot imports, testable alone).

Every render opens a fresh browser context, so nothing a page downloaded is reused by the next one: a
/zmd开盒 card or a gacha page fetched its dozens of icons and avatars again each time (0.3-1.5 s each from
this host). Pages now get those pictures from disk once they were downloaded; the bytes are the same, so
the screenshot is too. Only game-asset hosts are cached, never people's own avatars.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# hosts whose pictures are fixed per game id (character, weapon, item, banner); a refetch after MAX_AGE
HOSTS = ("web.hycdn.cn", "bbs.hycdn.cn", "endfieldtools.dev", "torappu.prts.wiki", "media.prts.wiki")
URL_RE = re.compile(r"^https?://(?:" + "|".join(re.escape(h) for h in HOSTS) + r")/")
MAX_AGE = 7 * 24 * 3600  # older copies are downloaded again (a picture changed upstream shows within a week)
PRUNE_AGE = 30 * 24 * 3600  # copies not used for this long are deleted at startup
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg"}
FETCH_TIMEOUT_MS = 20000


@dataclass
class PageStats:
    hits: int = 0
    misses: int = 0
    passed: int = 0  # not cacheable (error status, not a picture): handed over as fetched
    failed: int = 0
    started: float = field(default_factory=time.monotonic)


class ImageCache:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, Path] = {}
        for p in folder.iterdir():
            if p.suffix in TYPES.values():
                self.index[p.stem] = p

    def prune(self) -> int:
        now, removed = time.time(), 0
        for key, p in list(self.index.items()):
            try:
                if now - p.stat().st_atime > PRUNE_AGE and now - p.stat().st_mtime > PRUNE_AGE:
                    p.unlink()
                    del self.index[key]
                    removed += 1
            except OSError:
                self.index.pop(key, None)
        for p in self.folder.glob("*.tmp"):
            p.unlink(missing_ok=True)
        return removed

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url: str) -> tuple[bytes, str] | None:
        p = self.index.get(self.key(url))
        if p is None:
            return None
        try:
            if time.time() - p.stat().st_mtime > MAX_AGE:
                return None
            ctype = next(t for t, ext in TYPES.items() if ext == p.suffix)
            return p.read_bytes(), ctype
        except (OSError, StopIteration):
            return None

    def put(self, url: str, body: bytes, ctype: str) -> None:
        key, ext = self.key(url), TYPES[ctype]
        target = self.folder / f"{key}{ext}"
        tmp = self.folder / f"{key}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        tmp.write_bytes(body)
        os.replace(tmp, target)
        old = self.index.get(key)
        if old is not None and old != target:
            old.unlink(missing_ok=True)
        self.index[key] = target

    def handler(self, stats: PageStats):
        """A Playwright route handler for URL_RE: serve the copy on disk, else fetch, keep and serve."""

        async def serve(route) -> None:
            request = route.request
            if request.method != "GET":
                await route.fallback()
                return
            url = request.url
            cached = self.get(url)
            if cached is not None:
                stats.hits += 1
                await route.fulfill(status=200, body=cached[0], headers={"content-type": cached[1], "access-control-allow-origin": "*"})
                return
            try:
                response = await route.fetch(timeout=FETCH_TIMEOUT_MS)
            except Exception:
                stats.failed += 1
                await route.fallback()  # the browser tries itself, as before
                return
            ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if response.status == 200 and ctype in TYPES:
                try:
                    body = await response.body()
                    await asyncio.to_thread(self.put, url, body, ctype)
                except Exception:
                    stats.failed += 1
                else:
                    stats.misses += 1
            else:
                stats.passed += 1
            await route.fulfill(response=response)

        return serve
