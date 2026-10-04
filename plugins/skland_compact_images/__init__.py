"""Send every picture the Skland plugin renders as WebP.

Upstream screenshots its templates as PNG (several MB for a card). Upload from this host to QQ
drops to ~15 KB/s in the evening, so the size decides how long a member waits. The screenshot is
taken as PNG and re-encoded once as WebP, which needs well under half the bytes of a JPEG that
looks as sharp. The two Endfield pages can get very long (/zmd开盒 all, multi-page gacha
reports) and are scaled down a little beyond a height limit.
"""
import asyncio
import functools
import hashlib
import inspect
from io import BytesIO
from pathlib import Path

from PIL import Image

from nonebot import get_driver, logger, require

require("nonebot_plugin_skland")
from nonebot_plugin_skland import render

TARGETS = frozenset({"endfield_card.html.jinja2", "ef_gacha.html.jinja2"})  # the long Endfield pages
QUALITY = 60
TALL_HEIGHT = 9000  # px after the 1.5x device scale; taller pages are scaled by TALL_SCALE
TALL_SCALE = 0.8
WEBP_MAX_SIDE = 16383  # format limit
_expected = "6552ed367003eca27b1deaca36197e6945b80c11c9adc88ad75916307435ed00"
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
    data = await _original(*bound.args, **bound.kwargs)
    return await asyncio.to_thread(_to_webp, data, bound.arguments.get("template_name") in TARGETS)


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
