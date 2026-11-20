"""在 nonebot-plugin-skland 部分自带指令之后加按钮（能显示按钮的机器人，plugins/quick_buttons.py）。

  肉鸽战绩（/树海肉鸽 ...）   其他主题，一点即查
  /方舟干员                  职业和 6星，作为筛选
  /skl角色                   某个游戏有多个角色的成员：把另一个角色设为默认
                             （两个游戏都有多个角色时：先选游戏，再选该游戏的角色）
  /skl切换终末地角色         不带序号发送时（选完游戏发出的就是这个）：列出该游戏的角色

指令本身是上游的，不做改动：一个发送钩子记下指令已经发出了图片，指令结束后再跟上按钮。
以提示结束的指令（没绑定、什么都没查到）不加按钮。显示不了按钮的机器人上，只有不带序号的切换指令
改用文字列出角色；图片后面什么都不跟（这些指令帮助里都有，/skl角色 的卡片下面也留着教切换的两行字）。
"""

from __future__ import annotations

from nonebot import logger, require
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot
from nonebot.matcher import Matcher, current_matcher
from nonebot.message import run_postprocessor
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_alconna")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")

from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.db_handler import get_default_character, get_user_characters

from plugins.quick_buttons import Button, Rows, can_show_buttons, plain, rows_fit, rows_of, send_buttons
from plugins.skland_roles import owner_id_of

__plugin_meta__ = PluginMetadata(
    name="Skland buttons",
    description="在能显示按钮的机器人上，给肉鸽战绩、方舟干员、角色列表加快捷按钮。",
    usage="无指令，自动生效。",
    type="application",
)

SEND_APIS = frozenset({"send_msg", "send_group_msg", "send_private_msg", "send_forward_msg", "send_group_forward_msg", "send_private_forward_msg"})
ROGUE_THEMES = {"黑流树海": "树海肉鸽", "界园": "界园肉鸽", "萨卡兹": "萨卡兹肉鸽", "萨米": "萨米肉鸽", "水月": "水月肉鸽", "傀影": "傀影肉鸽"}  # 主题 -> 对应指令
BOX_FILTERS = ("先锋", "近卫", "重装", "狙击", "术师", "医疗", "辅助", "特种", "6星")
GAMES = {"endfield": ("终末地", "/skl切换终末地角色"), "arknights": ("明日方舟", "/skl切换方舟角色")}  # app code -> 游戏名，把角色设为默认的指令
_pictured: set[int] = set()  # 已经发出图片的运行中指令（按 id）


def has_picture(api: str, data: dict) -> bool:
    """这次发送是否带了图片（或多页卡片）？"""
    if "forward" in api:
        return True
    message = data.get("message")
    segments = [message] if hasattr(message, "type") else (message if not isinstance(message, str) and message is not None else ())
    return any((getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)) == "image" for seg in segments)


@Bot.on_called_api
async def _note_picture(bot: Bot, exception: Exception | None, api: str, data: dict, result) -> None:
    if exception is not None or api not in SEND_APIS:
        return
    matcher = current_matcher.get(None)
    if matcher is not None and matcher.plugin_name == "nonebot_plugin_skland" and has_picture(api, data):
        if len(_pictured) > 500:  # 一直没走到结束的指令
            _pictured.clear()
        _pictured.add(id(matcher))


def rogue_buttons(result) -> tuple[str, Rows] | None:
    """其他主题，给查看自己战绩的成员。"""
    if result.query("rogue.target") is not None:
        return None
    current = str(result.query("rogue.topic.topic_name") or "")
    role = result.query("rogue.role.role_index")
    suffix = f" 角色{role}" if role is not None else ""
    return "**肉鸽战绩** 换个主题", rows_of([(command, f"/{command}{suffix}") for theme, command in ROGUE_THEMES.items() if theme != current])


def box_buttons(result) -> tuple[str, Rows] | None:
    """职业和 6星 作为筛选，给查看自己干员的成员。"""
    if result.query("box.target") is not None:
        return None
    asked = {str(word) for word in result.query("box.filters") or ()}
    role = result.query("box.role.role_index")
    suffix = f" 角色{role}" if role is not None else ""
    return "**方舟干员** 筛选", rows_of([(word, f"/方舟干员 {word}{suffix}") for word in BOX_FILTERS if word not in asked])


Role = tuple[int, str]  # 角色的序号和昵称


async def switchable(owner: int | None) -> dict[str, list[Role]]:
    """成员（owner_id，没绑定时为 None）有多个角色的每个游戏：每个非默认角色的序号和昵称。"""
    games: dict[str, list[Role]] = {}
    if owner is None:
        return games
    async with get_session() as session:
        for app_code in GAMES:
            roles = await get_user_characters(owner, app_code, session)
            if len(roles) < 2:
                continue
            default = await get_default_character(owner, app_code, session)
            # 角色的序号就是它在这个列表里的位置，和卡片上、切换指令里的一致
            others = [(number, role.nickname) for number, role in enumerate(roles, 1) if default is None or role.id != default.id]
            if others:
                games[app_code] = others
    return games


def role_choices(app_code: str, roles: list[Role]) -> list[Button]:
    """这些角色各一个按钮，点一下设为默认。"""
    command = GAMES[app_code][1]
    return [(f"{number}.{nickname}"[:14], f"{command} {number}") for number, nickname in roles]


def game_offer(app_code: str, roles: list[Role]) -> tuple[str, Rows]:
    """某个游戏的角色，点一下就设为默认。"""
    return f"**切换{GAMES[app_code][0]}默认角色** 点一下设为默认", rows_fit(role_choices(app_code, roles))


def roles_in_words(app_code: str, games: dict[str, list[Role]]) -> str:
    """显示不了按钮时同样的选择：每个角色一行“序号. 昵称：指令”，另一个游戏也能切换时最后再给它的切换指令。"""
    name, command = GAMES[app_code]
    lines = [f"切换{name}默认角色，发送对应的指令：", *(f"{number}. {nickname}：{command} {number}" for number, nickname in games[app_code])]
    lines += [f"切换{other_name}角色：{other_command}" for other, (other_name, other_command) in GAMES.items() if other != app_code and other in games]
    return "\n".join(lines)


def role_offer(games: dict[str, list[Role]]) -> tuple[str, Rows] | None:
    """角色列表后面跟什么：每个游戏只有一个角色的成员什么都不跟；只有一个游戏可选时列出它的角色；否则先选游戏。"""
    if not games:
        return None
    if len(games) == 1:
        return game_offer(*next(iter(games.items())))
    # 不带序号的指令：用该游戏的角色来回答（offer_roles，来自 plugins/skland_shortcuts）
    return "**选择你要切换角色的游戏**", rows_of([(name, command) for app_code, (name, command) in GAMES.items() if app_code in games], per_row=2)


async def role_buttons(user_id: str) -> tuple[str, Rows] | None:
    async with get_session() as session:
        owner = await owner_id_of(session, user_id)
    return role_offer(await switchable(owner))


async def offer_roles(app_code: str, owner: int | None, bot: BaseBot, event: Event) -> bool:
    """对没指定角色的切换指令，列出该游戏可以切换的角色作答：按钮，另一个游戏也能切换时再加一个去那边的按钮；
    显示不了按钮（或按钮被拒）时是同样内容的文字（roles_in_words）。owner 是切换流程开头查到的成员 id。
    成员没有别的角色可切换、或查不了时返回 False（什么都不发）。"""
    try:
        games = await switchable(owner)
    except Exception as e:
        logger.warning(f"Skland buttons: roles not offered ({type(e).__name__}: {e})")
        return False
    if not games.get(app_code):
        return False
    title, rows = game_offer(app_code, games[app_code])
    # 另一个游戏也有角色可切换：最后一行给出它的切换指令（点了再列出那边的角色）
    rows += [[(f"切换{name}角色", command)] for other, (name, command) in GAMES.items() if other != app_code and other in games]
    return await send_buttons(title, rows, bot=bot, event=event, fallback=roles_in_words(app_code, games))


async def explain_switch(app_code: str, text: str, owner: int | None, bot: BaseBot, event: Event) -> bool:
    """切换做不了时的说明（plugins/skland_shortcuts），下面挂上另一个游戏可以切换的角色和“更新角色列表”。
    owner 同 offer_roles。机器人不显示按钮时返回 False（什么都不发）。"""
    if not can_show_buttons(bot):
        return False
    try:
        games = await switchable(owner)
    except Exception as e:
        logger.warning(f"Skland buttons: roles of the other game not offered ({type(e).__name__}: {e})")
        games = {}
    rows = [row for other, roles in games.items() if other != app_code for row in rows_fit(role_choices(other, roles))]
    # 无文字兜底：说明文字里已经写了要发的指令；没有按钮时由 plugins/skland_shortcuts 带 @ 发这段文字
    return await send_buttons(plain(text), [*rows, [("更新角色列表", "/skl角色更新")]], bot=bot, event=event)


async def buttons_for(result, user_id: str) -> tuple[str, Rows] | None:
    if result.find("rogue"):
        return rogue_buttons(result)
    if result.find("box"):
        return box_buttons(result)
    if result.find("char") and not result.find("char.set") and not result.find("char.update"):
        return await role_buttons(user_id)
    return None


@run_postprocessor
async def _offer_buttons(matcher: Matcher, bot: BaseBot, event: Event, exception: Exception | None) -> None:
    if id(matcher) not in _pictured:
        return
    _pictured.discard(id(matcher))
    if exception is not None or not can_show_buttons(bot):
        return
    result = getattr(matcher.state.get(ALCONNA_RESULT), "result", None)
    if result is None:
        return
    try:
        offer = await buttons_for(result, event.get_user_id())
    except Exception as e:  # 指令已经回答过了：没有按钮也不损失什么
        logger.warning(f"Skland buttons: none offered ({type(e).__name__}: {e})")
        return
    if offer is not None:
        # 无文字兜底：结果图后面的换主题、筛选和切换角色，指令帮助里都有，图后再跟一条文字会刷屏
        await send_buttons(*offer, bot=bot, event=event)
