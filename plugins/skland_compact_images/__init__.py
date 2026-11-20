"""Skland 插件渲染的每张图都以 WebP 发送。

上游把模板截图成 PNG（一张卡片好几 MB）。晚上从本机上传到 QQ 只有约 15 KB/s，所以图片大小决定了
成员要等多久。截图先按 PNG 截，再重新编码一次成 WebP；同样清晰度下，体积远不到 JPEG 的一半。
压缩目标（TARGETS）只剩终末地抽卡记录（ef_gacha.html.jinja2）：它可能很长（好几页），
超过一定高度后会再缩小一点。

截图要等页面上的每张图片加载完；外部图床慢的时候，页面 30 秒后超时，成员就什么回复都收不到。
所以超时的渲染会再试一次，再超时就先告诉成员（每条指令只说一次），然后结束。

渲染还要先等到 RENDER_SLOTS 中的一个空位：上游会把一份干员列表的各页同时绘制，一分钟内四条
/方舟干员就曾让十几页立绘页面同时压在 CPU 上，全部超时，重试也全部超时。
"""
import asyncio
import contextlib
import functools
import hashlib
import inspect
import weakref
from pathlib import Path

from nonebot import get_driver, logger, require
from nonebot.matcher import current_matcher
from playwright.async_api import TimeoutError as PageTimeout

require("nonebot_plugin_skland")
from nonebot_plugin_skland import render
from plugins.ef_theme import to_webp
from plugins.shared import REACTION_FAIL, react

TARGETS = frozenset({"ef_gacha.html.jinja2"})  # 很长的终末地页面（多页抽卡记录）
QUALITY = 60
TALL_HEIGHT = 9000  # 按 1.5 倍设备缩放后的像素；更高的页面按 TALL_SCALE 缩小
TALL_SCALE = 0.8
RENDER_TRIES = 2  # 超时的渲染（外部图床慢）再试一次
TIMEOUT_TEXT = "生成图片超时（图片素材加载太慢），请稍后再试"
RENDER_SLOTS = 3  # 所有指令合计同时渲染的页面数；其余的排队等候
_expected = "28713b0c8026e16f8c6b44ee305b443175296b3701e4334cf8cb9de4ee3b32a0"  # 0.7.4 版 render.py 的哈希值
if hashlib.sha256(Path(render.__file__).read_bytes()).hexdigest() != _expected:
    raise RuntimeError("Skland compact images requires re-audit of renderer")
_original = render.template_to_pic
_signature = inspect.signature(_original)
if getattr(_original, "_compact_images", False):
    raise RuntimeError("Skland compact images already installed")
_slots = asyncio.Semaphore(RENDER_SLOTS)
_told = weakref.WeakSet()  # 已经给成员发过 TIMEOUT_TEXT 的 matcher


@functools.wraps(_original)
async def compact_template_to_pic(*args, **kwargs):
    bound = _signature.bind(*args, **kwargs)
    bound.arguments["type"] = "png"
    bound.arguments["quality"] = None
    for attempt in range(RENDER_TRIES):
        try:
            async with _slots:  # 在这里等待不计入页面超时
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
    """在指令里说明为什么没有图（指令仍以出错结束）；每条指令只说一次。"""
    matcher = current_matcher.get(None)
    if matcher is None or matcher in _told:
        return
    _told.add(matcher)
    from nonebot_plugin_alconna import UniMessage

    await react(REACTION_FAIL)  # 失败表情 NapCat 上能看到；官方机器人不支持，react 会跳过
    with contextlib.suppress(Exception):
        await UniMessage.text(TIMEOUT_TEXT).send(reply_to=True)


def _to_webp(data: bytes, long_page: bool) -> bytes:
    """转成 WebP（长页面超过 TALL_HEIGHT 再缩小）；转不了就照发 PNG：大一点，但成员仍能收到图。"""
    try:
        return to_webp(data, QUALITY, tall_height=TALL_HEIGHT, tall_scale=TALL_SCALE if long_page else 1)
    except Exception as e:
        logger.warning(f"Skland picture kept as PNG: {type(e).__name__}")
        return data


compact_template_to_pic._compact_images = True
render.template_to_pic = compact_template_to_pic


@get_driver().on_startup
async def verify_compact_images():
    hook = render.template_to_pic
    while hook is not compact_template_to_pic and getattr(hook, "_ef_theme", False):  # skland_ef_theme 会包住这个钩子
        hook = hook.__wrapped__
    if hook is not compact_template_to_pic:
        raise RuntimeError("Skland compact image hook replaced")
    logger.info(f"Skland compact images ready: WebP quality={QUALITY}, Endfield pages taller than {TALL_HEIGHT}px scaled x{TALL_SCALE}")
