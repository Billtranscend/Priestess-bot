"""Endfield-themed pages for nonebot_plugin_skland's /zmd开盒 and /zmd抽卡记录.

site-packages stays untouched.

/zmd开盒: render.template_to_pic (already wrapped by skland_compact_images) is wrapped once more
and, for the card template only, `template_path` points at ./templates. The rendered page then
lives in ./templates, so relative "../images/..." paths would break: the template sets a <base>
back to the package's templates folder. The override is pinned to the sha256 of the upstream
files it was derived from; after an upstream update the original template is used again.

/zmd抽卡记录: see gacha.py. Since 0.7.2 that page is this project's own, handler included.

/skl角色: upstream prints its raw commands ("sk char set ark") on the picture. A reworded copy with
this bot's command names is generated into the cache at import; if one of the phrases is no longer
found exactly once, the upstream template is used as it is.
"""
import functools
import hashlib
import inspect
import shutil
from pathlib import Path

from nonebot import get_driver, logger, require

require("nonebot_plugin_localstore")
require("nonebot_plugin_skland")
require("plugins.skland_compact_images")

from nonebot_plugin_skland import render

from nonebot_plugin_localstore import get_plugin_cache_dir
from nonebot_plugin_skland.config import TEMPLATES_DIR

from plugins import ef_theme

LOCAL_DIR = Path(__file__).with_name("templates")
# template -> upstream files it depends on, with the sha256 they had when the local copy was made
UPSTREAM = {
    "endfield_card.html.jinja2": {
        "endfield_card.html.jinja2": "b2b6958d3280b55fb20a8cf1fa510c54ce7ece3dcce097076d0c1b914a749264",
        "endfield_macros.html.jinja2": "59f8728282438f40573172f4fe776abbfb17d420fff38e52dc5da6555e5194a5",
        "index.css": "13a449248d0208c6a6695369769e7866820b99c64f70d1a7166cdd85dde860d5",  # 0.7.2 (the card template itself is unchanged since 0.7.1)
    },
}


def _unchanged(files: dict[str, str]) -> bool:
    return all(hashlib.sha256((TEMPLATES_DIR / name).read_bytes()).hexdigest() == digest for name, digest in files.items())


ACTIVE = frozenset(name for name, files in UPSTREAM.items() if _unchanged(files))
for name in UPSTREAM.keys() - ACTIVE:
    logger.warning(f"Skland Endfield theme disabled for {name}: upstream template changed, re-audit needed")

# template -> (upstream phrase, replacement); the page keeps upstream's look, only the command names change
REWORDED = {
    "bound_roles.html.jinja2": (
        (">sk char set ark</span> &lt;序号&gt;", ">/skl切换方舟角色</span> 序号"),
        (">sk char set ef</span> &lt;序号&gt;", ">/skl切换终末地角色</span> 序号"),
        (
            '使用 <span class="font-[Bender]">sk bind</span> 或 <span class="font-[Bender]">sk qrcode</span> 绑定账号',
            '发送 <span class="font-[Bender]">/skl绑定</span> 扫码绑定账号',
        ),
    ),
}
REWORDED_DIR = get_plugin_cache_dir() / "templates"


def _reword(name: str, phrases: tuple[tuple[str, str], ...]) -> bool:
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    if any(text.count(old) != 1 for old, _ in phrases) or text.count("<head>") != 1:
        return False
    for old, new in phrases:
        text = text.replace(old, new)
    # the copy is rendered from the cache folder: relative paths must keep pointing at the package
    text = text.replace("<head>", f'<head>\n  <base href="{TEMPLATES_DIR.as_uri()}/">')
    REWORDED_DIR.mkdir(parents=True, exist_ok=True)
    (REWORDED_DIR / name).write_text(text, encoding="utf-8")
    shutil.copyfile(TEMPLATES_DIR / "index.css", REWORDED_DIR / "index.css")  # {% include 'index.css' %}
    return True


REWORDED_ACTIVE = frozenset(name for name, phrases in REWORDED.items() if _reword(name, phrases))
for name in REWORDED.keys() - REWORDED_ACTIVE:
    logger.warning(f"Skland picture {name} keeps upstream's raw command names: template changed, re-audit needed")

_previous = render.template_to_pic
_signature = inspect.signature(_previous)
if getattr(_previous, "_ef_theme", False):
    raise RuntimeError("Skland Endfield theme already installed")


@functools.wraps(_previous)
async def themed_template_to_pic(*args, **kwargs):
    bound = _signature.bind(*args, **kwargs)
    if bound.arguments.get("template_name") in ACTIVE:
        bound.arguments["template_path"] = str(LOCAL_DIR)
        bound.arguments["templates"] = {**(bound.arguments.get("templates") or {}), "ef_fonts": ef_theme.fonts_css()}
    elif bound.arguments.get("template_name") in REWORDED_ACTIVE:
        bound.arguments["template_path"] = str(REWORDED_DIR)
    return await _previous(*bound.args, **bound.kwargs)


themed_template_to_pic._ef_theme = True
render.template_to_pic = themed_template_to_pic

from . import gacha  # noqa: E402  (installs the gacha page; needs the wrapped renderer above)


@get_driver().on_startup
async def _verify_gacha_page() -> None:
    gacha.verify()
