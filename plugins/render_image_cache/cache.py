"""渲染页面加载的外部游戏图片的磁盘缓存（不导入 NoneBot，可单独测试）。

每次渲染都开一个全新的浏览器上下文，页面下载过的东西下一页用不上：/zmd开盒 卡片或抽卡页面
每次都要重新拉取几十张图标和头像（从本机拉每张 0.3-1.5 秒）。现在图片下载过一次后，页面就从磁盘取；
字节相同，截图也就相同。只缓存游戏素材的域名，从不缓存用户自己的头像。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# 这些域名的图片按游戏内 id（角色、武器、物品、卡池横幅）固定不变；超过 MAX_AGE 后重新拉取
HOSTS = ("web.hycdn.cn", "bbs.hycdn.cn", "endfieldtools.dev", "torappu.prts.wiki", "media.prts.wiki", "data.akedata.wiki")
URL_RE = re.compile(r"^https?://(?:" + "|".join(re.escape(h) for h in HOSTS) + r")/")
MAX_AGE = 7 * 24 * 3600  # 比这更旧的副本会重新下载（上游换了图，一周内就能显示出来）
PRUNE_AGE = 30 * 24 * 3600  # 这么久没用过的副本在启动时删除
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg"}
FETCH_TIMEOUT_MS = 20000


@dataclass
class PageStats:
    hits: int = 0
    misses: int = 0
    passed: int = 0  # 不可缓存（错误状态码、不是图片）：原样交给浏览器
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

    def get(self, url: str, stale_ok: bool = False) -> tuple[bytes, str] | None:
        """磁盘上的副本；超过 MAX_AGE 的只在 stale_ok 时给（重新拉取失败时，旧图总比没有图强）。"""
        p = self.index.get(self.key(url))
        if p is None:
            return None
        try:
            if not stale_ok and time.time() - p.stat().st_mtime > MAX_AGE:
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
        """URL_RE 的 Playwright 路由处理函数：磁盘上有副本就直接给，否则拉取、保存后再给。"""

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
                stale = self.get(url, stale_ok=True)
                if stale is not None:  # 过期的副本：拉不到新的就先用它（不再让浏览器重试，免得超过截图期限）
                    await route.fulfill(status=200, body=stale[0], headers={"content-type": stale[1], "access-control-allow-origin": "*"})
                    return
                await route.fallback()  # 交给浏览器自己去取，和以前一样
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
