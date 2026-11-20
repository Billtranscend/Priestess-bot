"""有人刚加机器人好友时的欢迎语、机器人被加进群时说的话，以及欢迎语里推荐的抽卡记录指令。

在能在私聊消息下方放可点击指令的机器人上（官方机器人，见 plugins/quick_buttons.py），
加好友的人会收到一条消息：一行欢迎语，下面是 SUGGESTED 里的指令。推荐指令点下去发送的就是
显示的文字，而抽卡记录分成两个指令，每款游戏一个；所以这里单独给它一个指令 /抽卡记录，
由它询问要看哪款游戏。

同一个机器人被加进群时，会说一次自己能做什么，以及群主可以打开哪两项设置让功能完整
（GROUP_WELCOME）：大多数群主根本不知道有这两项设置，不打开的话机器人收不到消息 @ 了谁，
也发不了提醒。这段话作为对“被加进群”事件的回复发出——群里允许这样回复，即使还没允许机器人
主动发言；之后不再重复，也不会发到机器人已经在的群。

其他机器人上不发欢迎语：那里机器人不主动说话，打招呼的处理也就只在官方机器人上、只为这两种通知运行
（NapCat 上每条通知都会过一遍这里）。/抽卡记录 在所有机器人上都会回答，
不能显示按钮的地方用文字列出两个指令。
"""

from __future__ import annotations

from nonebot import logger, on_command, on_notice
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11.event import FriendAddNoticeEvent, GroupIncreaseNoticeEvent
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from plugins.quick_buttons import can_show_buttons, send_buttons, send_hints
from plugins.strict_command import strict

__plugin_meta__ = PluginMetadata(
    name="Friend welcome",
    description="有人添加机器人时发一句欢迎语，下面带几个可以直接点的指令；机器人被加进群时说明一次群主可以开的两项设置；/抽卡记录 询问看哪一款游戏。",
    usage="/抽卡记录",
    type="application",
)

GACHA_COMMAND = "抽卡记录"
SUGGESTED = ["/skl绑定", f"/{GACHA_COMMAND}", "/帮助"]
WELCOME = "欢迎使用！先发送 /skl绑定 扫码绑定森空岛账号，之后就能查抽卡记录、看角色卡、每晚自动签到。下面三个指令可以直接点，在输入框输入 / 能看到更多。"
GACHA_GAMES = [("明日方舟：终末地", "/zmd抽卡记录"), ("明日方舟", "/mrfz抽卡记录")]
# 两项设置用的是 QQ 显示给群主的名称。之所以说不用 @ 也能发指令，是因为旧机器人已经不在了；
# 旧机器人还在时，它不在的群只会响应 @。
GROUP_WELCOME = (
    "本机器人可以查抽卡记录、看角色卡、每晚自动签到森空岛、看活动日历。@我 再发指令就能用，下面两个按钮可以直接点。",
    "",
    "群主可以在群成员列表里点开我，进入「设置」打开两项，功能才完整：",
    "1. 「机器人可获取的群聊消息范围」选「获取群内全部消息」：不用 @ 我也能发指令，指令里还能 @ 别人，比如 /zmd开盒 @某人",
    "2. 打开「机器人主动在群聊内发言」：活动通知、资料库更新通知这类推送才发得出来",
    "",  # 空行：没有它，最后一行会被显示成第 2 项的一部分（在真实群里看到的）
    "不开也可以用，只是少了上面这些。",
)
GROUP_SUGGESTED = [("查看帮助", "/帮助"), ("绑定账号", "/skl绑定")]


def _greeting_due(bot: Bot, event: Event) -> bool:
    """官方机器人上，有人加了好友，或者机器人自己被加进群。"""
    if not can_show_buttons(bot):
        return False
    return isinstance(event, FriendAddNoticeEvent) or (isinstance(event, GroupIncreaseNoticeEvent) and event.user_id == event.self_id)


greet = on_notice(rule=Rule(_greeting_due), priority=20, block=False)
gacha = on_command(GACHA_COMMAND, rule=strict, priority=5, block=True)


@greet.handle()
async def greet_newcomer(event: FriendAddNoticeEvent) -> None:
    await send_hints(WELCOME, SUGGESTED)


@greet.handle()
async def greet_group(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    # 无文字兜底：按钮被拒时下面自己发同样的文字，而且发不出就算了（进群时说的话不是回答指令）
    if await send_buttons("\n".join(GROUP_WELCOME), [GROUP_SUGGESTED], bot=bot, event=event):
        return
    try:
        await bot.call_api("send_group_msg", group_id=event.group_id, message="\n".join(GROUP_WELCOME))
    except Exception as e:  # 群里不允许机器人发言，事件也没法回复：什么都不说
        logger.info(f"Group welcome not sent ({type(e).__name__})")


def games_in_words() -> str:
    return "看哪一款的抽卡记录？\n" + "\n".join(f"{game}：{command}" for game, command in GACHA_GAMES)


@gacha.handle()
async def offer_games() -> None:
    await send_buttons("**抽卡记录** 看哪一款？", [GACHA_GAMES], fallback=games_in_words())
    await gacha.finish()
