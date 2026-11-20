"""几个插件共用的小工具（普通模块，不是插件）。

- write_json：原子地写 JSON 文件；
- react 和三个表情码：指令处理中给消息加表情回应；
- first_bot：定时任务里取一个可用的 OneBot V11 机器人；
- tell_superusers：私聊告诉每个超级用户一件事；
- bot_left_group：通知是“机器人自己不在这个群了”（被移出，或群已解散），各插件据此清掉该群的数据；
  left_for_sure 确认这条通知，groups_left 供每天补清漏掉的群；
- upload_timed_out 和 SLOW_UPLOAD：发图失败其实只是 NapCat 上传慢，回这句话而不是“生成失败”；
- heybox_hint：抽卡记录页面上提醒成员可以从小黑盒补全更早的记录。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from nonebot.adapters import Bot, Event

def bot_left_group(event: Event) -> bool:
    """群成员减少的通知里，离开的是机器人自己。"""
    return getattr(event, "notice_type", None) == "group_decrease" and str(getattr(event, "user_id", "")) == str(getattr(event, "self_id", None))


LEAVE_SETTLE = 10  # 收到通知后等这么久再看群列表：NapCat 的列表不一定马上变
RECHECK_GAP = 50  # 两次读群列表隔这么久，两次都没有才算不在：一次读到不全的列表删不掉任何东西
_seen_bots: set[str] = set()  # 这次运行里连上过的 OneBot V11 账号


def _remember_bots() -> None:
    """连上来的账号都记下：一个号掉线时读不到它在的群，那些群不能当作机器人已经不在。"""
    from nonebot import get_driver

    async def remember(bot: Bot) -> None:  # Bot 在模块顶部导入：NoneBot 按模块的名字解析这个注解
        _seen_bots.add(str(bot.self_id))

    get_driver().on_bot_connect(remember)


with contextlib.suppress(ValueError):  # nonebot 还没初始化（单独导入这个模块时）
    _remember_bots()


async def _groups_of(bot) -> set[int] | None:
    """这个机器人现在所在的群；读不到或读到空列表时为 None（空列表多半是没读到，不能当作哪个群都不在）。"""
    from nonebot import logger

    try:
        return {int(group["group_id"]) for group in await bot.get_group_list(no_cache=True)} or None
    except Exception as e:
        logger.warning(f"Group list unavailable ({type(e).__name__}); no group data is cleaned up")
        return None


async def groups_now() -> set[int] | None:
    """连着的各个 OneBot V11 机器人现在所在的群（合在一起）。说不清时为 None，调用方什么都不删：

    没有机器人在线、连上过的某个账号现在掉线、有一个读不到群列表或读到空列表，或者加载了官方机器人的桥接。
    桥接自己每天逐群核对，不在的群会像被移出一样发通知；它的群列表也不能这样用（重新查询时不列出还没见过成员的群）。
    """
    from nonebot import get_bots
    from nonebot.adapters.onebot.v11 import Bot

    if "plugins.official_bridge" in sys.modules:
        return None
    bots = {str(bot.self_id): bot for bot in get_bots().values() if isinstance(bot, Bot)}
    _seen_bots.update(bots)
    if _seen_bots - bots.keys():
        return None
    found: set[int] = set()
    for bot in bots.values():
        groups = await _groups_of(bot)
        if groups is None:
            return None
        found |= groups
    return found or None


async def groups_left(groups: Iterable[int]) -> set[int]:
    """groups 里机器人已经不在的群：隔 RECHECK_GAP 秒两次读群列表都没有才算。说不清（见 groups_now）时为空。"""
    first = await groups_now()
    missing = set(groups) - first if first is not None else set()
    if not missing:
        return set()
    await asyncio.sleep(RECHECK_GAP)
    second = await groups_now()
    return missing - second if second is not None else set()


async def left_for_sure(bot, group_id: int) -> bool:
    """收到 bot_left_group 的通知后，是不是真该清掉这个群的数据。

    官方机器人：桥接确认过（平台的移出事件，或平台说群不存在）、删掉自己的群记录之后才发这条通知，直接算数；
    这时它的群列表反而确认不了（只在这一个群时是空的）。其他机器人（NapCat）：等 LEAVE_SETTLE 秒，
    再按 groups_left 看群列表；说不清或还在群里就先不删，留给各插件每天的补清。
    """
    from nonebot import logger

    from plugins.quick_buttons import is_official_bot

    if is_official_bot(bot):
        return True
    await asyncio.sleep(LEAVE_SETTLE)
    if group_id in await groups_left([group_id]):
        return True
    logger.info("The bot was reported out of a group, but its group list does not confirm it; the group's data is kept")
    return False


# 和森空岛插件用同一组 QQ 表情
REACTION_PROCESSING = "66"  # 爱心：正在处理
REACTION_DONE = "144"  # 彩带：完成
REACTION_FAIL = "10060"  # 叉号：失败


SLOW_UPLOAD = "图片上传较慢，可能会晚一点出现；如果一直没有出现，请过几分钟再发一次"


def upload_timed_out(e: Exception) -> bool:
    """发送失败是 NapCat 等 QQ 收图超时：图往往稍后仍会送达，说“生成失败”会让成员重发、收到两份。

    认 NapCat 回的 ActionFailed（retcode 1200 或提到 timeout），以及 NoneBot 自己等 NapCat 回应超时的
    NetworkError。官方机器人一律不算：桥接用 1200 表示别的失败（机器人不在群、消息为空、文件读不到等）。
    """
    from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

    from plugins.quick_buttons import is_official_bot

    if is_official_bot():
        return False
    if isinstance(e, ActionFailed):
        return e.info.get("retcode") == 1200 or "timeout" in str(e).lower()
    return isinstance(e, NetworkError) and "timeout" in str(e.msg or "").lower()


def write_json(path: Path, value: Any, **options: Any) -> None:
    """把 value 写成 JSON：先写同目录下的临时文件（权限 600），再整个替换，读的一方不会读到写了一半的文件。

    options 原样交给 json.dumps；不写 ensure_ascii 时中文按原样写入。
    """
    options.setdefault("ensure_ascii", False)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, **options), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


async def react(emoji: str) -> None:
    """给正在处理的那条消息加一个表情回应；做不到（例如官方机器人不支持）就算了。"""
    from nonebot_plugin_alconna import message_reaction

    with contextlib.suppress(Exception):
        await message_reaction(emoji)


def first_bot():
    """当前连着的第一个 OneBot V11 机器人（官方机器人经桥接也算），没有时为 None。"""
    from nonebot import get_bots
    from nonebot.adapters.onebot.v11 import Bot

    return next((bot for bot in get_bots().values() if isinstance(bot, Bot)), None)


async def tell_superusers(text: str, who: str) -> None:
    """私聊发给每个超级用户，绝不发到群里；who 是写日志时的插件名。"""
    from nonebot import get_driver, logger

    bot = first_bot()
    if bot is None:
        logger.warning(f"{who}: no bot online, superusers not told")
        return
    for user_id in get_driver().config.superusers:
        try:
            await bot.send_private_msg(user_id=int(user_id), message=text)
        except Exception as e:
            logger.warning(f"{who}: a superuser could not be told ({type(e).__name__})")


HEYBOX_HINT = "官方接口只提供近期记录，如果你在小黑盒有更早的抽卡记录，可发送 {commands} 补全"
HEYBOX_IMPORTS = {"endfield": "/zmd导入小黑盒 小黑盒ID", "arknights": "/mrfz导入小黑盒 小黑盒ID"}
HEYBOX_GAP_DAYS = 30  # 建号之后这么久还没有一条官方记录，才算可能缺了早期的抽卡（新玩家建号当天就会抽新手池）
HEYBOX_HINT_TIMES = 3  # 每个角色最多提醒几次：没用过小黑盒的老玩家不会一直被提醒
HEYBOX_HINTS_FILE = Path(__file__).resolve().parents[1] / "data" / "shared" / "heybox_hints.json"


def heybox_hint(earliest: int | None, imported: bool, app_code: str, created: float | None, character_id: int) -> str:
    """抽卡记录页面上的提醒：返回一句话，或空字符串。

    earliest：最早一条抽卡记录的时间；imported：有没有导入过小黑盒的行（pos 为负）。
    要提醒：没导入过小黑盒，而建号之后过了 HEYBOX_GAP_DAYS 天才有第一条官方记录
    （官方接口只给近几个月，更早的抽卡查不到了）。建号时间不知道时不提醒。有没有小黑盒数据这边看不出来，
    所以每个角色最多提醒 HEYBOX_HINT_TIMES 次。绑定后的通知可能没送到，看记录时再说。
    """
    if earliest is None or imported or not created or earliest - created < HEYBOX_GAP_DAYS * 86400:
        return ""
    try:
        shown = json.loads(HEYBOX_HINTS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        shown = {}
    key = str(character_id)
    if shown.get(key, 0) >= HEYBOX_HINT_TIMES:
        return ""
    shown[key] = shown.get(key, 0) + 1
    with contextlib.suppress(OSError):
        HEYBOX_HINTS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(HEYBOX_HINTS_FILE, shown)
    return HEYBOX_HINT.format(commands=HEYBOX_IMPORTS[app_code])
