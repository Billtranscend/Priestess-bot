"""明日方舟和终末地的理智状态，以及理智满时的群提醒。

/理智              查看两款游戏当前的理智
/理智提醒 开|关     在本群订阅；理智满时机器人在这里 @ 你

后台任务每 10 分钟跑一次，但只有当记录的回满时间已到，或上次检查已超过 REFRESH_SECONDS 时，
才会请求森空岛。只有重新检查确认理智已满才发提醒，每个回满周期只发一次。

机器人不在某个群了，发往那里的提醒删掉：收到通知时按 plugins.shared.left_for_sure 确认，
另外每天按群列表补清一次（漏掉的通知、已解散的群）。

配了官方 QQ 机器人（QQ_BOTS）时只有 /理智：提醒、它的后台检查和退群清理都不注册，/理智提醒 不是指令。
官方机器人只能在打开了“机器人主动在群聊内发言”的群里主动发消息，提醒在很多群里发不出去。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass

from nonebot import get_driver, logger, on_command, on_notice, require
from nonebot.adapters import Bot, Message
from nonebot.adapters.onebot.v11 import GroupDecreaseNoticeEvent, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import Rule
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_skland")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_user")

import nonebot_plugin_localstore as store
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.exception import LoginException, RequestException, UnauthorizedException
from nonebot_plugin_skland.model import Character, SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.schemas.arknights.card import ArkCard
from nonebot_plugin_skland.schemas.endfield.card import EndfieldCard
from nonebot_plugin_skland.services.auth import refresh_credentials

from plugins.skland_roles import default_role, owner_id_of
from sqlalchemy import select
from plugins.shared import REACTION_DONE, bot_left_group, first_bot, groups_left, left_for_sure, write_json, react as _react

__plugin_meta__ = PluginMetadata(
    name="Sanity reminder",
    description="Shows Arknights/Endfield sanity and @s subscribers in their group when it is full.",
    usage="/理智 | /理智提醒 开 | /理智提醒 关",
    type="application",
)

GAMES = {"arknights": "明日方舟", "endfield": "终末地"}
CHECK_MINUTES = 10
REFRESH_SECONDS = 3 * 3600
DUE_MARGIN = 120  # 在预计回满时间之前这么多秒重新检查
REQUEST_GAP = 2.0
SUBS_FILE = store.get_plugin_data_file("subscriptions.json")

OFFICIAL_BOT = bool(getattr(get_driver().config, "qq_bots", None))  # 和 bot.py 的判断相同

sanity = on_command("理智", rule=strict, priority=5, block=True)
_lock = asyncio.Lock()


@dataclass
class Sanity:
    current: int
    maximum: int
    full_ts: float

    @property
    def is_full(self) -> bool:
        return self.current >= self.maximum or time.time() >= self.full_ts


def _load() -> dict:
    if not SUBS_FILE.exists():
        return {}
    return json.loads(SUBS_FILE.read_text("utf-8"))


def _save(subs: dict) -> None:
    write_json(SUBS_FILE, subs, ensure_ascii=False)


@refresh_credentials
async def _ark_card(user: SkUser, uid: str) -> ArkCard:
    return await SklandAPI.ark_card(CRED(cred=user.cred, token=user.cred_token), uid)


@refresh_credentials
async def _endfield_card(user: SkUser, char: Character) -> EndfieldCard:
    return await SklandAPI.endfield_card(
        CRED(cred=user.cred, token=user.cred_token), user_id=user.skland_user_id, role_id=char.role_id, server_id=char.channel_master_id
    )


async def _fetch(session, owner_id: int, game: str) -> Sanity | None:
    """成员在某个游戏里默认角色的理智（None：没有该角色）。"""
    selected = await default_role(session, owner_id, game)
    if selected is None:
        return None
    user, char = selected
    if game == "arknights":
        ap = (await _ark_card(user, str(char.uid))).status.ap
        return Sanity(ap.ap_now if ap.current < ap.max else ap.current, ap.max, float(ap.completeRecoveryTime))
    dungeon = (await _endfield_card(user, char)).dungeon
    cur, mx = int(dungeon.curStamina or 0), int(dungeon.maxStamina or 0)
    if mx <= 0 or not dungeon.maxTs:  # 森空岛没给上限或回满时间：说明不了什么，当作这次没读到（不能报“已回满”）
        return None
    return Sanity(cur, mx, float(dungeon.maxTs))


async def _member_for_qq(session, qq: str) -> int | None:
    """绑定了至少一个森空岛账号的 QQ 对应的成员 id。"""
    owner_id = await owner_id_of(session, qq)
    if owner_id is None or (await session.scalars(select(SkUser.id).where(SkUser.owner_id == owner_id).limit(1))).first() is None:
        return None
    return owner_id


def _remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}小时{seconds % 3600 // 60}分"




@sanity.handle()
async def _(event: MessageEvent) -> None:
    lines = []
    async with get_session() as session:
        user = await _member_for_qq(session, str(event.user_id))
        if user is None:
            await sanity.finish("未绑定森空岛账号，请先发送 /skl绑定", at_sender=True)
        for game, label in GAMES.items():
            try:
                state = await _fetch(session, user, game)
            except (RequestException, LoginException, UnauthorizedException) as e:
                lines.append(f"{label}：查询失败（{e.args[0] if e.args else type(e).__name__}）")
                continue
            if state is None:
                continue
            note = "已回满" if state.is_full else f"约 {_remaining(state.full_ts - time.time())}后回满"
            lines.append(f"{label}：理智 {min(state.current, state.maximum)}/{state.maximum}，{note}")
        await session.commit()
    await sanity.finish("\n".join(lines) or "没有找到已绑定的明日方舟或终末地角色", at_sender=True)


UNPROMPTED_REFUSED = 40_034_105  # 平台：群里没开“机器人主动在群聊内发言”
REFUSED = "本群没有开启“机器人主动在群聊内发言”，回满时的提醒发不进来，所以没有开启。可以随时发 /理智 查看"


def _refuses_unprompted(group_id: int) -> bool:
    """官方机器人在这个群发过主动消息并被平台拒绝过（桥接记得）。没有桥接或不知道时为 False。"""
    bridge = sys.modules.get("plugins.official_bridge")  # 只在配置了官方机器人的地方才有；绝不从这里导入
    check = getattr(bridge, "refuses_unprompted", None)
    try:
        return bool(check(group_id)) if check else False
    except Exception:
        return False


async def _subscribe(event: MessageEvent, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    qq = str(event.user_id)
    if action not in ("开", "关"):
        await _reminder.finish("用法：/理智提醒 开  或  /理智提醒 关")

    async with _lock:
        subs = _load()
        if action == "关":
            removed = subs.pop(qq, None)
            _save(subs)
            await _react(REACTION_DONE)
            await _reminder.finish("已关闭理智回满提醒" if removed else "你还没有开启理智提醒", at_sender=True)

        if not isinstance(event, GroupMessageEvent):
            await _reminder.finish("请在群里发送 /理智提醒 开，回满时会在该群 @你")
        if _refuses_unprompted(event.group_id):
            await _reminder.finish(REFUSED, at_sender=True)
        async with get_session() as session:
            user = await _member_for_qq(session, qq)
            if user is None:
                await _reminder.finish("未绑定森空岛账号，请先发送 /skl绑定", at_sender=True)
            games = [g for g in GAMES if await default_role(session, user, g) is not None]
        if not games:
            await _reminder.finish("没有找到已绑定的明日方舟或终末地角色", at_sender=True)
        subs[qq] = {"group": event.group_id, "games": {g: {"full_ts": None, "checked": 0, "notified": False} for g in games}}
        _save(subs)
    await _react(REACTION_DONE)
    names = "、".join(GAMES[g] for g in games)
    await _reminder.finish(f"已开启理智回满提醒（{names}），回满时会在本群 @你。关闭请发 /理智提醒 关", at_sender=True)


async def _check() -> None:
    if _lock.locked():
        return
    async with _lock:
        subs = _load()
        if not subs:
            return
        changed = False
        async with get_session() as session:
            for qq, sub in subs.items():
                for game, st in sub["games"].items():
                    now = time.time()
                    due = st["full_ts"] is not None and now >= st["full_ts"] - DUE_MARGIN
                    if not (due or now - st["checked"] >= REFRESH_SECONDS):
                        continue
                    try:
                        user = await _member_for_qq(session, qq)
                        state = await _fetch(session, user, game) if user is not None else None
                        await session.commit()  # 刷新到的 token 立刻提交：整个循环都持有写锁会挡住其他写操作
                    except Exception as e:
                        logger.warning(f"Sanity check failed for one {game} subscriber: {type(e).__name__}")
                        st["checked"] = now
                        changed = True
                        continue
                    finally:
                        await asyncio.sleep(REQUEST_GAP)
                    st["checked"], changed = now, True
                    if state is None:
                        continue
                    if not state.is_full:
                        st["full_ts"], st["notified"] = state.full_ts, False
                        continue
                    st["full_ts"] = None
                    if st["notified"]:
                        continue
                    bot = first_bot()
                    if bot is None:
                        continue
                    try:
                        await bot.send_group_msg(
                            group_id=sub["group"],
                            message=MessageSegment.at(int(qq))
                            + f" 你的{GAMES[game]}理智已回满（{state.maximum}/{state.maximum}），记得上线使用～",
                        )
                        st["notified"] = True
                    except Exception as e:
                        if getattr(e, "retcode", None) == UNPROMPTED_REFUSED:
                            st["notified"] = True  # 这个群不让机器人主动发言：重试也发不出去，等下次回满再说
                            logger.info("Sanity reminder not delivered: the group does not let the bot speak unprompted")
                        else:
                            logger.warning(f"Sanity reminder send failed: {type(e).__name__}")
            await session.commit()
        if changed:
            _save(subs)


async def _forget_groups(groups: set[int]) -> None:
    """机器人不在这些群了：发往那里的回满提醒没处可发，订阅删掉。"""
    gone = {str(group) for group in groups}
    async with _lock:
        subs = _load()
        kept = {qq: sub for qq, sub in subs.items() if str(sub.get("group")) not in gone}
        if len(kept) != len(subs):
            _save(kept)
            logger.info(f"Sanity reminder: {len(subs) - len(kept)} subscriptions to a group the bot left were removed")


async def _forget_group(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """收到机器人不在群了的通知：确认之后才删。"""
    if await left_for_sure(bot, event.group_id):
        await _forget_groups({event.group_id})


async def _recheck_groups() -> None:
    """每天补清一次：订阅里机器人已经不在的群。"""
    if gone := await groups_left({int(sub["group"]) for sub in _load().values()}):
        await _forget_groups(gone)


if not OFFICIAL_BOT:
    _reminder = on_command("理智提醒", rule=strict, priority=5, block=True)
    _reminder.handle()(_subscribe)
    scheduler.scheduled_job("interval", minutes=CHECK_MINUTES, id="sanity_reminder_check")(_check)
    scheduler.scheduled_job("cron", hour=4, minute=50, timezone="Asia/Shanghai", id="sanity_reminder_groups", misfire_grace_time=3600)(_recheck_groups)
    left = on_notice(rule=Rule(bot_left_group), priority=5, block=False)
    left.handle()(_forget_group)
