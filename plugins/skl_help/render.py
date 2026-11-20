"""本部署的 /skl帮助 图片，由 help.html 生成。

help.html 用注释把只适用于部分部署的内容括起来：

    <!--official-->...<!--/official-->    只在有官方机器人时（设置了 QQ_BOTS）
    <!--napcat-->...<!--/napcat-->        只在没有官方机器人时
    <!--migration-->...<!--/migration--> 只在群友从 NapCat 账号迁移过来期间
                                          （HELP_MIGRATION_NOTE=true；它位于 official 部分之内）
    <!--bison-->...<!--/bison-->          只在机器人旁边运行着 nonebot-bison 时（HELP_BISON=true）：
                                          它的订阅指令不属于本项目

机器人的名字写作 BOT_NAME（HELP_BOT_NAME，默认“机器人”）。只要生成的页面与上次渲染时的不同，
就会重新渲染图片，所以改 help.html 或换机器人都不需要手动操作。

这里不导入 NoneBot：deploy/render_skl_help.py 会单独加载这个文件。
"""

from __future__ import annotations

import hashlib
import html as markup
import os
import re
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).with_name("help.html")
OUT_DIR = ROOT / "data" / "skl_help"
IMAGE = OUT_DIR / "help.webp"
STAMP = OUT_DIR / "help.sha256"
BROWSERS = ROOT / "data" / "nonebot_plugin_htmlrender"
VERSION = "1"  # 渲染方式改变时加一，让每个部署都重新渲染
DEFAULT_NAME = "机器人"

_rendering = threading.Lock()


def _flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


def settings(config: Any) -> dict[str, Any]:
    """决定页面内容的设置，从 NoneBot 配置（.env）读取。"""
    return {
        "official": bool(getattr(config, "qq_bots", None)),
        "migration": _flag(getattr(config, "help_migration_note", False)),
        "bison": _flag(getattr(config, "help_bison", False)),
        "name": str(getattr(config, "help_bot_name", "") or "").strip() or DEFAULT_NAME,
    }


def page(*, official: bool, migration: bool, name: str, bison: bool = False) -> str:
    """只保留本部署适用部分的 help.html，并填入机器人名字和主题字体。"""
    from plugins import ef_theme

    html = SOURCE.read_text("utf-8")
    shown = {"official": official, "napcat": not official, "migration": official and migration, "bison": bison}
    for kind, keep in shown.items():
        html = re.sub(rf"<!--{kind}-->(.*?)<!--/{kind}-->", r"\1" if keep else "", html, flags=re.S)
    # 字体 URL 是本检出目录下的 file:// 路径，所以在这里填入，而不是写死在文件里
    return html.replace("BOT_NAME", markup.escape(name)).replace("/*EF_FONTS*/", ef_theme.fonts_css())


def fingerprint(html: str) -> str:
    """页面、出图设置（缩放、画质）和字体文件一起算：换了任何一样，启动时都会重新生成帮助图。"""
    from plugins import ef_theme

    fonts = sorted((f.name, f.stat().st_size, int(f.stat().st_mtime)) for f in ef_theme.FONT_DIR.glob("*") if f.is_file())
    return hashlib.sha256(f"{VERSION}\n{ef_theme.PAGE_SCALE}\n{ef_theme.PAGE_QUALITY}\n{fonts}\n{html}".encode()).hexdigest()


def is_current(html: str) -> bool:
    try:
        return IMAGE.is_file() and STAMP.read_text("utf-8").strip() == fingerprint(html)
    except OSError:
        return False


def render(html: str, output: Path | None = None) -> Path:
    """给页面截图并存为 WebP；会阻塞，在异步代码里要放到线程中运行。

    不给输出路径时，生成的就是 /skl帮助 发送的那张图，并记下它的指纹。
    """
    if BROWSERS.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS))
    from playwright.sync_api import sync_playwright

    from plugins import ef_theme

    target = output or IMAGE
    with _rendering:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        rendered = OUT_DIR / f".help.{os.getpid()}.rendered.html"  # 机器人和 deploy/render_skl_help.py 同时渲染也不会互相删掉
        rendered.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        rendered.write_text(html, "utf-8")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    view = browser.new_page(viewport={"width": 1080, "height": 800}, device_scale_factor=ef_theme.PAGE_SCALE)
                    view.goto(rendered.as_uri(), wait_until="load")
                    screenshot = view.screenshot(type="png", full_page=True)
                finally:
                    browser.close()
        finally:
            rendered.unlink(missing_ok=True)
        partial = target.with_name(f".{target.name}.{os.getpid()}.part")
        partial.write_bytes(ef_theme.to_webp(screenshot, ef_theme.PAGE_QUALITY))
        os.chmod(partial, 0o600)
        os.replace(partial, target)
        if output is None:
            STAMP.write_text(fingerprint(html) + "\n", "utf-8")
            os.chmod(STAMP, 0o600)
    return target
