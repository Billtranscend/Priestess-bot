"""同意入群邀请。

通过 NapCat 时，入群邀请要等账号同意；本插件一律同意。官方机器人由群成员直接加进群，没有需要同意的邀请。
"""

from __future__ import annotations

from nonebot import logger, on_request
from nonebot.adapters.onebot.v11 import Bot, GroupRequestEvent
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Group onboarding",
    description="自动同意入群邀请。",
    usage="无指令，自动生效。",
    type="application",
)


async def _is_invitation(event: GroupRequestEvent) -> bool:
    return event.sub_type == "invite"


invited = on_request(rule=_is_invitation, priority=5, block=False)


@invited.handle()
async def _(bot: Bot, event: GroupRequestEvent) -> None:
    await event.approve(bot)
    logger.info("Group onboarding: invitation accepted")
