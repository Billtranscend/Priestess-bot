"""在能显示按钮的机器人上，给回复下面挂按钮；显示不了按钮的机器人上改发文字。

官方机器人可以在消息下面挂几行按钮；点一下就以群友本人的名义发出一条指令（而且是真正 @ 了机器人，
所以在任何群都能用）。OneBot 机器人做不到，由 `fallback` 给出的文字代替：

    await send_buttons("竞速榜 · 请选择", [[("本周回响", "/回响竞速")], [("幽影刻形", "/丰碑竞速 幽影刻形")]], fallback=TEXT_MENU)
    await matcher.finish()

图片后面附带的下一步用 fallback=True：显示不了按钮时把这些指令写成一行“还可以发：/a、/b”。
每个 send_buttons 调用要么带 fallback，要么在上一行写明“# 无文字兜底：<原因>”（tests/test_button_fallbacks.py 检查）。

一条消息最多五行、每行五个按钮，而一行超过三个在手机上就不好看了。放不下的行接着发第二、第三条
消息（"标题（2/3）"）；连这样都放不下的选项就不用按钮了，改发 fallback 的文字，它没有这种限制。
`rows_of` 把一串按钮排成行；`rows_fit` 用于标签长短不一的情况（称号），给长标签留更多位置。

菜单由这里统一撤回（`_track_menus`）：按钮消息，以及显示不了按钮的机器人在群里发的文字菜单
（`send_menu`，fallback 的文字也走它）。提问的人发出了其中一条指令（做出了选择），回答发出后就撤回；
群里没人理会的，MENU_IDLE_SECONDS 后撤回。调用方不用自己记。

在私聊里，同一个机器人还可以在消息下方放推荐指令，每条显示的就是点一下发出的文字
（`send_hints`）：用于对某人说的第一句话，也就是欢迎语。

这里不导入官方机器人的桥接插件（只有配置了官方机器人时才加载，见 bot.py）：而是在已加载的模块里查找，
找不到就当作显示不了按钮的机器人。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from nonebot import logger
from nonebot.adapters import Bot as AnyBot
from nonebot.matcher import current_bot, current_event

Button = tuple[str, str]  # 按钮上的文字，点一下发送的指令
Rows = list[list[Button]]
ROWS_PER_MESSAGE = 5  # 平台限制
MESSAGES_AT_MOST = 3  # 一条回复后面最多跟这么多条按钮消息；更长的选项交给文字菜单
PER_ROW = 3
LABEL_FITS = {2: 9, 3: 5}  # 一行的按钮数 -> 这一行在手机上还能完整显示的最长标签字数（估计值）
IN_WORDS = "还可以发："  # fallback=True 时那一行文字的开头


def rows_of(buttons: list[Button], per_row: int = PER_ROW) -> Rows:
    """按给定顺序把按钮排成每行 `per_row` 个。"""
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def rows_fit(buttons: list[Button]) -> Rows:
    """按给定顺序、按标签长度排行：短的一行三个，稍长的一行两个，很长的独占一行。"""
    rows: Rows = []
    for button in buttons:
        row = rows[-1] if rows else None
        if row is not None and len(row) < PER_ROW and max(len(label) for label, _ in [*row, button]) <= LABEL_FITS[len(row) + 1]:
            row.append(button)
        else:
            rows.append([button])
    return rows


def pages_of(rows: Rows) -> list[Rows] | None:
    """把各行切分成几条消息；超过一条回复后允许跟的数量时返回 None。"""
    rows = [row[:5] for row in rows if row]
    pages = [rows[i : i + ROWS_PER_MESSAGE] for i in range(0, len(rows), ROWS_PER_MESSAGE)]
    return pages if 0 < len(pages) <= MESSAGES_AT_MOST else None


def is_official_bot(bot=None) -> bool:
    """这个机器人（默认是正在回复指令的那个）是否为经 plugins/official_bridge 接入的官方机器人。"""
    bridge = sys.modules.get("plugins.official_bridge")
    try:
        return bridge is not None and isinstance(bot if bot is not None else current_bot.get(), bridge.BridgeBot)
    except LookupError:
        return False


def plain(text: str) -> str:
    """普通文字放进按钮消息（按 markdown 显示）时照原样显示：用桥接的转义；没加载桥接时原样返回。"""
    bridge = sys.modules.get("plugins.official_bridge.bridge")
    return bridge.markdown_escaped(text) if bridge is not None else text


def can_show_buttons(bot=None) -> bool:
    """这个机器人（默认是正在回复指令的那个）能否显示按钮？用于事先决定发什么。"""
    return is_official_bot(bot)


def _spaced(text: str) -> str:
    """空白规整成一个空格：按钮或菜单里的指令与成员发的文字按这个形式比较。"""
    return " ".join(str(text).split())


def _commands_in(rows: Rows) -> list[str]:
    """各按钮发出的指令，按顺序、去掉重复。"""
    return [command for command in dict.fromkeys(_spaced(command) for row in rows for _, command in row) if command]


@contextmanager
def _answering(bot, event) -> Iterator[None]:
    """机器人根据当前指令判断在回复谁：把上下文设好，指令之外（钩子里）调用时也一样。"""
    tokens = current_bot.set(bot), current_event.set(event)
    try:
        yield
    finally:
        current_bot.reset(tokens[0])
        current_event.reset(tokens[1])


async def send_buttons(title: str | list[str], rows: Rows, *, bot=None, event=None, sent: list | None = None, fallback: str | bool = False) -> bool:
    """向正在回复的会话发送 `title`，下面挂上按钮。什么都没发出时返回 False。

    按钮要分几条消息时，`title` 可以是每条消息各自的文字（条数与 pages_of(rows) 相同），
    否则每条都用同一段文字，后面标上第几条。

    `fallback` 是按钮发不出时的文字，按文字菜单发出（见 send_menu），发出了也返回 True：
    - 一段文字：按钮没发出就发这段文字（代替调用方自己的文字菜单），发送失败照常抛出；
    - True：显示不了按钮的机器人上把各按钮的指令写成一行“还可以发：/a、/b”。官方机器人的按钮被拒时
      照旧什么都不发：这只是图后附带的提示。

    `bot` 和 `event` 供指令结束后运行的钩子使用（那时指令的上下文已经没了）。
    给了 `sent` 的话，每条发出的按钮消息的 id 都会加进去（以便之后撤回）。
    """
    try:
        bot = bot if bot is not None else current_bot.get()
        event = event if event is not None else current_event.get()
    except LookupError:
        return False
    if await _send_cards(title, rows, bot, event, sent):
        return True
    return await _send_in_words(fallback, rows, bot, event)


async def _send_cards(title: str | list[str], rows: Rows, bot, event, sent: list | None) -> bool:
    """发按钮消息（规则见 send_buttons）；一条都没发出时返回 False。"""
    pages = pages_of(rows)
    if not can_show_buttons(bot) or pages is None:
        return False
    group_id, user_id = getattr(event, "group_id", None), getattr(event, "user_id", None)
    with _answering(bot, event):
        for number, page in enumerate(pages, 1):
            if isinstance(title, list):
                text = title[number - 1] if len(title) == len(pages) else title[0]
            else:
                text = title if len(pages) == 1 else f"{title}（{number}/{len(pages)}）"
            try:
                answer = await bot.call_api("send_card", group_id=group_id, user_id=user_id, text=text, rows=[[list(button) for button in row] for row in page])
            except Exception as e:  # 按钮只是附加的：由纯文字代替
                logger.info(f"Buttons not sent ({type(e).__name__})" + ("; the plain text is used" if number == 1 else f"; {number - 1} of {len(pages)} messages of them went out"))
                return number > 1
            if sent is not None and isinstance(answer, dict) and answer.get("message_id") is not None:
                sent.append(answer["message_id"])
    return True


async def _send_in_words(fallback: str | bool, rows: Rows, bot, event) -> bool:
    """按钮没发出时的文字（规则见 send_buttons）。"""
    commands = _commands_in(rows)
    if isinstance(fallback, str):
        return bool(fallback) and await send_menu(fallback, commands, bot=bot, event=event)
    if not fallback or not commands or can_show_buttons(bot):
        return False
    try:
        return await send_menu(IN_WORDS + "、".join(commands), commands, bot=bot, event=event)
    except Exception as e:  # 只是附加的提示：回答已经发出了
        logger.info(f"Commands in words not sent ({type(e).__name__})")
        return False


MENU_RECALL_SECONDS = 300  # 菜单在这段时间内被做出选择时撤回；更早的留着
MENU_IDLE_SECONDS = 90  # 群里回答指令的菜单，没人理会这么久就撤回（平台只允许撤回 2 分钟内的消息）
Chat = tuple[str, int]  # ("group", 群号) 或 ("private", QQ 号)


@dataclass
class Menu:
    """回答同一条消息时发出的菜单：按钮消息（分几条发的也算一个菜单），或显示不了按钮的机器人上的文字菜单。"""

    answering: int  # 所回答的那条消息的 id
    asker: str  # 发那条消息的人：只有他做出的选择才撤回这个菜单
    sent_at: float
    ids: list[int] = field(default_factory=list)
    commands: set[str] = field(default_factory=set)  # 菜单里的指令：按钮点一下发出的，或文字菜单列出的


_menus: dict[Chat, list[Menu]] = {}  # 还挂着的菜单
_gone: deque[int] = deque(maxlen=500)  # 已经撤回（或试过撤回）的菜单消息，不再撤第二次
_timers: set[asyncio.Task] = set()
_text_menu: ContextVar[frozenset[str] | None] = ContextVar("text_menu", default=None)  # send_menu 正在发的、要登记的文字菜单里的指令


def _chat_of(event) -> Chat | None:
    """消息所在的会话。群临时会话是带着群号的私聊消息，回复也发到私聊，算私聊。"""
    group, user = getattr(event, "group_id", None), getattr(event, "user_id", None)
    if getattr(event, "message_type", None) == "private":
        group = None
    try:
        return ("group", int(group)) if group else ("private", int(user)) if user else None
    except (TypeError, ValueError):
        return None


def said(event) -> str:
    """消息里的文字，空白规整成一个空格：与菜单里的指令比较用（取预处理改写之前的原文）。"""
    message = getattr(event, "original_message", None) or event.get_message()
    return _spaced(message.extract_plain_text())


async def _recall(bot, message_ids: list[int]) -> None:
    for message_id in message_ids:
        if message_id in _gone:
            continue
        _gone.append(message_id)
        try:
            await bot.call_api("delete_msg", message_id=message_id)
        except Exception as e:  # 例如已超过平台允许机器人撤回自己消息的时限
            logger.info(f"A menu message not recalled ({type(e).__name__})")


async def _recall_when_idle(bot, message_id: int) -> None:
    await asyncio.sleep(MENU_IDLE_SECONDS)
    await _recall(bot, [message_id])


def _offered(api: str, data: dict) -> set[str] | frozenset[str] | None:
    """这次发出的若是菜单，返回菜单里的指令；不是菜单时返回 None。"""
    if api == "send_card":
        return None if data.get("keep") else {_spaced(button[1]) for row in data.get("rows") or [] for button in row}
    return _text_menu.get()


@AnyBot.on_called_api
async def _track_menus(bot, exception: Exception | None, api: str, data: dict, result) -> None:
    """所有插件的菜单都在这里管：回答成员消息时发出的记下来；之后提问的人发出了其中某条指令
    （做出了选择），回答它的第一条消息发出后就撤回那个菜单（别人发了不撤：可能是他自己打的同一条指令）。
    群里没人理会的，MENU_IDLE_SECONDS 后撤回。不是在回答成员的消息（如进群欢迎）时发的按钮留着；
    调用时带 keep=True 的按钮消息（说明性的回复，下面附上要发的指令）也留着，不算菜单。
    文字菜单由 send_menu 交来（_text_menu）：只有显示不了按钮的机器人在群里发的才算。"""
    event = current_event.get(None)
    if exception is not None or not api.startswith("send_") or getattr(event, "post_type", None) != "message":
        return
    chat = _chat_of(event)
    if chat is None:
        return
    answering = int(getattr(event, "message_id", 0) or id(event))
    asker = str(getattr(event, "user_id", ""))
    now = time.monotonic()
    menus = [m for m in _menus.get(chat, []) if now - m.sent_at <= MENU_RECALL_SECONDS]
    text = said(event)
    chosen = [m for m in menus if m.answering != answering and m.asker == asker and text in m.commands]
    menus = [m for m in menus if m not in chosen]
    offered = _offered(api, data)
    if offered is not None and isinstance(result, dict) and result.get("message_id") is not None:
        message_id = int(result["message_id"])
        menu = next((m for m in menus if m.answering == answering), None)
        if menu is None:
            menu = Menu(answering, asker, now)
            menus.append(menu)
        menu.ids.append(message_id)
        menu.commands |= offered
        if chat[0] == "group":
            task = asyncio.create_task(_recall_when_idle(bot, message_id))
            _timers.add(task)
            task.add_done_callback(_timers.discard)
    if menus:
        _menus[chat] = menus
    else:
        _menus.pop(chat, None)
    for menu in chosen:  # 在后台撤：不耽误正在发送的回答（例如图片之后等着带 @ 的按钮消息）
        task = asyncio.create_task(_recall(bot, menu.ids))
        _timers.add(task)
        task.add_done_callback(_timers.discard)


async def send_menu(text: str, commands: Iterable[str] = (), *, bot=None, event=None) -> bool:
    """向正在回复的会话发送文字菜单 `text`（列出可以发的指令），用来代替 matcher.send(text)。

    显示不了按钮的机器人在群里发的，和按钮菜单一样撤回：提问的人发出了 `commands` 中的一条
    （整句相同；带占位的写法对不上，就只剩到时撤回），回答发出后撤回；没人理会的 MENU_IDLE_SECONDS 后撤回。
    能显示按钮的机器人上（那里文字只在按钮发不出时用）和私聊里（群临时会话也是）只是发出，不撤回。
    没有正在回复的会话时返回 False；发送失败照常抛出。
    """
    try:
        bot = bot if bot is not None else current_bot.get()
        event = event if event is not None else current_event.get()
    except LookupError:
        return False
    chat = _chat_of(event)
    tracked = chat is not None and chat[0] == "group" and not can_show_buttons(bot)
    token = _text_menu.set(frozenset(filter(None, map(_spaced, commands))) if tracked else None)
    try:
        with _answering(bot, event):
            await bot.send(event, text)
    finally:
        _text_menu.reset(token)
    return True


async def send_hints(text: str, commands: list[str], *, bot=None, event=None) -> bool:
    """向当前私聊发送 `text`，下面列出点一下就能发送的指令。做不到时返回 False（什么都不发）。

    推荐指令显示的就是它发送的文字，每行一条，所以每条都必须是完整的指令。
    仅限私聊。没有纯文字替代：显示不了时由调用方决定要不要说点什么。
    """
    try:
        bot = bot if bot is not None else current_bot.get()
        event = event if event is not None else current_event.get()
    except LookupError:
        return False
    wanted = [command for command in commands if command]
    user_id = getattr(event, "user_id", None)
    if not can_show_buttons(bot) or not wanted or user_id is None or getattr(event, "group_id", None) is not None:
        return False
    try:
        await bot.call_api("send_hints", user_id=user_id, text=text, commands=wanted)
    except Exception as e:
        logger.info(f"Suggested commands not sent ({type(e).__name__})")
        return False
    return True
