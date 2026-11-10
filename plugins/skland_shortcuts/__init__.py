"""Idempotent project-local Skland shortcuts with protected administration."""

from __future__ import annotations

import os
import re
import shelve
from dataclasses import dataclass
from time import time_ns
from typing import Any

from sqlalchemy import select

from nonebot import get_driver, logger, on_command, on_message, require
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
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
from nonebot_plugin_user.models import Bind
from nonebot_plugin_user import UserSession
from nonebot_plugin_skland.commands.arksign import arksign_all_handler, arksign_status_handler
from nonebot_plugin_skland.commands.endfield.sign import ef_sign_all_handler, ef_sign_status_handler
from nonebot_plugin_skland.config import CACHE_DIR
from nonebot_plugin_skland.db_handler import get_accounts, get_default_character, get_user_characters
from nonebot_plugin_skland.matcher import skland, skland_command

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
    """A query and its `-r 序号` form (another role of the member, see /skl角色); nothing else may follow."""
    return ShortcutSpec(pattern, command), ShortcutSpec(pattern + ROLE, f"{command} -r {{1}}")


CUSTOM_SHORTCUTS = (
    ShortcutSpec(r"(森空岛|skl|skd)绑定", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)扫码", "skland qrcode"),
    ShortcutSpec(r"(森空岛|skl|skd)添加账号", "skland qrcode --add"),
    ShortcutSpec(r"(森空岛|skl|skd)token绑定", "skland bind", fuzzy=True),
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
    *_query(r"(明日方舟|方舟|mrfz)签到详情", "skland arksign status"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)签到", "skland arksign sign --all"),
    *_query(r"(明日方舟|方舟|mrfz)抽卡记录", "skland gacha"),
    ShortcutSpec(r"(明日方舟|方舟|mrfz)导入小黑盒", "skland import", fuzzy=True),
)

if any(not spec.prefix for spec in CUSTOM_SHORTCUTS):
    raise RuntimeError("All reviewed Skland shortcuts must require a command prefix")

CONFLICTING_BUILTINS = (
    "森空岛绑定",
    "森空岛解绑",
    "明日方舟签到",
    "方舟抽卡记录",
    "导入抽卡记录",  # /mrfz导入小黑盒 now, in line with /zmd导入小黑盒
    "终末地签到",
    "终末地签到详情",
    "终末地全体签到",
    "终末地全体签到详情",
    "终末地抽卡记录",
    r"(ef|zmd)",
    "战争回响",  # 0.7.2 maps it to its own record card; here /战争回响 is the rotation page of endfield_guide
    "森空岛角色",
    "全体签到",  # 2026-11-08: /全体签到 and /全体签到详情 cover both games, see sign_all below
    "全体签到详情",
)

# Shortcuts this plugin registered before; the shortcut cache keeps them, so they are removed by name.
# 2026-10-14: /zmd开盒 (with all, 卡片, 查询) is the full roster of plugins.endfield_roster now.
RETIRED_SHORTCUTS = (
    r"(终末地|zmd|ef)卡片",
    r"(终末地|zmd|ef)卡片" + ROLE,
    r"(终末地|zmd|ef)查询",
    r"(终末地|zmd|ef)查询" + ROLE,
    r"(终末地|zmd|ef)开盒 all",
    r"(终末地|zmd|ef)开盒",
    # 2026-10-27: /mrfz开盒 (with 卡片, 查询) is plugins.arknights_roster now.
    r"(明日方舟|方舟|mrfz)卡片",
    r"(明日方舟|方舟|mrfz)卡片" + ROLE,
    r"(明日方舟|方舟|mrfz)查询",
    r"(明日方舟|方舟|mrfz)查询" + ROLE,
    r"(明日方舟|方舟|mrfz)开盒",
    r"(明日方舟|方舟|mrfz)开盒" + ROLE,
    # 2026-11-08: the per-game sign-all commands are one /全体签到 (/全体签到详情) for both games.
    r"(终末地|zmd|ef)全体签到详情",
    r"(终末地|zmd|ef)全体签到",
    r"(明日方舟|方舟|mrfz)全体签到详情",
    r"(明日方舟|方舟|mrfz)全体签到",
)

_SHORTCUT_EXTENSION_ID = "builtins.extensions.shortcut:SuperUserShortcutExtension"
driver = get_driver()


# Upstream hints name raw option flags that users of the shortcuts never type; say the shortcut instead.
HINT_REWRITES = {
    "请先使用 -u 参数从接口拉取数据": "请先发送 /zmd抽卡记录更新 拉取数据",
    "临时选角: 查询、签到、状态及抽卡导入命令可追加 -r <序号>": "临时查看别的角色：在指令后面空一格加 -r 和角色序号，例如 /zmd开盒 -r 2（不改默认角色）",
    "切换默认角色: sk char set ark <序号> / sk char set ef <序号>": "切换默认角色：/skl切换方舟角色 序号、/skl切换终末地角色 序号",
    "请执行 sk char set ark <序号>": "请发送 /skl切换方舟角色 序号",
    "请执行 sk char set ef <序号>": "请发送 /skl切换终末地角色 序号",
    "请以最新 sk char 卡片为准": "请以 /skl角色 的最新卡片为准",
    "请先执行 sk char update": "请先发送 /skl角色更新",
    "请通过 sk unbind 移除异常项": "请发送 /skl解绑 移除异常的账号",
    "请使用 sk bind -u 更新": "请发送 /skl绑定 重新扫码更新",
    # every member with roles has a default here (skland_health.ensure_defaults), so this means "not bound"
    "目标用户尚未设置明日方舟默认角色": "对方还没有绑定明日方舟角色",
    "目标用户尚未设置终末地默认角色": "对方还没有绑定终末地角色",
    "二维码绑定将由本次命令发起者在角色列表中确认,有效时间约两分钟": "二维码只能由发指令的本人扫码，有效时间约两分钟",
    # upstream decorates its resource-update replies with emoji (written as escapes: no emoji in this repository)
    "\u2705 ": "",
    "\U0001f4e6 ": "",
    "\u274c ": "",
}


# Alconna's own replies (argument prompts and type errors) are written for programmers (owner, 2026-11-10:
# a member got ">> index: 请输入角色卡片中的序号" and then "参数 '算了' 不正确, 其应该符合 'int'").
def _shown(raw: str) -> str:
    """What the member typed, as Alconna quoted it (repr: '…' or "…"), kept short."""
    quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\""
    if not quoted and re.fullmatch(r"[A-Z]\w*\(.*\)", raw, re.S):  # a message element (At(target=…), Image(…)): no ids in the reply
        return "图片或@"
    text = raw[1:-1] if quoted else raw
    return text if len(text) <= 20 else text[:20] + "…"


AGAIN = "这次没有执行。请重新发送完整的指令"
PATTERN_REWRITES = (
    (re.compile(r"^>> [\w.-]+: (.+?)\s*$", re.S), r"\1，直接回复即可（1 分钟内有效）"),
    # whatever was typed where a number belongs, quoted any way (owner, 2026-11-10)
    (re.compile(r"参数 (.+?) 不正确, 其应该符合 '?int'?"), lambda m: f"「{_shown(m.group(1))}」不是数字，{AGAIN}，序号写数字"),
    (re.compile(r"参数 (.+?) 不正确, 其应该符合 [^\n]+"), lambda m: f"「{_shown(m.group(1))}」不符合要求，{AGAIN}"),
    (re.compile(r"参数 (.+?) 匹配失败"), lambda m: f"「{_shown(m.group(1))}」无法识别，{AGAIN}"),
    (re.compile(r"参数 \S+ 丢失|你应当传入更多的参数来完成解析!|子命令 \"[^\"]*\" 缺少必要参数"), f"指令缺少内容，{AGAIN}"),
    (re.compile(r"补全会话超时, 已自动退出"), "等待超时，已取消"),
    (re.compile(r"补全会话已退出"), "已取消"),
)


def _rewrite(text: str) -> str:
    for hint, replacement in HINT_REWRITES.items():
        text = text.replace(hint, replacement)
    for pattern, replacement in PATTERN_REWRITES:
        text = pattern.sub(replacement, text)
    return text


@OneBotV11Bot.on_calling_api
async def _rewrite_upstream_hints(bot, api: str, data: dict[str, Any]) -> None:
    if api not in ("send_msg", "send_group_msg", "send_private_msg"):
        return
    message = data.get("message")
    if isinstance(message, str):
        data["message"] = _rewrite(message)
        return
    segments = [message] if hasattr(message, "type") and hasattr(message, "data") else message or ()  # a lone MessageSegment is not iterable
    for segment in segments:
        text = getattr(segment, "data", {}).get("text") if getattr(segment, "type", "") == "text" else None
        if text:
            segment.data["text"] = _rewrite(text)


# /skl切换终末地角色 without a number opened Alconna's argument prompt, which then took the member's next
# message as the number. Without a plain number the usage is answered here and nothing waits.
SWITCH = re.compile(r"^\s*/\s*(?:森空岛|skl|skd)\s*切换\s*(终末地|zmd|ef|明日方舟|方舟|mrfz)\s*角色(.*)$", re.I | re.S)


def _switch_without_number(event: Event) -> bool:
    if event.get_type() != "message":
        return False
    match = SWITCH.match(event.get_plaintext())
    return bool(match) and not re.fullmatch(r"\s*\d+\s*", match.group(2))


switch_usage = on_message(rule=_switch_without_number, priority=0, block=True)


@switch_usage.handle()
async def _(event: Event) -> None:
    match = SWITCH.match(event.get_plaintext())
    game = "方舟" if match and match.group(1).lower() in ("明日方舟", "方舟", "mrfz") else "终末地"
    await switch_usage.finish(
        f"请在指令后面空一格写上角色序号，例如 /skl切换{game}角色 2。序号在 /skl角色 的卡片上，每个角色前面的数字就是",
        at_sender=True,
    )


def _shortcut_admin_is_protected() -> bool:
    return any(extension.id == _SHORTCUT_EXTENSION_ID for extension in skland.executor.extensions)


if not _shortcut_admin_is_protected():
    add_global_extension(SuperUserShortcutExtension)
if not _shortcut_admin_is_protected():
    raise RuntimeError("Skland shortcut administration protection could not be attached")


def _delete_if_present(key: str) -> None:
    if key in command_manager.get_shortcut(skland_command):
        skland_command.shortcut(key, delete=True)


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


# ── /全体签到, /全体签到详情 (owner 2026-11-08): one command signs, or shows, every bound role of both games ──

from plugins.strict_command import strict  # noqa: E402

sign_all = on_command("全体签到", rule=strict, permission=SUPERUSER, priority=5, block=True)
sign_all_status = on_command("全体签到详情", rule=strict, permission=SUPERUSER, priority=5, block=True)


@sign_all.handle()
async def _(bot: OneBotV11Bot, user_session: UserSession, session: async_scoped_session) -> None:
    await arksign_all_handler(user_session, session, bot, True)
    await ef_sign_all_handler(user_session, session, bot, True)


@sign_all_status.handle()
async def _(bot: OneBotV11Bot, user_session: UserSession, session: async_scoped_session) -> None:
    await arksign_status_handler(user_session, session, bot, True, is_superuser=True)
    await ef_sign_status_handler(user_session, session, bot, True, is_superuser=True)


# A member with two Skland accounts of which only one has the game: the game's numbers run over that account's
# roles alone, so "/skl切换终末地角色 2" (meant as "the second account") fails and "1" is the role in use already.
# Then say why (owner, 2026-11-10). Every other case is upstream's.
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
        owner_id = await session.scalar(select(Bind.bind_id).where(Bind.platform_id == str(event.get_user_id())))
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
            return False  # a real switch: upstream does it
        state["switch_note"] = {
            "app_code": app_code, "index": index, "accounts": len(accounts), "with_game": len(with_game),
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
        lines.append(f"序号只能填 1" + (f" 到 {note['roles']}" if note["roles"] > 1 else "") + "。")
    if note["roles"]:
        lines.append(f"如果想换的是{other}角色，请发送 /skl切换{other_short}角色 序号；刚在另一个账号建了{game}角色的话，先发送 /skl角色更新")
    return "\n".join(lines)


@switch_note.handle()
async def _(state: T_State) -> None:
    await switch_note.finish(switch_note_text(state["switch_note"]), at_sender=True)
