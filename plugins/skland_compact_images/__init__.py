"""Send every picture the Skland plugin renders as WebP.

Upstream screenshots its templates as PNG (several MB for a card). Upload from this host to QQ
drops to ~15 KB/s in the evening, so the size decides how long a member waits. The screenshot is
taken as PNG and re-encoded once as WebP, which needs well under half the bytes of a JPEG that
looks as sharp. The two Endfield pages can get very long (/zmd开盒 all, multi-page gacha
reports) and are scaled down a little beyond a height limit.

A screenshot waits for every image of the page; when an outside image host is slow the page times out
after 30 s and the member used to get no answer at all (2026-10-19, /mrfz开盒). A timed-out render is
now tried once more, and if that also times out the member is told so before the command ends.
"""
import asyncio
import contextlib
import functools
import hashlib
import inspect
from io import BytesIO
from pathlib import Path

from PIL import Image

from nonebot import get_driver, logger, require
from nonebot.matcher import current_matcher
from playwright.async_api import TimeoutError as PageTimeout

require("nonebot_plugin_skland")
from nonebot_plugin_skland import render

TARGETS = frozenset({"endfield_card.html.jinja2", "ef_gacha.html.jinja2"})  # the long Endfield pages
QUALITY = 60
TALL_HEIGHT = 9000  # px after the 1.5x device scale; taller pages are scaled by TALL_SCALE
TALL_SCALE = 0.8
WEBP_MAX_SIDE = 16383  # format limit
RENDER_TRIES = 2  # a render that times out (slow outside image host) is tried once more
TIMEOUT_TEXT = "生成图片超时（图片素材加载太慢），请稍后再试"
REACTION_FAIL = "10060"
_expected = "71cb72802aa29d8fc6abc9a9de7e0f37d0d3dfa856e6d865a44204c52f0a48d0"  # render.py of 0.7.2
if hashlib.sha256(Path(render.__file__).read_bytes()).hexdigest() != _expected:
    raise RuntimeError("Skland compact images requires re-audit of renderer")
_original = render.template_to_pic
_signature = inspect.signature(_original)
if getattr(_original, "_compact_images", False):
    raise RuntimeError("Skland compact images already installed")


@functools.wraps(_original)
async def compact_template_to_pic(*args, **kwargs):
    bound = _signature.bind(*args, **kwargs)
    bound.arguments["type"] = "png"
    bound.arguments["quality"] = None
    for attempt in range(RENDER_TRIES):
        try:
            data = await _original(*bound.args, **bound.kwargs)
            break
        except PageTimeout:
            if attempt < RENDER_TRIES - 1:
                logger.info("Skland picture render timed out, trying once more")
                continue
            logger.warning(f"Skland picture render timed out {RENDER_TRIES} times")
            await _tell_timeout()
            raise
    return await asyncio.to_thread(_to_webp, data, bound.arguments.get("template_name") in TARGETS)


async def _tell_timeout() -> None:
    """Inside a command, say why no picture comes (the command still ends with the error)."""
    if current_matcher.get(None) is None:
        return
    from nonebot_plugin_alconna import UniMessage, message_reaction

    with contextlib.suppress(Exception):
        await message_reaction(REACTION_FAIL)
    with contextlib.suppress(Exception):
        await UniMessage.text(TIMEOUT_TEXT).send(reply_to=True)


def _to_webp(data: bytes, long_page: bool) -> bytes:
    try:
        with Image.open(BytesIO(data)) as im:
            im = im.convert("RGB")
            scale = TALL_SCALE if long_page and im.height > TALL_HEIGHT else 1
            scale = min(scale, WEBP_MAX_SIDE / max(im.size))
            if scale < 1:
                im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
            out = BytesIO()
            im.save(out, "WEBP", quality=QUALITY, method=4)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"Skland picture kept as PNG: {type(e).__name__}")
        return data


compact_template_to_pic._compact_images = True
render.template_to_pic = compact_template_to_pic


@get_driver().on_startup
async def verify_compact_images():
    hook = render.template_to_pic
    while hook is not compact_template_to_pic and getattr(hook, "_ef_theme", False):  # skland_ef_theme wraps this hook
        hook = hook.__wrapped__
    if hook is not compact_template_to_pic:
        raise RuntimeError("Skland compact image hook replaced")
    logger.info(f"Skland compact images ready: WebP quality={QUALITY}, Endfield pages taller than {TALL_HEIGHT}px scaled x{TALL_SCALE}")
