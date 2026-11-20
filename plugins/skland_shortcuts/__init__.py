"""可重复执行的本项目森空岛快捷指令，管理操作受保护。"""

from __future__ import annotations

import asyncio
import os
import re
import shelve
import sys
from dataclasses import dataclass
from time import time_ns
from typing import Any

from nonebot import get_driver, logger, on_command, on_message, require
from nonebot.adapters import Bot as AnyBot
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.exception import MockApiException
from nonebot.matcher import current_event
from nonebot.permission import SUPERUSER
from nonebot.typing import T_State
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_orm")
require("nonebot_plugin_user")
require("nonebot_plugin_skland")

from nonebot_plugin_alconna import add_global_extension, command_manager
from nonebot_plugin_alconna.builtins.extensions.shortcut import (
    SuperUserShortcutExtension,
)
from nonebot_plugin_orm import async_scoped_session, get_session
from nonebot_plugin_user import UserSession
from nonebot_plugin_skland.commands.arksign import arksign_status_handler
from nonebot_plugin_skland.commands.endfield.sign import ef_sign_status_handler
from nonebot_plugin_skland.config import CACHE_DIR
from nonebot_plugin_skland.db_handler import get_accounts, get_default_character, get_user_characters
from nonebot_plugin_skland.matcher import skland, skland_command
from nonebot_plugin_skland.utils.message import send_reaction

from plugins.skland_roles import owner_id_of

__plugin_meta__ = PluginMetadata(
    name="Skland project shortcuts",
    description="Registers reviewed Skland aliases idempotently and persists them.",
    usage="Loaded automatically.",
    type="application",
)


@dataclass(frozen=True)
class ShortcutSpec:
    pattern: str
    command: str
    fuzzy: bool = False
    prefix: bool = True


ROLE = r"\s+(?:-r|--role)\s*(\d+)"


def _query(pattern: str, command: str) -> tuple[ShortcutSpec, ShortcutSpec]:
    """一条查询及其 `-r 序号` 写法（成员的另一个角色，见 /skl角色）；后面不能再跟别的。"""
    return ShortcutSpec(pattern, command), ShortcutSpec(pattern + ROLE, f"{command} -r {{1}}")


CUSTOM_SHORTCUTS = (
    ShortcutSpec(r"(森空岛|skl|skd)绑定", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)扫码", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)添加账号", "skland qrcode --add"),
    ShortcutSpec(r"(森空岛|skl|skd)解绑", "skland unbind"),
    ShortcutSpec(r"(森空岛|skl|skd)全体角色更新", "skland char update --all"),
    ShortcutSpec(r"(森空岛|skl|skd)角色更新", "skland char update"),
    ShortcutSpec(r"(森空岛|skl|skd)角色", "skland char"),
    ShortcutSpec(r"(森空岛|skl|skd)切换(终末地|zmd|ef)角色", "skland char set ef", fuzzy=True),
    ShortcutSpec(r"(森空岛|skl|skd)切换(明日方舟|方舟|mrfz)角色", "skland char set ark", fuzzy=True),
    *_query(r"(终末地|zmd|ef)签到详情", "skland efsign status"),
    ShortcutSpec(r"(终末地|zmd|ef)签到", "skland efsign sign --all"),
    *_query(r"(终末地|zmd|ef)抽卡记录更新", "skland efgacha -u"),
    *_query(r"(终末地|zmd|ef)抽卡记录", "skland efgacha"),
    # 某一类卡池（plugins.skland_ef_theme.gacha.KINDS）：抽卡记录下面的按钮发的就是这个
    ShortcutSpec(r"(终末地|zmd|ef)抽卡记录\s*(限定池|武器池|其他卡池)", "skland efgacha -k {1}"),
    ShortcutSpec(r"(终末地|zmd|ef)抽卡记录\s*(限定池|武器池|其他卡池)" + ROLE, "skland efgacha -k {1} -r {2}"),
    *_query(r"(明日方舟|方舟|mrfz)签到详情", "skland arksign status"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)签到", "skland arksign sign --all"),
    *_query(r"(明日方舟|方舟|mrfz)抽卡记录", "skland gacha"),
    # 需要好几张图的记录中的一张（plugins.skland_ark_gacha）：抽卡记录下面的按钮发的就是这个
    ShortcutSpec(r"(明日方舟|方舟|mrfz)抽卡记录\s*第\s*(\d+)\s*页", "skland gacha -p {1}"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)抽卡记录\s*第\s*(\d+)\s*页" + ROLE, "skland gacha -p {1} -r {2}"),
)

if any(not spec.prefix for spec in CUSTOM_SHORTCUTS):
    raise RuntimeError("All reviewed Skland shortcuts must require a command prefix")

CONFLICTING_BUILTINS = (
    "卫戍战绩",  # /卫 和 /卫戍战绩 由 plugins.arknights_garrison 回答（上游的数据，本项目的图和中文选项）
    "卫",
    "森空岛绑定",
    "森空岛解绑",
    "明日方舟签到",
    "方舟抽卡记录",
    "导入抽卡记录",  # 现在是 /mrfz导入小黑盒，与 /zmd导入小黑盒 一致
    "终末地签到",
    "终末地签到详情",
    "终末地全体签到",
    "终末地全体签到详情",
    "终末地抽卡记录",
    r"(ef|zmd)",
    "战争回响",  # 0.7.2 把它映射到自己的战绩卡；这里 /战争回响 是 endfield_guide 的轮换页面
    "森空岛角色",
    "全体签到",  # /全体签到 和 /全体签到详情 覆盖两个游戏，见下面的 sign_all
    "全体签到详情",
    "签到详情",  # 上游只查明日方舟；/签到详情 改为两款游戏都显示，见下面的 sign_status
)

# 本插件以前注册过的快捷指令；快捷指令缓存会保留它们，所以按名称移除。
# /zmd开盒（含 all、卡片、查询）由 plugins.endfield_roster 提供。
RETIRED_SHORTCUTS = (
    r"(终末地|zmd|ef)卡片",
    r"(终末地|zmd|ef)卡片" + ROLE,
    r"(终末地|zmd|ef)查询",
    r"(终末地|zmd|ef)查询" + ROLE,
    r"(终末地|zmd|ef)开盒 all",
    r"(终末地|zmd|ef)开盒",
    # /mrfz开盒（含 卡片、查询）由 plugins.arknights_roster 提供。
    r"(明日方舟|方舟|mrfz)卡片",
    r"(明日方舟|方舟|mrfz)卡片" + ROLE,
    r"(明日方舟|方舟|mrfz)查询",
    r"(明日方舟|方舟|mrfz)查询" + ROLE,
    r"(明日方舟|方舟|mrfz)开盒",
    r"(明日方舟|方舟|mrfz)开盒" + ROLE,
    # 各游戏分开的全体签到指令：由一个 /全体签到（/全体签到详情）覆盖两个游戏。
    r"(终末地|zmd|ef)全体签到详情",
    r"(终末地|zmd|ef)全体签到",
    r"(明日方舟|方舟|mrfz)全体签到详情",
    r"(明日方舟|方舟|mrfz)全体签到",
    # 用凭据绑定已关闭，而且不回应（plugins.skland_bind_sign）：旧写法也不再是快捷指令。
    r"(森空岛|skl|skd)token绑定",
    # 明日方舟改为按小黑盒 ID 导入（plugins.heybox_import），不再转给上游按导出链接导入。
    r"(明日方舟|方舟|mrfz)导入小黑盒",
)

# 上游改过写法的快捷指令：新写法沿用原来的名称，旧写法的匹配规则就没了名称，按名称删不掉，
# 却一直留在快捷指令缓存里，照样接走消息（每次启动都会从缓存回来）。先给它一个临时名称，再按这个名称删除。
STALE_UPSTREAM_RULES = (
    "签到详情",  # 旧写法只查明日方舟，抢在下面的 sign_status 前面回答
    "资源更新",  # 旧写法连图片一起更新；上游现在的 /资源更新 只更新数据
)

_SHORTCUT_EXTENSION_ID = "builtins.extensions.shortcut:SuperUserShortcutExtension"
driver = get_driver()


RESCAN = "森空岛登录已失效，请发送 /skl绑定 重新扫码"

# 上游的提示里写的是原始选项参数，用快捷指令的人从来不打这些；改成说快捷指令。
HINT_REWRITES = {
    "临时选角: 查询、签到、状态及抽卡导入命令可追加 -r <序号>": "临时查看别的角色：在指令后面加 角色序号，例如 /zmd开盒 角色2（不改默认角色）",
    "切换默认角色: sk char set ark <序号> / sk char set ef <序号>": "切换默认角色：/skl切换方舟角色 序号、/skl切换终末地角色 序号",
    "请执行 sk char set ark <序号>": "请发送 /skl切换方舟角色 序号",
    "请执行 sk char set ef <序号>": "请发送 /skl切换终末地角色 序号",
    "请以最新 sk char 卡片为准": "请以 /skl角色 的最新卡片为准",
    "请先执行 sk char update": "请先发送 /skl角色更新",
    "请通过 sk unbind 移除异常项": "请发送 /skl解绑 移除异常的账号",
    "请使用 sk bind -u 更新": "请发送 /skl绑定 重新扫码更新",
    # 这里每个有角色的成员都有默认角色（skland_health.ensure_defaults），所以这句的意思是“未绑定”
    "目标用户尚未设置明日方舟默认角色": "对方还没有绑定明日方舟角色",
    "目标用户尚未设置终末地默认角色": "对方还没有绑定终末地角色",
    "二维码绑定将由本次命令发起者在角色列表中确认,有效时间约两分钟": "二维码只能由发指令的本人扫码，有效时间约两分钟",
    # 成员不知道 token 是什么（官方也没有给成员获取它的地方）：提示里不提它，只说重新扫码
    "当前角色所属账号未保存 token,请使用 token 或扫码更新该账号": RESCAN,
    "cred失效且账号未保存token": RESCAN,
    # 上游给资源更新的回复加了 emoji（写成转义序列：本仓库里不放 emoji）
    "\u2705 ": "",
    "\U0001f4e6 ": "",
    "\u274c ": "",
}


# Alconna 自己的回复（参数提示和类型错误）是写给程序员看的：
# 有成员先收到 ">> index: 请输入角色卡片中的序号"，接着又收到 "参数 '算了' 不正确, 其应该符合 'int'"。
def _shown(raw: str) -> str:
    """成员输入的内容，按 Alconna 引用的样子（repr：'…' 或 "…"），截短。"""
    quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\""
    if not quoted and re.fullmatch(r"[A-Z]\w*\(.*\)", raw, re.S):  # 消息元素（At(target=…)、Image(…)）：回复里不出现 id
        return "图片或@"
    text = raw[1:-1] if quoted else raw
    return text if len(text) <= 20 else text[:20] + "…"


AGAIN = "这次没有执行。请重新发送完整的指令"
PATTERN_REWRITES = (
    (re.compile(r"^>> [\w.-]+: (.+?)\s*$", re.S), r"\1，直接回复即可（1 分钟内有效）"),
    # 应该填数字的地方填了别的，不管用哪种引号
    (re.compile(r"参数 (.+?) 不正确, 其应该符合 '?int'?"), lambda m: f"「{_shown(m.group(1))}」不是数字，{AGAIN}，序号写数字"),
    (re.compile(r"参数 (.+?) 不正确, 其应该符合 [^\n]+"), lambda m: f"「{_shown(m.group(1))}」不符合要求，{AGAIN}"),
    (re.compile(r"参数 (.+?) 匹配失败"), lambda m: f"「{_shown(m.group(1))}」无法识别，{AGAIN}"),
    (re.compile(r"参数 \S+ 丢失|你应当传入更多的参数来完成解析!|子命令 \"[^\"]*\" 缺少必要参数"), f"指令缺少内容，{AGAIN}"),
    (re.compile(r"补全会话超时, 已自动退出"), "等待超时，已取消"),
    (re.compile(r"补全会话已退出"), "已取消"),
    # 上游接口报错里的 token（“接口请求失败,刷新token失败：…”、“绑定失败,错误信息:获取token失败：…”）
    (re.compile(r"(?:刷新token|使用token获得认证代码|获取role token)失败：[^\n]*"), RESCAN),
    (re.compile(r"获取token失败：[^\n]*"), "扫码登录没有成功，请重新发送 /skl绑定"),
)


# 上游放在角色列表下面、教人输入角色序号的两行。机器人在列表后面跟着按钮时（plugins/skland_buttons）
# 就去掉这两行：那里点一下就能切换，而 角色序号 的写法在帮助图片里有。
ROLE_LIST_LINES = (
    "临时选角: 查询、签到、状态及抽卡导入命令可追加 -r <序号>",
    "切换默认角色: sk char set ark <序号> / sk char set ef <序号>",
)


def _rewrite(text: str, taps: bool = False) -> str:
    if taps:
        for line in ROLE_LIST_LINES:
            text = text.replace("\n" + line, "").replace(line, "")
    for hint, replacement in HINT_REWRITES.items():
        text = text.replace(hint, replacement)
    for pattern, replacement in PATTERN_REWRITES:
        text = pattern.sub(replacement, text)
    return text


NOT_BOUND = ("你还没有绑定森空岛账号", "未绑定森空岛账号，请先发送 /skl绑定")  # 上游各指令和理智指令的说法
BIND_FIRST = "你还没有绑定森空岛账号，请先发送 /skl绑定 扫码绑定"  # 没有按钮时代替上游那句：说出下一步发什么
# 回复里叫成员接着发的、不用再写任何内容的指令：能挂按钮时，消息下面给它们各一个按钮
NEXT_STEPS = ("/skl绑定", "/skl添加账号", "/skl角色更新", "/zmd抽卡记录更新", "/mrfz抽卡记录更新")
_NEXT_STEP = re.compile("(" + "|".join(re.escape(c) for c in sorted(NEXT_STEPS, key=len, reverse=True)) + r")(?![\w-])")


_SENTENCE = re.compile(r"[^。；;！!？?\n]+")
NOT_NOW = ("不要", "别", "如果", "私聊")  # 句子里有这些词：不是叫他现在在这里发（“收到通知前请先不要发”）


def next_steps(text: str) -> list[str]:
    """文字里叫成员现在发的 NEXT_STEPS 指令，按出现的顺序，不重复：指令前面同一句里要有“发”，
    整句不能带 NOT_NOW 里的词。"""
    found = []
    for sentence in _SENTENCE.findall(text):
        if any(word in sentence for word in NOT_NOW):
            continue
        found += [m.group(1) for m in _NEXT_STEP.finditer(sentence) if "发" in sentence[: m.start()]]
    return list(dict.fromkeys(found))


# 上游在成员没有某个游戏的默认角色时，一律叫他切换默认角色。可大多数时候他根本没有这个游戏的角色
# （只在另一个森空岛账号上有，或者刚建还没更新），切换无从谈起：按他实际有没有角色来说。
NO_DEFAULT = re.compile(r"当前尚未设置(明日方舟|终末地)默认角色,请执行 sk char set (?:ark|ef) <序号>")


async def no_default_words(game: str, user_id: str | None) -> str:
    app_code = "arknights" if game == "明日方舟" else "endfield"
    roles = 0
    try:
        async with get_session() as session:
            owner_id = await owner_id_of(session, user_id)
            if owner_id is not None:
                roles = len(await get_user_characters(owner_id, app_code, session))
    except Exception as e:
        logger.info(f"Skland shortcuts: roles not counted for the no-default hint ({type(e).__name__})")
    if roles:
        return f"还没有设置{game}默认角色，请发送 /skl切换{'方舟' if app_code == 'arknights' else '终末地'}角色 序号"
    return (f"你绑定的森空岛账号里没有{game}角色。刚在游戏里建了角色的话，请发送 /skl角色更新；"
            f"{game}在另一个森空岛账号上的话，请发送 /skl添加账号 绑定那个账号")


@OneBotV11Bot.on_calling_api
async def _rewrite_upstream_hints(bot, api: str, data: dict[str, Any]) -> None:
    if api not in ("send_msg", "send_group_msg", "send_private_msg"):
        return
    message = data.get("message")
    taps = can_show_buttons(bot)
    if isinstance(message, str):
        data["message"] = BIND_FIRST if message.strip() == NOT_BOUND[0] else _rewrite(message, taps)
        return
    segments = [message] if hasattr(message, "type") and hasattr(message, "data") else message or ()  # 单独一个 MessageSegment 不能迭代
    emptied = []
    for segment in segments:
        text = getattr(segment, "data", {}).get("text") if getattr(segment, "type", "") == "text" else None
        if text and (missing := NO_DEFAULT.search(text)):
            words = await no_default_words(missing.group(1), getattr(current_event.get(None), "user_id", None))
            text = segment.data["text"] = NO_DEFAULT.sub(lambda _: words, text)
        if text:
            segment.data["text"] = _rewrite(text, taps)
            if taps and text.strip() and not segment.data["text"].strip():
                emptied.append(segment)  # 除了被去掉的那几行什么都没有：图片单独发
    for segment in emptied:
        if isinstance(message, list):
            message.remove(segment)
    # 叫成员接着发某条指令的纯文字回复（没绑定、要更新角色、要拉取抽卡记录……）：能挂按钮时同一句话
    # 下面带这些指令的按钮；发不出按钮就照原样发，只有没绑定的那句换成写出指令的 BIND_FIRST。
    words = "".join(s.data.get("text", "") for s in segments if s.type == "text")
    asker = getattr(current_event.get(None), "user_id", None)
    only_words = all(s.type == "text" or (s.type == "at" and str(s.data.get("qq")) == str(asker)) for s in segments)  # 只 @ 了提问的人
    steps = (["/skl绑定"] if words.strip() in NOT_BOUND else next_steps(words)) if taps and only_words else []
    if steps:
        asker = asker if data.get("group_id") else None  # 群里由桥接 @ 他（真正的 @）
        text = "你还没有绑定森空岛账号\n点击下面的按钮进行扫码绑定" if words.strip() in NOT_BOUND else plain(words.strip())
        try:
            result = await bot.call_api("send_card", group_id=data.get("group_id"), user_id=data.get("user_id"), mention=asker, text=text, rows=[[[c, c] for c in steps]], keep=True)
        except Exception as e:
            logger.info(f"Skland shortcuts: next-step buttons not sent ({type(e).__name__}); the plain reply is used")
        else:
            raise MockApiException(result)
    if only_words and words.strip() == NOT_BOUND[0]:
        for segment in segments:
            if segment.type == "text" and segment.data.get("text", "").strip() == NOT_BOUND[0]:
                segment.data["text"] = segment.data["text"].replace(NOT_BOUND[0], BIND_FIRST)


# 不带序号的 /skl切换终末地角色 会打开 Alconna 的参数提示，然后把成员的下一条消息当成序号。
# 没有给出纯数字时，这里直接回答用法，不等待任何输入。
SWITCH = re.compile(r"^\s*/\s*(?:(?:森空岛|skl|skd)\s*)?切换\s*(终末地|zmd|ef|明日方舟|方舟|mrfz)\s*角色(.*)$", re.I | re.S)  # 不带 skl 的上游写法也算


def _switch_without_number(event: Event) -> bool:
    if event.get_type() != "message":
        return False
    match = SWITCH.match(event.get_plaintext())
    return bool(match) and not re.fullmatch(r"\s*\d+\s*", match.group(2))


switch_usage = on_message(rule=_switch_without_number, priority=0, block=True)


@switch_usage.handle()
async def _(bot: AnyBot, event: Event) -> None:
    match = SWITCH.match(event.get_plaintext())
    game = "方舟" if match and match.group(1).lower() in ("明日方舟", "方舟", "mrfz") else "终末地"
    # 成员在该游戏有别的角色可切换时，列出这些角色（能显示按钮时是按钮，否则每个角色一行文字，
    # plugins/skland_buttons，加载了才有），而不是回答用法。
    app_code = "arknights" if game == "方舟" else "endfield"
    buttons = sys.modules.get("plugins.skland_buttons")
    if not match.group(2).strip():
        owner = await _owner_of(event.get_user_id())  # 按钮和说明都用这一次查到的成员
        if buttons is not None and await buttons.offer_roles(app_code, owner, bot, event):
            await switch_usage.finish()
        if (text := await _nothing_to_switch(owner, app_code)) is not None:
            if buttons is not None and await buttons.explain_switch(app_code, text, owner, bot, event):
                await switch_usage.finish()
            await switch_usage.finish(text, at_sender=True)
    await switch_usage.finish(
        f"请在指令后面空一格写上角色序号，例如 /skl切换{game}角色 2。序号在 /skl角色 的卡片上，每个角色前面的数字就是",
        at_sender=True,
    )


# 上游留下、我们不用的指令：完全不回应（不回答，也不交给上游）。排在上游的 matcher（优先级 1）之前。
#  - 群友手打的 /sk、/skland 原始写法（每个功能都有中文指令；中文指令和按钮在内部展开成原始写法，不受影响）；
#  - 对谁都关掉的：不带子指令的 /sk（上游旧角色卡，已由 /mrfz开盒 取代）、--help、garrison（/卫）、
#    efwar（/战争回响、/回响竞速）、efcard（/zmd开盒）、import（上游按导出链接导入；方舟抽卡改由 /mrfz导入小黑盒
#    按小黑盒 ID 导入，plugins.heybox_import）；
#  - 依赖插件自带的 /user、/bind（把两个账号合并成同一个用户，会共用森空岛绑定）、/argot；
#  - 只有超级用户能用的维护指令，别人发了也不回应（上游会回一句“仅超管可用”）。
RAW_UPSTREAM = re.compile(r"^\s*/\s*(?:skland|sk)(?=\s|$)\s*(\S*)", re.I)
CLOSED_FOR_ALL = ("", "-h", "--help", "garrison", "efwar", "efcard", "import")
OTHER_UPSTREAM = re.compile(r"^\s*/\s*(?:user|bind|argot)(?=\s|$)", re.I)
SUPERUSER_ONLY = re.compile(r"^\s*/\s*(?:资源更新|(?:森空岛|skl|skd)?全体角色更新)(?=\s|$)", re.I)


def closed_upstream(text: str, superuser: bool) -> bool:
    if OTHER_UPSTREAM.match(text):
        return True
    if raw := RAW_UPSTREAM.match(text):
        first = raw.group(1).lower()
        # 不带子指令的 /sk 也包括 /sk @某人、/sk -r 2、/sk 12345（都是上游的旧角色卡）
        return not superuser or first in CLOSED_FOR_ALL or first.startswith(("-r", "--role")) or first[:1].isdigit()
    return not superuser and SUPERUSER_ONLY.match(text) is not None


def _is_closed(event: Event) -> bool:
    if event.get_type() != "message":
        return False
    return closed_upstream(event.get_plaintext(), event.get_user_id() in get_driver().config.superusers)


closed = on_message(rule=_is_closed, priority=0, block=True)


@closed.handle()
async def _() -> None:
    logger.info("Skland shortcuts: an upstream command that is not offered was ignored")


def _shortcut_admin_is_protected() -> bool:
    return any(extension.id == _SHORTCUT_EXTENSION_ID for extension in skland.executor.extensions)


if not _shortcut_admin_is_protected():
    add_global_extension(SuperUserShortcutExtension)
if not _shortcut_admin_is_protected():
    raise RuntimeError("Skland shortcut administration protection could not be attached")


def _delete_if_present(key: str) -> None:
    if key in command_manager.get_shortcut(skland_command):
        skland_command.shortcut(key, delete=True)


def _drop_stale_rule(key: str) -> None:
    holder = f"旧规则 {key}"
    skland.shortcut(key, command="skland", prefix=True, humanized=holder)
    skland_command.shortcut(holder, delete=True)


def _secure_shortcut_cache() -> None:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CACHE_DIR, 0o700)
    for path in CACHE_DIR.glob("shortcut.db*"):
        if path.is_file():
            os.chmod(path, 0o600)


def _all_serialized_shortcuts_require_prefix(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("prefix") is False:
            return False
        return all(_all_serialized_shortcuts_require_prefix(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_all_serialized_shortcuts_require_prefix(item) for item in value)
    return True


def _ownerless_rules(payload: dict) -> list[str]:
    """没有名称的匹配规则，见 STALE_UPSTREAM_RULES；启动时还有就记一条警告。"""
    found = []
    for names, rules in payload.values():
        owned = set(names)
        for args in names.values():
            if isinstance(args, dict) and "origin_key" in args:
                owned.update(re.escape(prefix) + args["origin_key"] for prefix in args.get("prefixes") or [""])
        found += [key for key in rules if key not in owned]
    return found


def _persist_shortcut_cache() -> None:
    _secure_shortcut_cache()
    cache_base = CACHE_DIR / "shortcut.db"
    temporary_base = CACHE_DIR / f".shortcut.db.{os.getpid()}.{time_ns()}"
    try:
        command_manager.dump_cache(temporary_base, command=skland_command)
        with shelve.open(temporary_base.as_posix(), flag="r") as database:
            payload = database.get("shortcuts")
            if payload is None or not _all_serialized_shortcuts_require_prefix(payload):
                raise RuntimeError("Shortcut cache contains a command without the required prefix")
            if stale := _ownerless_rules(payload):
                logger.warning(f"Skland shortcut cache keeps {len(stale)} rules without a name: {stale}")

        temporary_files = sorted(CACHE_DIR.glob(f"{temporary_base.name}*"))
        if len(temporary_files) != 1:
            raise RuntimeError("Shortcut cache backend is not safely replaceable as one file")

        temporary_file = temporary_files[0]
        suffix = temporary_file.name.removeprefix(temporary_base.name)
        target_file = CACHE_DIR / f"{cache_base.name}{suffix}"
        os.chmod(temporary_file, 0o600)
        os.replace(temporary_file, target_file)
        for stale_file in CACHE_DIR.glob(f"{cache_base.name}*"):
            if stale_file.is_file() and stale_file != target_file:
                stale_file.unlink()
    finally:
        for temporary_file in CACHE_DIR.glob(f"{temporary_base.name}*"):
            if temporary_file.is_file():
                temporary_file.unlink()
    _secure_shortcut_cache()


@driver.on_startup
async def _configure_shortcuts() -> None:
    if not _shortcut_admin_is_protected():
        raise RuntimeError("Skland shortcut administration protection is not active")

    for spec in CUSTOM_SHORTCUTS:
        _delete_if_present(spec.pattern)
    for key in (*CONFLICTING_BUILTINS, *RETIRED_SHORTCUTS):
        _delete_if_present(key)
    for key in STALE_UPSTREAM_RULES:
        _drop_stale_rule(key)

    for spec in CUSTOM_SHORTCUTS:
        skland.shortcut(
            re.compile(spec.pattern),
            command=spec.command,
            fuzzy=spec.fuzzy,
            prefix=spec.prefix,
            humanized=spec.pattern,
        )

    current = command_manager.get_shortcut(skland_command)
    missing = [spec.pattern for spec in CUSTOM_SHORTCUTS if spec.pattern not in current]
    if missing:
        raise RuntimeError(f"Skland shortcut registration incomplete: {len(missing)} missing")

    _persist_shortcut_cache()
    logger.info(f"Configured {len(CUSTOM_SHORTCUTS)} reviewed Skland shortcuts")


# ── /全体签到、/全体签到详情：一条指令签到或查看两个游戏所有已绑定的角色 ──

from plugins.quick_buttons import can_show_buttons, plain  # noqa: E402
from plugins.strict_command import strict  # noqa: E402

sign_all = on_command("全体签到", rule=strict, permission=SUPERUSER, priority=5, block=True)
sign_all_status = on_command("全体签到详情", rule=strict, permission=SUPERUSER, priority=5, block=True)
sign_status = on_command("签到详情", rule=strict, priority=5, block=True)


@sign_all.handle()
async def _(bot: OneBotV11Bot, user_session: UserSession, session: async_scoped_session) -> None:
    """两个游戏同时签，用每晚签到那一套（plugins/skland_health：几个账号并行、签完一个保存一个、
    等重新绑定的成员不签），签完回复两份签到结果。"""
    from plugins import skland_health  # 在本插件之后加载

    send_reaction(user_session, "processing")
    await asyncio.gather(skland_health.run_daily_sign("arknights"), skland_health.run_daily_sign("endfield"))
    await arksign_status_handler(user_session, session, bot, True, is_superuser=True)
    await ef_sign_status_handler(user_session, session, bot, True, is_superuser=True)


@sign_all_status.handle()
async def _(bot: OneBotV11Bot, user_session: UserSession, session: async_scoped_session) -> None:
    await arksign_status_handler(user_session, session, bot, True, is_superuser=True)
    await ef_sign_status_handler(user_session, session, bot, True, is_superuser=True)


@sign_status.handle()
async def _(bot: OneBotV11Bot, user_session: UserSession, session: async_scoped_session) -> None:
    """自己的签到情况，两款游戏都显示（和 /全体签到 一样合在一起）。没绑定时只说一次。"""
    if not await get_accounts(user_session.user_id, session):
        await session.rollback()
        await sign_status.finish(NOT_BOUND[0], at_sender=True)
    await arksign_status_handler(user_session, session, bot, False, is_superuser=False)
    await session.refresh(user_session.user)  # 上游没查到结果时会回滚，成员对象随之过期：重新读一次，终末地那半才不会 MissingGreenlet
    await ef_sign_status_handler(user_session, session, bot, False, is_superuser=False)


# 有两个森空岛账号、但只有一个账号有该游戏的成员：该游戏的序号只在那个账号的角色里排，
# 所以“/skl切换终末地角色 2”（本意是“第二个账号”）会失败，而“1”就是正在用的角色。
# 这时说明原因。其他情况都交给上游。
GAMES = {"endfield": ("终末地", "明日方舟", "方舟"), "arknights": ("明日方舟", "终末地", "终末地")}


async def _switch_cannot_help(event: Event, state: T_State) -> bool:
    if event.get_type() != "message":
        return False
    match = SWITCH.match(event.get_plaintext())
    if not match or not re.fullmatch(r"\s*\d+\s*", match.group(2)):
        return False
    app_code = "arknights" if match.group(1).lower() in ("明日方舟", "方舟", "mrfz") else "endfield"
    index = int(match.group(2))
    async with get_session() as session:
        owner_id = await owner_id_of(session, event.get_user_id())
        if owner_id is None:
            return False
        accounts = await get_accounts(owner_id, session)
        roles = await get_user_characters(owner_id, app_code, session)
        with_game = {role.account_id for role in roles}
        if len(accounts) < 2 or len(with_game) >= len(accounts):
            return False
        target = roles[index - 1] if 1 <= index <= len(roles) else None
        current = await get_default_character(owner_id, app_code, session)
        if target is not None and (current is None or current.id != target.id):
            return False  # 真正要切换：交给上游去做
        state["switch_note"] = {
            "owner": owner_id, "app_code": app_code, "index": index, "accounts": len(accounts), "with_game": len(with_game),
            "roles": len(roles), "current": target.nickname if target is not None else "",
        }
    return True


switch_note = on_message(rule=_switch_cannot_help, priority=0, block=True)


def switch_note_text(note: dict) -> str:
    game, other, other_short = GAMES[note["app_code"]]
    lines = [f"你绑定了 {note['accounts']} 个森空岛账号，但只有 {note['with_game']} 个账号有{game}角色，所以{game}角色一共只有 {note['roles']} 个，序号是在这些角色里排的，不是账号的顺序。"]
    if note["roles"] == 0:
        lines = [f"你绑定的 {note['accounts']} 个森空岛账号里都没有{game}角色，没有可以切换的。如果刚创建了角色，请先发送 /skl角色更新"]
    elif note["current"]:
        lines.append(f"序号 {note['index']} 就是你现在在用的{game}角色（{note['current']}），不用切换。")
    else:
        lines.append("序号只能填 1" + (f" 到 {note['roles']}" if note["roles"] > 1 else "") + "。")
    if note["roles"]:
        lines.append(f"如果想换的是{other}角色，请发送 /skl切换{other_short}角色 序号；刚在另一个账号建了{game}角色的话，先发送 /skl角色更新")
    return "\n".join(lines)


async def _owner_of(user_id: str) -> int | None:
    """切换流程开头查一次成员 id；查不了就当没绑定：回答写序号的用法，总比不回答好。"""
    try:
        async with get_session() as session:
            return await owner_id_of(session, user_id)
    except Exception as e:
        logger.info(f"Skland shortcuts: member not looked up for the switch hint ({type(e).__name__})")
        return None


async def _nothing_to_switch(owner_id: int | None, app_code: str) -> str | None:
    """成员在这个游戏只有一个角色（或没有）时，说明不用切换；有可以切换的角色、或没绑定（None）时为 None。"""
    if owner_id is None:
        return None
    try:
        async with get_session() as session:
            roles = await get_user_characters(owner_id, app_code, session)
    except Exception as e:  # 查不了就当不知道：回答写序号的用法，总比不回答好
        logger.info(f"Skland shortcuts: roles not counted for the switch hint ({type(e).__name__})")
        return None
    if len(roles) > 1:
        return None
    game, other, other_short = GAMES[app_code]
    if not roles:
        return f"你绑定的森空岛账号里没有{game}角色，没有可以切换的。如果刚创建了角色，请先发送 /skl角色更新"
    return (f"你只有 1 个{game}角色（{roles[0].nickname}），就是你现在在用的，不用切换。\n"
            f"如果想换的是{other}角色，请发送 /skl切换{other_short}角色 序号；刚在另一个账号建了{game}角色的话，先发送 /skl角色更新")


@switch_note.handle()
async def _(bot: AnyBot, event: Event, state: T_State) -> None:
    note = state["switch_note"]
    text = switch_note_text(note)
    buttons = sys.modules.get("plugins.skland_buttons")
    if buttons is not None and await buttons.explain_switch(note["app_code"], text, note["owner"], bot, event):
        await switch_note.finish()
    await switch_note.finish(text, at_sender=True)
