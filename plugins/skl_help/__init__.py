"""/skl帮助：帮助图；在能显示按钮的机器人上，先给出可点的分类。

回复时最慢的是上传图片（要几秒），所以能在消息下挂按钮的机器人收到不带参数的 /skl帮助 时，
回复自动运行的功能，并为页面的每个分类放一个按钮；点开分类就是该分类指令的文字说明，
点「完整帮助图」则发送图片。在任何机器人上，/skl帮助 <分类> 都用文字回答，/skl帮助 完整帮助图
发图片；不能显示按钮的机器人收到不带参数的 /skl帮助 时照旧发图片，分类文字末尾写着其他分类和
完整帮助图怎么要（代替两个按钮）。文字内容从 help.html 读取（guide.py）。

带按钮的消息在有人从中做出选择后撤回（由 quick_buttons 统一处理）：选了分类或图片就撤回菜单，点返回就撤回分类。
不能显示按钮的机器人在群里发的分类文字也一样（按文字菜单发出）。

图片按本部署的情况从 help.html 生成（render.py：官方机器人或 NapCat 各自的部分、机器人的名字），
存在 data/skl_help/。自上次以来 help.html 或设置有变化时，启动时渲染一次，否则直接从磁盘读取，
这样重复发送用的是同一个文件，QQ 可以复用上传。启动时渲染失败的话（例如首次启动时浏览器还在下载），
至少五分钟后的 /skl帮助 会再试；这期间如果有旧图就发旧图。
deploy/render_skl_help.py 可以不重启手动渲染。
"""

import asyncio
import time

from nonebot import get_driver, logger, on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from plugins.quick_buttons import can_show_buttons, pages_of, rows_fit, send_buttons
from plugins.strict_command import strict
from nonebot_plugin_alconna import UniMessage

from . import guide as help_guide
from . import render as help_render
from plugins.shared import REACTION_DONE, REACTION_FAIL, REACTION_PROCESSING, react as _react

__plugin_meta__ = PluginMetadata(
    name="Skland help image",
    description="Replies to /skl帮助 with the bot's command guide: sections to tap where buttons can be shown, the image otherwise.",
    usage="/skl帮助　/帮助　/help　/帮助 分类名　/帮助 完整帮助图",
    type="application",
)

driver = get_driver()

STARTUP_DELAY = 20  # 秒：先让机器人连上，并让 htmlrender 下载好浏览器
RETRY_AFTER = 300  # 渲染失败后隔多少秒再试（每次尝试都要启动一个浏览器）

skl_help = on_command("skl帮助", aliases={"skd帮助", "森空岛帮助", "帮助", "help"}, rule=strict, priority=5, block=True)

_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()
_failed_at: float | None = None  # 上次渲染失败时的 time.monotonic()


class RenderPaused(Exception):
    """上次渲染刚失败不久：暂不重试。"""


async def help_image() -> bytes:
    """本部署用的帮助图；没有或过期时先渲染。"""
    global _failed_at
    html = help_render.page(**help_render.settings(driver.config))
    async with _lock:
        if not help_render.is_current(html):
            if _failed_at is not None and time.monotonic() - _failed_at < RETRY_AFTER:
                raise RenderPaused("the last render failed less than five minutes ago")
            try:
                await asyncio.to_thread(help_render.render, html)
            except Exception:
                _failed_at = time.monotonic()
                raise
            _failed_at = None
            logger.info("Skland help image rendered for this deployment")
    return help_render.IMAGE.read_bytes()


async def _render_at_startup() -> None:
    await asyncio.sleep(STARTUP_DELAY)
    try:
        await help_image()
    except Exception as e:
        logger.warning(f"Skland help image not rendered at startup, the next /skl帮助 tries again: {type(e).__name__}: {e}")


@driver.on_startup
async def _schedule_render() -> None:
    task = asyncio.create_task(_render_at_startup())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)




def in_words(asked: str, buttons: bool):
    """代替图片要说的文字，以及挂在下面的按钮；该发图片（或页面读不了）时返回 None。"""
    try:
        page = help_render.page(**help_render.settings(driver.config))
        return help_guide.answer(help_guide.parse(page), asked, buttons)
    except Exception as e:  # 还有图片可以发
        logger.warning(f"Skland help in words not built, the image is sent: {type(e).__name__}: {e}")
        return None


@skl_help.handle()
async def answer_help(arg: Message = CommandArg()) -> None:
    await say_help(arg.extract_plain_text())


async def say_help(asked: str) -> None:
    said = in_words(asked, can_show_buttons())
    if said is not None:
        text, buttons = said
        back = buttons[-len(help_guide.BACK):] == help_guide.BACK
        rows = rows_fit(buttons[: -len(help_guide.BACK)]) + [list(help_guide.BACK)] if back else rows_fit(buttons)  # 返回和图片单独一行，放在最后
        pages = pages_of(rows)
        if back:  # 分类或说明：没有按钮，文字照样能用
            await send_buttons(help_guide.words_by_page(text, pages) if pages else text, rows, fallback=help_guide.plain(text))
            await skl_help.finish()
        # 无文字兜底：总览只有挂着按钮才有意义，发不出就改发下面的帮助图
        if await send_buttons(text, rows):
            await skl_help.finish()
    await _react(REACTION_PROCESSING)
    try:
        image = await help_image()
    except Exception as e:
        if not isinstance(e, RenderPaused):
            logger.warning(f"Skland help image could not be rendered: {type(e).__name__}: {e}")
        try:
            image = help_render.IMAGE.read_bytes()  # 有旧图总比没有强
        except OSError:
            await _react(REACTION_FAIL)
            await skl_help.finish("帮助图暂时不可用，请联系管理员")
    try:
        await UniMessage.image(raw=image).send()
    except Exception:
        await _react(REACTION_FAIL)
        raise
    await _react(REACTION_DONE)
