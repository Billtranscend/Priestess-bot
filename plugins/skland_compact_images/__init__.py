"""Use native JPEG screenshots for Endfield cards and gacha reports."""
import functools
import hashlib
import inspect
from io import BytesIO
from pathlib import Path

from PIL import Image

from nonebot import get_driver, logger, require

require("nonebot_plugin_skland")
from nonebot_plugin_skland import render

TARGETS = frozenset({"endfield_card.html.jinja2", "ef_gacha.html.jinja2"})
QUALITY = 72
# Very tall images (e.g. /zmd开盒 all) are downscaled: QQ upload from this host is ~25KB/s.
TALL_HEIGHT = 4000
TALL_SCALE = 0.75
MID_HEIGHT = 2500  # light Endfield theme compresses worse: also trim medium-tall images
MID_SCALE = 0.85
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
    if bound.arguments.get("template_name") in TARGETS:
        bound.arguments["type"] = "jpeg"
        bound.arguments["quality"] = QUALITY
        return _shrink_tall(await _original(*bound.args, **bound.kwargs))
    return await _original(*bound.args, **bound.kwargs)


def _shrink_tall(data: bytes) -> bytes:
    try:
        with Image.open(BytesIO(data)) as im:
            width, height = im.size
            if height <= MID_HEIGHT:
                return data
            scale = TALL_SCALE if height > TALL_HEIGHT else MID_SCALE
            resized = im.convert("RGB").resize((int(width * scale), int(height * scale)), Image.LANCZOS)
        out = BytesIO()
        resized.save(out, "JPEG", quality=QUALITY, optimize=True)
        logger.debug(f"Skland tall image {width}x{height} {len(data)}B -> {resized.size} {out.tell()}B")
        return out.getvalue()
    except Exception as e:
        logger.warning(f"Skland tall image downscale skipped: {type(e).__name__}")
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
    logger.info(f"Skland compact images ready: Endfield card/gacha JPEG quality={QUALITY}, images taller than {MID_HEIGHT}/{TALL_HEIGHT}px scaled x{MID_SCALE}/x{TALL_SCALE}")
