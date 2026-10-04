"""Accept group invitations and give each newly joined group the default Bison subscriptions.

  - an invitation to a group (request.group.invite) is approved at once;
  - when the bot has joined, a request for that group is queued for deploy/bison_subscribe.py,
    which runs outside the bot's sandbox (systemd path unit) and adds the default nonebot-bison
    subscriptions: 明日方舟 and 明日方舟终末地 on Bilibili, with the rules most groups use.

The queue is a directory of empty files named after the group id; this process cannot write to
Bison's database itself.
"""

from __future__ import annotations

import asyncio

from nonebot import logger, on_notice, on_request, require
from nonebot.adapters.onebot.v11 import Bot, GroupIncreaseNoticeEvent, GroupRequestEvent
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as store

__plugin_meta__ = PluginMetadata(
    name="Group onboarding",
    description="自动同意入群邀请，并为新加入的群添加默认的 B 站推送订阅。",
    usage="无指令，自动生效。",
    type="application",
)

REQUESTS_DIR = store.get_plugin_data_dir() / "requests"
REQUESTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
JOIN_CHECKS = (10, 60, 300)  # seconds after approving an invitation; QQ does not always report the join
_tasks: set[asyncio.Task] = set()


def queue_subscriptions(group_id: int) -> None:
    (REQUESTS_DIR / str(int(group_id))).touch(mode=0o600)
    logger.info("Group onboarding: default Bison subscriptions queued for a new group")


async def _queue_once_joined(bot: Bot, group_id: int) -> None:
    waited = 0
    for moment in JOIN_CHECKS:
        await asyncio.sleep(moment - waited)
        waited = moment
        try:
            groups = await bot.get_group_list(no_cache=True)
        except Exception as e:
            logger.warning(f"Group onboarding: group list unavailable ({type(e).__name__})")
            continue
        if any(group.get("group_id") == group_id for group in groups):
            queue_subscriptions(group_id)
            return
    logger.warning("Group onboarding: an accepted invitation did not lead to a join")


async def _is_invitation(event: GroupRequestEvent) -> bool:
    return event.sub_type == "invite"


async def _bot_joined(event: GroupIncreaseNoticeEvent) -> bool:
    return event.user_id == event.self_id


invited = on_request(rule=_is_invitation, priority=5, block=False)


@invited.handle()
async def _(bot: Bot, event: GroupRequestEvent) -> None:
    await event.approve(bot)
    logger.info("Group onboarding: invitation accepted")
    task = asyncio.get_running_loop().create_task(_queue_once_joined(bot, event.group_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


joined = on_notice(rule=_bot_joined, priority=5, block=False)


@joined.handle()
async def _(event: GroupIncreaseNoticeEvent) -> None:
    queue_subscriptions(event.group_id)
