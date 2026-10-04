"""Send the static Skland help image for /skl帮助.

The image is rendered from help.html by deploy/render_skl_help.py and read on
every request, so re-rendering does not need a bot restart. A static file also
lets QQ reuse the uploaded image, keeping repeat sends fast.
"""

import contextlib
from pathlib import Path

from nonebot import logger, on_command
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict
from nonebot_plugin_alconna import UniMessage, message_reaction

__plugin_meta__ = PluginMetadata(
    name="Skland help image",
    description="Replies to /skl帮助 with the bot's command guide image.",
    usage="/skl帮助",
    type="application",
)

HELP_IMAGE = Path(__file__).with_name("help.jpg")

if not HELP_IMAGE.is_file():
    logger.warning(f"Skland help image is missing: {HELP_IMAGE}")

# Same QQ face IDs Skland uses for its message reactions.
REACTION_PROCESSING = "66"  # heart
REACTION_DONE = "144"  # party popper
REACTION_FAIL = "10060"  # cross mark

skl_help = on_command("skl帮助", aliases={"skd帮助", "森空岛帮助"}, rule=strict, priority=5, block=True)


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


@skl_help.handle()
async def _() -> None:
    await _react(REACTION_PROCESSING)
    try:
        image = HELP_IMAGE.read_bytes()
    except OSError as e:
        logger.warning(f"Skland help image unavailable: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await skl_help.finish("帮助图暂时不可用，请联系管理员")
    try:
        await UniMessage.image(raw=image).send()
    except Exception:
        await _react(REACTION_FAIL)
        raise
    await _react(REACTION_DONE)
