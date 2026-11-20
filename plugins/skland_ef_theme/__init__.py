"""终末地风格的 /zmd抽卡记录，以及对上游其他图片的小修正；site-packages 不动。

/zmd抽卡记录：见 gacha.py。0.7.2 起这一页连同指令处理都是本项目自己的。

对上游其他图片的小修正（REVISED）：导入时在缓存里生成一份模板副本，替换掉其中几句；
如果某一句不再恰好出现一次，就照原样使用上游模板。
  /skl角色    上游直接印出它自己的原始指令（"sk char set ark"）；副本里写的是本机器人的指令。
"""
import functools
import inspect
import re
import shutil

from nonebot import get_driver, logger, require

require("nonebot_plugin_localstore")
require("nonebot_plugin_skland")
require("plugins.skland_compact_images")

from nonebot_plugin_skland import render

from nonebot_plugin_localstore import get_plugin_cache_dir
from nonebot_plugin_skland.config import TEMPLATES_DIR

# 模板 -> （上游原句，替换句）；页面保持上游的样子
REVISED = {
    "bound_roles.html.jinja2": (
        (">sk char set ark</span> &lt;序号&gt;", ">/skl切换方舟角色</span> 序号"),
        (">sk char set ef</span> &lt;序号&gt;", ">/skl切换终末地角色</span> 序号"),
        (
            '使用 <span class="font-[Bender]">sk bind</span> 或 <span class="font-[Bender]">sk qrcode</span> 绑定账号',
            '发送 <span class="font-[Bender]">/skl绑定</span> 扫码绑定账号',
        ),
    ),
}
REVISED_DIR = get_plugin_cache_dir() / "templates"
_PULLED_IN = re.compile(r"""{%-?\s*(?:include|from|import)\s+['"]([\w.-]+)['"]""")


def _revise(name: str, phrases: tuple[tuple[str, str], ...]) -> bool:
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    if any(text.count(old) != 1 for old, _ in phrases) or text.count("<head>") != 1:
        return False
    for old, new in phrases:
        text = text.replace(old, new)
    # 副本从缓存目录渲染：相对路径必须仍指向插件包里的文件
    text = text.replace("<head>", f'<head>\n  <base href="{TEMPLATES_DIR.as_uri()}/">')
    REVISED_DIR.mkdir(parents=True, exist_ok=True)
    (REVISED_DIR / name).write_text(text, encoding="utf-8")
    pending, copied = _PULLED_IN.findall(text), set()
    while pending:  # 它 include 或 import 的文件也要放在它旁边，才找得到
        part = pending.pop()
        if part not in copied and (TEMPLATES_DIR / part).is_file():
            copied.add(part)
            shutil.copyfile(TEMPLATES_DIR / part, REVISED_DIR / part)
            pending += _PULLED_IN.findall((TEMPLATES_DIR / part).read_text(encoding="utf-8"))
    return True


REVISED_ACTIVE = frozenset(name for name, phrases in REVISED.items() if _revise(name, phrases))
for name in REVISED.keys() - REVISED_ACTIVE:
    logger.warning(f"Skland picture {name} is rendered from upstream's template as it is: it changed, re-audit the corrections")

_previous = render.template_to_pic
_signature = inspect.signature(_previous)
if getattr(_previous, "_ef_theme", False):
    raise RuntimeError("Skland Endfield theme already installed")


@functools.wraps(_previous)
async def themed_template_to_pic(*args, **kwargs):
    bound = _signature.bind(*args, **kwargs)
    if bound.arguments.get("template_name") in REVISED_ACTIVE:
        bound.arguments["template_path"] = str(REVISED_DIR)
    return await _previous(*bound.args, **bound.kwargs)


themed_template_to_pic._ef_theme = True
render.template_to_pic = themed_template_to_pic

from . import gacha  # noqa: E402  （安装抽卡页面；需要先有上面包好的渲染函数）


@get_driver().on_startup
async def _verify_gacha_page() -> None:
    gacha.verify()
