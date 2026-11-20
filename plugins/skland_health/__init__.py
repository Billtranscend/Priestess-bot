"""每周的森空岛绑定体检（周一 04:00，Asia/Shanghai），以及签到的网络重试。

对每个已绑定的森空岛账号（一个成员可以有多个）：
  1. 验证登录（请求绑定列表；过期时刷新 cred_token / 重新授权 cred）；
     登录无法续期 -> 解绑该账号。其他任何回答（API 繁忙时的错误码）都不算定论：
     这个账号先不动，等下一次运行；
  2. 用插件自己的账号同步，从森空岛重新同步角色列表（森空岛那边已解绑的角色会被去掉）；
  3. 用只读的签到状态查询探测每个明日方舟 / 终末地角色；被 API 连续拒绝两次的角色
     （例如“当前用户未经授权”）会被移除；
  4. 只要还有任何一个角色能用，账号本身就保留：只有登录失效，或者它所有真实角色都被拒绝时
     才解绑；
  0. 既不在机器人任何一个群里、也不是机器人好友的成员，在为他发出任何森空岛请求之前，连同所有
     账号一起解绑（他们用不了机器人，每晚的任务却还在为他们跑；只在私聊里用机器人的好友保留）。
     前提是每个群的成员列表和好友列表都完整读到了（好友列表是空的也算没读到），有官方机器人时还要查清
     谁在等重新绑定（见 0b）；超级用户保留；
  0b. 官方 QQ 机器人接替了 OneBot 账号的地方（plugins/official_bridge）：通过旧账号绑定、官方机器人
     从未认出的成员，从旧账号不在任何群里那天起，不参加每晚签到，也不参加这里的任何检查，
     保留 45 天后解绑（见 dormant.py）。回来要靠他们自己：在私聊里扫码登录
     森空岛，就会把绑定交接过来；
  5. 某个游戏里仍有角色、却没有默认角色的成员，重新给他一个默认角色（森空岛自己有默认角色就用它，
     否则用最早的角色）。启动时也会做同样的修复。
网络失败从不被当作失效：试 3 次后，这次运行就跳过该账号。
每删一个账号或角色，提交之前先把删掉的行追加进这次运行的 backups/skland-health/<UTC 时间>.jsonl（一行一次删除，
权限 0600，见 backup.py）；写回数据库用 deploy/restore_health_backup.py。

/森空岛体检         （超级用户）试运行，报告会移除什么
/森空岛体检 清理    （超级用户）运行并移除

每晚的签到任务用 httpx 默认的 5 秒超时，超时就报告为角色失败；
ark_sign / endfield_sign / refresh_token、绑定列表 / 用户 id 查询，以及图片类指令（卡片、肉鸽、抽卡页面）
背后的只读查询，都套上了网络错误重试。登录相关的调用（二维码、授权码、cred）不按指令期望的方式报告
网络错误：它们也会重试，之后以 RequestException 抛出，这样连不上森空岛的绑定会回答“绑定失败”，
而不是悄无声息地结束。

两个每晚任务本身（00:15 明日方舟，00:20 终末地）在这里用各自独立的会话运行，每个账号处理完就保存：
见文件末尾的 run_daily_sign（/全体签到 也用它，plugins/skland_shortcuts）。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from nonebot import get_driver, logger, on_command, require
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")

from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_scoped_session, get_session
from nonebot_plugin_skland import tasks
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.api.login import skland_app_code
from nonebot_plugin_skland.account import sync_account
from nonebot_plugin_skland.db_handler import get_account_characters, select_all_accounts
from nonebot_plugin_skland.exception import RequestException
from nonebot_plugin_skland.model import Character, CharacterDefault, GachaRecord, SkUser
from nonebot_plugin_skland.player_data import ark_card_data
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_user.models import Bind
from nonebot_plugin_skland.services import sign as sign_service
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from . import backup
from .dormant import KEEP_DAYS, days_left, overdue, waiting_owners
from plugins.shared import first_bot, tell_superusers, write_json

__plugin_meta__ = PluginMetadata(
    name="Skland health check",
    description="每周一 04:00 清理失效的森空岛绑定（含已退出机器人所有群的成员），并为签到增加网络重试。",
    usage="/森空岛体检 [清理]（超级用户）",
    type="application",
)

BASE = "https://zonai.skland.com/api/v1"
BINDING_URL = f"{BASE}/game/player/binding"
ARK_ATTENDANCE_URL = f"{BASE}/game/attendance"
EF_ATTENDANCE_URL = "https://zonai.skland.com/web/v1/game/endfield/attendance"
REFRESH_URL = f"{BASE}/auth/refresh"
CRED_URL = f"{BASE}/user/auth/generate_cred_by_code"
GRANT_URL = "https://as.hypergryph.com/user/oauth2/v2/grant"
CHECKED_APPS = ("arknights", "endfield")
GAP = 0.6  # 同一个 worker 两次请求之间的秒数
WORKERS = 5  # 同时检查的账号数，与竞速榜采集相同
RECHECK_GAP = 8  # 被拒绝的角色隔多少秒再问第二次
NO_ROLE = "终末地绑定下没有角色（仅移除这条空记录）"
EXPIRED = "登录已过期"  # 唯一会导致解绑的账号状态
ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "backups" / "skland-health"
AUTO_GACHA_QUEUE = ROOT / "data" / "skland_auto_gacha" / "queue.sqlite3"

checkup = on_command("森空岛体检", rule=strict, permission=SUPERUSER, priority=5, block=True)
_lock = asyncio.Lock()


class NetworkError(Exception):
    """重试后仍连不上森空岛；这不说明账号有任何问题。"""


def _why(e: Exception) -> str:
    """日志里写的出错原因：异常类型，数据库报错再加驱动的说明（例如 database is locked）。
    不写 str(e)：SQLAlchemy 的报错文字带着 SQL 和参数（含凭证）；驱动的说明 e.orig 不带。"""
    if isinstance(e, DBAPIError):
        return f"{type(e).__name__}: {str(e.orig)[:80]}"
    return type(e).__name__


# ── 区分错误类型的 HTTP 请求 ────────────────────────────────────


async def _http(method: str, url: str, **kwargs) -> dict:
    last = "error"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, url, **kwargs)
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            last = "bad payload"
        except (httpx.HTTPError, ValueError) as e:
            last = type(e).__name__
        if attempt < 2:  # 最后一次失败后直接放弃，不等
            await asyncio.sleep(3 * (attempt + 1))
    raise NetworkError(last)


async def _signed_get(url: str, user: SkUser, extra: dict | None = None) -> dict:
    cred = CRED(cred=user.cred, token=user.cred_token)
    headers = await SklandAPI.get_sign_header(cred, url, method="get")
    return await _http("GET", url, headers={**headers, **(extra or {})})


async def _refresh_token(user: SkUser) -> bool:
    payload = await _http("GET", REFRESH_URL, headers={**SklandLoginAPI._headers, "cred": user.cred})
    token = (payload.get("data") or {}).get("token")
    if payload.get("code") == 0 and token:
        user.cred_token = token
        return True
    return False


async def _regrant(user: SkUser) -> tuple[bool, str]:
    """用保存的登录凭证重新换 cred：（成功与否，森空岛的说法）。"""
    if not user.access_token:
        return False, "没有保存登录凭证"
    grant = await _http(
        "POST", GRANT_URL, json={"appCode": skland_app_code, "token": user.access_token, "type": 0}, headers={**SklandLoginAPI._headers}
    )
    code = (grant.get("data") or {}).get("code")
    if grant.get("status") != 0 or not code:
        return False, str(grant.get("msg") or grant.get("message") or grant.get("status"))
    payload = await _http("POST", CRED_URL, json={"code": code, "kind": 1}, headers={**SklandLoginAPI._headers})
    data = payload.get("data") or {}
    if payload.get("code") != 0 or not data.get("cred") or not data.get("token"):
        return False, str(payload.get("message") or payload.get("code"))
    user.cred, user.cred_token = data["cred"], data["token"]
    return True, ""


def _says_expired(said: str) -> bool:
    """森空岛明确说登录不能用了（“登录已过期，请重新登录”之类）；限流、繁忙这类回答不算。"""
    return any(word in said for word in ("过期", "失效", "重新登录", "未登录", "没有保存登录凭证"))


async def _login(user: SkUser) -> tuple[str, list | None]:
    """返回 ("ok", 绑定列表) 或 (原因, None)。森空岛要求时续期 cred_token / cred。"""
    code = None
    for _ in range(3):
        payload = await _signed_get(BINDING_URL, user)
        code = payload.get("code")
        if code == 0:
            return "ok", (payload.get("data") or {}).get("list") or []
        if code == 10000 and await _refresh_token(user):
            continue
        if code in (10000, 10002):
            granted, said = await _regrant(user)
            if granted:
                continue
            # 只有森空岛明确说登录过期才算：限流、系统繁忙之类的回答说明不了什么，下次再查
            return (EXPIRED, None) if _says_expired(said) else (f"续期没有成功：{said[:24]}", None)
        break
    return f"接口错误 {code}", None


async def _role_state(user: SkUser, char: Character) -> str:
    if char.app_code == "endfield":
        if not char.role_id:
            return NO_ROLE
        payload = await _signed_get(EF_ATTENDANCE_URL, user, {"sk-game-role": f"3_{char.role_id}_{char.channel_master_id}"})
    else:
        payload = await _signed_get(f"{ARK_ATTENDANCE_URL}?{urlencode({'uid': char.uid, 'gameId': char.channel_master_id})}", user)
    code = payload.get("code")
    return "ok" if code == 0 else f"{char.app_code} {code} {str(payload.get('message') or '')[:24]}"


# ── 数据库修改 ────────────────────────────────────────────────────


def _row(obj) -> dict:
    return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}


async def _remove_role(char: Character, session, export: dict) -> None:
    records = (await session.scalars(select(GachaRecord).where(GachaRecord.character_id == char.id))).all()
    export["skland_gacha_record"] += [_row(r) for r in records]
    export["skland_characters"].append(_row(char))
    # 显式删除：不管数据库是否执行 ON DELETE CASCADE 都正确。
    await session.execute(delete(GachaRecord).where(GachaRecord.character_id == char.id))
    await session.execute(delete(CharacterDefault).where(CharacterDefault.character_id == char.id))
    await session.execute(delete(Character).where(Character.id == char.id))
    await session.flush()


async def _remove_user(user: SkUser, session, export: dict) -> None:
    """解绑一个森空岛账号，连同它的角色及其记录。"""
    account_id = user.id
    role_ids = select(Character.id).where(Character.account_id == account_id)
    records = (await session.scalars(select(GachaRecord).where(GachaRecord.character_id.in_(role_ids)))).all()
    export["skland_gacha_record"] += [_row(r) for r in records]
    export["skland_characters"] += [_row(c) for c in await get_account_characters(account_id, session)]
    export["skland_user"].append(_row(user))
    await session.execute(delete(GachaRecord).where(GachaRecord.character_id.in_(role_ids)))
    await session.execute(delete(CharacterDefault).where(CharacterDefault.character_id.in_(role_ids)))
    await session.execute(delete(Character).where(Character.account_id == account_id))
    await session.execute(delete(SkUser).where(SkUser.id == account_id))
    await session.flush()
    with contextlib.suppress(Exception):
        await ark_card_data.invalidate_account(account_id)


async def _drop_role(char: Character, session, export: Counter) -> None:
    """删掉这个角色（未提交）并把删掉的追加进备份；提交由调用方做。"""
    removed = backup.empty()
    await _remove_role(char, session, removed)
    await _checkpoint(removed, export)


async def _drop_user(user: SkUser, session, export: Counter) -> None:
    """解绑这个账号（未提交）并把删掉的追加进备份；提交由调用方做。"""
    removed = backup.empty()
    await _remove_user(user, session, removed)
    await _checkpoint(removed, export)


async def ensure_defaults(session, owner_ids: list[int] | None = None) -> int:
    """给每个成员在其有角色的每个游戏里设一个默认角色；返回设置了多少个。

    上游只在某个游戏绑定第一个角色时才自己选默认角色。默认角色随角色一起消失时（上面被移除，
    或者在 0.7.2 迁移中两个角色都标成默认而丢失），成员的指令就会要求先 `sk char set`。
    """
    owners = owner_ids if owner_ids is not None else list(await session.scalars(select(SkUser.owner_id).distinct()))
    fixed = 0
    for owner_id in owners:
        have = set(await session.scalars(select(CharacterDefault.app_code).where(CharacterDefault.owner_id == owner_id)))
        for app_code in CHECKED_APPS:
            if app_code in have:
                continue
            role_id = await session.scalar(
                select(Character.id)
                .join(Character.account)
                .where(SkUser.owner_id == owner_id, Character.app_code == app_code, Character.role_id != "")
                .order_by(Character.is_skland_default.desc(), Character.id)
                .limit(1)
            )
            if role_id is not None:
                session.add(CharacterDefault(owner_id=owner_id, app_code=app_code, character_id=role_id))
                fixed += 1
    await session.flush()
    return fixed


_export_path: Path | None = None  # 这次运行的备份文件；预览时为 None，什么都不写


async def _checkpoint(removed: dict, export: Counter) -> None:
    """删除提交之前先把这次删的追加进备份（一行）：运行中途出错或被重启，已经删掉的也都在备份里。
    写不成就抛出：调用方回滚，这个账号这次不删。export 累计这次运行备份了每张表多少行。"""
    if _export_path is None:
        return
    await asyncio.to_thread(backup.append, _export_path, removed)
    export.update({table: len(removed[table]) for table in backup.TABLES})


async def _backup_complete(path: Path, export: Counter) -> bool:
    """删完之后再读一遍备份：每行都读得出，每张表的行数等于这次删的。不抛出：核对不了也要照常报告结果。"""
    try:
        found = await asyncio.to_thread(backup.tally, path)
    except Exception as e:
        logger.warning(f"Skland health check: backup {path.name} not readable ({type(e).__name__})")
        return False
    if all(found[table] == export[table] for table in backup.TABLES):
        return True
    logger.warning(f"Skland health check: backup {path.name} holds {dict(found)} rows, {dict(export)} were removed")
    return False


SUSPECTS_FILE = ROOT / "data" / "skland_health" / "suspect_roles.json"
SECOND_STRIKE_AFTER = 5 * 86400  # 第一次被拒至少过了这么久、还被拒，才删角色（每周一次，所以就是下一周）
_suspects: dict[str, float] = {}  # 角色 id -> 第一次被拒的时间


def _load_suspects() -> dict[str, float]:
    try:
        return {str(k): float(v) for k, v in json.loads(SUSPECTS_FILE.read_text("utf-8")).items()}
    except FileNotFoundError:
        return {}
    except Exception as e:  # 读不了就当没有：最多是被拒的角色晚一周才删
        logger.warning(f"Skland health check: suspect roles not read ({type(e).__name__})")
        return {}


def _save_suspects(suspects: dict[str, float]) -> None:
    SUSPECTS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(SUSPECTS_FILE, suspects)


def _second_strike(character_id: int) -> bool:
    first = _suspects.get(str(character_id))
    return first is not None and time.time() - first >= SECOND_STRIKE_AFTER


def _drop_queue(user_ids: list[int]) -> None:
    """清掉已经没有账号的成员（所有者 id）在队列里的抽卡同步。"""
    if not user_ids or not AUTO_GACHA_QUEUE.exists():
        return
    with contextlib.closing(sqlite3.connect(AUTO_GACHA_QUEUE, timeout=15)) as queue, queue:
        queue.execute(f"delete from pending where user_id in ({','.join('?' * len(user_ids))})", user_ids)


# ── 体检本身 ───────────────────────────────────────────────────────────


MEMBER_LIST_TRIES = 3
MEMBER_LIST_PAUSE = 5  # 秒


async def absent_owners(session) -> set[int] | None:
    """有森空岛账号、但 QQ 既不在机器人任何一个群里、也不是机器人好友的所有者（bind id）。

    只在私聊里用机器人的好友不算离开。判断不了时为 None：没有机器人、
    没有群，某个成员列表读回来少了人，或者好友列表读不到或是空的（名单读错，绝不能因此解绑任何人）。
    """
    bot = first_bot()
    if bot is None:
        return None
    try:
        groups = await bot.get_group_list(no_cache=True)
        members: set[str] = set()
        for group in groups:
            gid, expected, listed = group["group_id"], group.get("member_count") or 0, []
            # 大群的人数和成员列表是先后读的：中间有人进群或退群，列表就少一个，整个步骤
            # 就会被跳过。放弃之前再问一次，人数也重新问。
            for _attempt in range(MEMBER_LIST_TRIES):
                listed = await bot.get_group_member_list(group_id=gid, no_cache=True)
                if listed and len(listed) >= expected:
                    break
                await asyncio.sleep(MEMBER_LIST_PAUSE)
                with contextlib.suppress(Exception):
                    expected = (await bot.get_group_info(group_id=gid, no_cache=True)).get("member_count") or expected
            else:
                logger.info(f"Skland health check: member list of group {gid} short ({len(listed or [])}/{expected}) after {MEMBER_LIST_TRIES} tries, nobody unbound for leaving")
                return None
            members |= {str(member["user_id"]) for member in listed}
            await asyncio.sleep(0.5)
        friends = await bot.get_friend_list(no_cache=True)
        if not friends:  # 机器人总有好友（至少是管理它的人）：空列表是没读到，不是真的没有
            logger.info("Skland health check: the friend list came back empty, nobody unbound for leaving")
            return None
        members |= {str(friend["user_id"]) for friend in friends}
    except Exception as e:
        logger.info(f"Skland health check: group members or friends not read ({type(e).__name__}), nobody unbound for leaving")
        return None
    if not groups:
        return None
    superusers = {str(user) for user in get_driver().config.superusers}
    rows = (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.bind_id.in_(select(SkUser.owner_id))))).all()
    present = {bind_id for platform_id, bind_id in rows if platform_id in members or platform_id in superusers}
    return {bind_id for _, bind_id in rows} - present



async def members_waiting(session) -> tuple[set[int] | None, float | None]:
    """通过旧机器人绑定、官方机器人从未认出的所有者，以及他们从什么时候开始等待。

    不涉及这个问题时（这里没有官方机器人，或者旧机器人还在某个群里）是空集合和 None；
    有官方机器人、却查不清楚时是 (None, None)，由调用方决定怎么办。
    """
    bridge = sys.modules.get("plugins.official_bridge")  # 只在配置了官方机器人的地方才有；绝不从这里导入
    if bridge is None:
        return set(), None
    try:
        gone_at = bridge.old_bot_gone_at()
        if gone_at is None:
            return set(), None
        rows = (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.bind_id.in_(select(SkUser.owner_id))))).all()
        superusers = {str(user) for user in get_driver().config.superusers}
        return waiting_owners([(str(number), owner) for number, owner in rows], bridge.recognised, superusers), gone_at
    except Exception as e:
        logger.warning(f"Skland health check: who is waiting to be recognised could not be told ({_why(e)}); "
                       "the nightly sign leaves nobody out, the weekly check unbinds nobody for leaving")
        return None, None


async def _check_account(account_id: int, session, apply: bool, absent: set[int] | None, waiting: set[int], time_is_up: bool,
                         stats: Counter, reasons: Counter, export: Counter, touched: set[int]) -> None:
    """检查一个账号（run 的一个 worker 调用）；session 只给这一个账号用，不自动 flush、提交后不过期（见 run）。export 见 _checkpoint。

    写了什么就马上提交，绝不带着没提交的写入去等网络或 sleep：SQLite 同时只能有一个写事务，
    其他 worker 的写入会一直等到 database is locked。
    """
    user = await session.get(SkUser, account_id)
    if user is None:
        return
    owner_id = user.owner_id
    stats["accounts"] += 1

    async def remove(stat: str, reason: str, char: Character | None = None) -> None:
        """记一次移除（char 为空时是整个账号）。清理时先删、追加备份、提交，都成了才记：
        备份写不成或提交失败会抛出，这次没删，也就不记。"""
        if apply:
            await (_drop_user(user, session, export) if char is None else _drop_role(char, session, export))
            await session.commit()
            touched.add(owner_id)
        stats[stat] += 1
        reasons[reason] += 1

    if owner_id in waiting:
        # 官方机器人接手以来一直没认出的人：不为他们发任何请求，
        # 也不按他们在哪些群里来判断。
        if time_is_up:
            await remove("waiting_removed", f"账号：机器人更换后 {KEEP_DAYS} 天内没有重新绑定")
        else:
            stats["waiting_accounts"] += 1
        return
    if absent and owner_id in absent:
        await remove("left_accounts", "账号：本人已不在机器人所在的任何群，也不是机器人好友")
        return
    try:
        state, _ = await _login(user)
        if session.dirty:
            await session.commit()  # 续期后的 token 马上保存：后面还有同步和角色检查要等网络
        if state == EXPIRED:
            await remove("invalid_accounts", f"账号：{state}")
            return
        if state != "ok":
            # 其他任何回答（错误码、API 繁忙）都说明不了登录状态：
            # 账号和记录都保留，下次运行再看。
            stats["unclear_accounts"] += 1
            reasons[f"账号：{state}（未处理，下次再查）"] += 1
            return
        if apply:
            synced = await sync_account(account_id, session)
            if not synced.success:
                stats["sync_failed"] += 1
                logger.info("Skland health check: role list not refreshed for one account")
            user = await session.get(SkUser, account_id)
            if user is None:
                return
        roles = [c for c in await get_account_characters(account_id, session) if c.app_code in CHECKED_APPS]
        alive = rejected = 0
        for char in roles:
            stats["roles"] += 1
            role_state = await _role_state(user, char)
            if role_state not in ("ok", NO_ROLE):
                # 森空岛有时会对正常的角色回一个临时的错误码；
                # 只有回答重复出现时，才放弃这个角色（及其记录）。
                await asyncio.sleep(RECHECK_GAP)
                role_state = await _role_state(user, char)
            if role_state == "ok":
                alive += 1
                _suspects.pop(str(char.id), None)
            elif role_state != NO_ROLE and not _second_strike(char.id):
                # 第一次被拒（本周两次都被拒）：先记下，下周还被拒才删。临时的错误码不会连着两周出现。
                stats["suspect_roles"] += 1
                reasons[f"角色：{role_state}（第一次被拒，下周再查）"] += 1
                if apply:
                    _suspects.setdefault(str(char.id), time.time())
            else:
                rejected += role_state != NO_ROLE  # 空的终末地绑定不算被拒绝的角色
                await remove("invalid_roles", f"角色：{role_state}", char)
                if apply:
                    _suspects.pop(str(char.id), None)
            await asyncio.sleep(GAP)
        # 只要还有角色能用，账号就保留；只有所有真实角色都被拒绝才移除。
        if rejected and not alive:
            await remove("empty_accounts", "账号：全部角色都被接口拒绝")
    except NetworkError:
        stats["network_skipped"] += 1
        await session.rollback()
    except Exception as e:
        stats["errors"] += 1
        logger.warning(f"Skland health check: account skipped after {_why(e)}")
        await session.rollback()


async def run(apply: bool) -> dict:
    """检查每个账号；`apply` 为真时，还会重新同步角色并移除失效的东西。

    开始记一行 started，结束记结果（"{'apply': …"）或 failed：deploy/restart_when_idle.py 看到开始了没结束就不重启。
    """
    async with _lock:
        logger.info(f"Skland health check: started ({'clean-up' if apply else 'preview'})")
        try:
            result = await _check_all(apply)
        except Exception as e:
            logger.warning(f"Skland health check: failed ({_why(e)})")
            raise
        logger.info(f"Skland health check: {result}")
        return result


async def _check_all(apply: bool) -> dict:
    """run 的正文：检查一遍，返回结果。"""
    global _export_path
    stats, reasons, export = Counter(), Counter(), Counter()  # export：备份了每张表多少行
    _export_path = BACKUP_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl" if apply else None
    _suspects.clear()
    _suspects.update(_load_suspects())
    touched: set[int] = set()  # 失去了账号或角色的所有者
    removed_owners: list[int] = []  # 其中一个账号都不剩的
    started = time.time()
    session = get_scoped_session()
    try:
        absent = await absent_owners(session)
        if absent is None:
            stats["groups_unchecked"] = 1
        waiting, gone_at = await members_waiting(session)
        if waiting is None:
            # 有官方机器人，却查不清谁在等：等的人多半也不在任何群里，这次谁都不按“已退群”解绑
            waiting, absent = set(), None
            stats["waiting_unknown"] = 1
        time_is_up = overdue(gone_at, time.time())
        if waiting:
            stats["waiting_days_left"] = days_left(gone_at, time.time()) or 0
        account_ids = list((await session.scalars(select(SkUser.id))).all())
        queue: asyncio.Queue[int] = asyncio.Queue()
        for account_id in account_ids:
            queue.put_nowait(account_id)

        async def worker() -> None:
            while not queue.empty():
                account_id = queue.get_nowait()
                try:
                    # 不自动 flush、提交后不过期：什么时候写由 _check_account 决定，提交后手里的账号和角色照样能读
                    async with get_session(autoflush=False, expire_on_commit=False) as own:
                        await _check_account(account_id, own, apply, absent, waiting, time_is_up, stats, reasons, export, touched)
                except Exception as e:  # 一个账号出错不能停掉其他 worker；这个账号这次不动
                    stats["errors"] += 1
                    logger.warning(f"Skland health check: account skipped after {_why(e)}")
                await asyncio.sleep(GAP)

        await asyncio.gather(*(worker() for _ in range(min(WORKERS, len(account_ids)) or 1)))
        if apply and touched:
            try:
                left = set(await session.scalars(select(SkUser.owner_id).where(SkUser.owner_id.in_(touched)).distinct()))
                removed_owners = sorted(touched - left)
                stats["defaults_set"] = await ensure_defaults(session, sorted(touched))
                await session.commit()
            except Exception as e:  # 删掉的都已提交，报告更要紧：默认角色下次启动时会再补
                await session.rollback()
                stats["defaults_unchecked"] = 1
                logger.warning(f"Skland health check: default roles not checked after the removals ({_why(e)})")
    finally:
        await session.close()
        if apply:
            try:
                await asyncio.to_thread(_save_suspects, _suspects)
            except Exception as e:  # 存不下名单就还是上次的，只记日志：结果报告更要紧
                logger.warning(f"Skland health check: suspect roles not saved ({type(e).__name__})")
    backup_name = ""
    if apply and any(export.values()):  # 删掉的都已逐次追加进备份，这里只核对
        backup_name = _export_path.name
        if not await _backup_complete(_export_path, export):
            stats["backup_unverified"] = 1
        try:
            await asyncio.to_thread(_drop_queue, removed_owners)
        except Exception as e:  # 结果报告比这个要紧：清不掉只记日志
            logger.warning(f"Skland health check: gacha sync queue not cleared ({type(e).__name__})")
    return {
        "apply": apply, "seconds": round(time.time() - started), "backup": backup_name, "reasons": dict(reasons), **dict(stats),
        "removed_gacha_records": export["skland_gacha_record"],
    }


def summary(result: dict) -> str:
    removed_accounts = result.get("invalid_accounts", 0) + result.get("empty_accounts", 0) + result.get("left_accounts", 0) + result.get("waiting_removed", 0)
    verb = "已解除" if result["apply"] else "将会解除"
    lines = [
        f"森空岛账号体检{'' if result['apply'] else '（仅检查，未改动）'}",
        f"检查 {result.get('accounts', 0)} 个账号、{result.get('roles', 0)} 个角色，用时 {result['seconds']} 秒",
        f"{verb} {removed_accounts} 个失效账号、{result.get('invalid_roles', 0)} 个失效角色",
    ]
    lines += [f"· {reason} ×{count}" for reason, count in sorted(result["reasons"].items(), key=lambda kv: -kv[1])]
    skipped = result.get("network_skipped", 0) + result.get("errors", 0) + result.get("unclear_accounts", 0)
    if skipped:
        lines.append(f"因网络、接口报错或异常跳过 {skipped} 个账号（未改动，下次再查）")
    if result.get("groups_unchecked"):
        lines.append("群成员名单或好友列表没能完整读取：本次没有按「已退群」解除任何账号")
    if result.get("waiting_unknown"):
        lines.append("没能查清哪些成员还在等机器人更换后重新绑定：本次没有按「已退群」解除任何账号，这些成员的账号照常检查")
    if result.get("waiting_accounts"):
        lines.append(f"机器人更换后还没重新绑定的老账号 {result['waiting_accounts']} 个：已停自动签到、未检查，{result.get('waiting_days_left', 0)} 天后解除")
    if result["apply"] and result["backup"]:
        lines.append(f"删除前已备份：backups/skland-health/{result['backup']}（含 {result['removed_gacha_records']} 条抽卡记录）")
    if result.get("backup_unverified"):
        lines.append("删完后核对这个备份没有通过（有读不出的行，或行数和删掉的对不上），请检查这个文件")
    if result.get("defaults_unchecked"):
        lines.append("删完后没能给失去角色的成员补默认角色（数据库报错），下次启动时会自动补上")
    if not result["apply"] and (removed_accounts or result.get("invalid_roles")):
        lines.append("发送 /森空岛体检 清理 执行解除")
    return "\n".join(lines)


@scheduler.scheduled_job("cron", day_of_week="mon", hour=4, minute=0, timezone="Asia/Shanghai", id="skland_health", misfire_grace_time=3600)
async def _weekly() -> None:
    result = await run(apply=True)
    # errors 也要说：备份写不成时要删的都只记在这里
    if not any(result.get(key) for key in ("invalid_accounts", "empty_accounts", "left_accounts", "invalid_roles", "network_skipped", "errors",
                                           "groups_unchecked", "waiting_unknown", "waiting_removed", "backup_unverified", "defaults_unchecked")):
        return
    await tell_superusers(summary(result), "Skland health check")


@get_driver().on_startup
async def _repair_defaults() -> None:
    try:
        async with get_session() as session:
            fixed = await ensure_defaults(session)
            await session.commit()
        if fixed:
            logger.info(f"Skland health check: default role set for {fixed} member/game pairs that had none")
    except Exception as e:
        logger.warning(f"Skland health check: default roles not verified ({type(e).__name__})")


@checkup.handle()
async def _(arg: Message = CommandArg()) -> None:
    apply = arg.extract_plain_text().strip() == "清理"
    if _lock.locked():
        await checkup.finish("体检正在进行中，请稍后再试")
    await checkup.send("开始检查全部森空岛账号，约需几分钟…")
    await checkup.finish(summary(await run(apply)))


# ── 签到的网络重试 ─────────────────────────────────────────────


class SklandUnreachable(RequestException):
    """在指令把错误写进回复的地方，读起来是普通文字（NoneBot 的异常打印出来是 repr）。"""

    def __str__(self) -> str:
        return str(self.args[0])


def _retry_network(bound):
    """森空岛调用因网络失败时重试（插件把这类失败报告为 RequestException）。

    httpx 默认超时是 5 秒，而超时异常不带文字，所以不包这一层的话，指令会回答“…失败:”，冒号后面什么都没有。
    试三次之后，会告诉成员发生了什么。
    """

    @functools.wraps(bound)
    async def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return await bound(*args, **kwargs)
            except RequestException as e:
                cause = e.__cause__ or e.__context__
                if not isinstance(cause, httpx.HTTPError):
                    raise
                if attempt == 2:
                    logger.warning(f"Skland call {bound.__name__} unreachable after 3 tries: {type(cause).__name__}")
                    raise SklandUnreachable("连接森空岛失败（网络超时或中断），请稍后重试") from cause
                await asyncio.sleep(2 * (attempt + 1))

    wrapper._network_retry = True
    return wrapper


for _owner, _name in (
    (SklandAPI, "ark_sign"), (SklandAPI, "endfield_sign"), (SklandLoginAPI, "refresh_token"),
    (SklandAPI, "get_binding"), (SklandAPI, "get_user_ID"),  # 绑定过程中和卡片都会读取
    # 图片类指令背后的只读查询
    (SklandAPI, "endfield_card"), (SklandAPI, "ark_card"), (SklandAPI, "get_rogue"), (SklandAPI, "endfield_war_echoes"),
    (SklandAPI, "get_gacha_categories"), (SklandAPI, "get_gacha_history"), (SklandAPI, "get_ef_gacha_history"), (SklandAPI, "get_ef_gacha_content"),
):
    _method = getattr(_owner, _name)
    if not getattr(_method, "_network_retry", False):
        setattr(_owner, _name, staticmethod(_retry_network(_method)))


def _report_network(bound, attempts: int):
    """登录调用遇到网络失败时，重试之后转成插件自己的 RequestException。

    登录调用会把 httpx 的错误直接放出来，而指令只处理 RequestException：成员刚扫完二维码就
    连接超时，绑定会以一段 traceback 结束，没有任何回复。
    """

    @functools.wraps(bound)
    async def wrapper(*args, **kwargs):
        for attempt in range(attempts):
            try:
                return await bound(*args, **kwargs)
            except httpx.HTTPError as e:
                if attempt == attempts - 1:
                    logger.warning(f"Skland login call {bound.__name__} unreachable after {attempts} tries: {type(e).__name__}")
                    raise SklandUnreachable("连接森空岛失败（网络超时或中断），请稍后重试") from e
                await asyncio.sleep(2 * (attempt + 1))

    wrapper._network_reported = True
    return wrapper


# 二维码循环每两秒轮询一次 get_scan_status，它把 RequestException 当作“还没扫码”。
for _name, _attempts in (("get_scan", 3), ("get_scan_status", 1), ("get_token_by_scan_code", 3), ("get_grant_code", 3),
                         ("get_cred", 3), ("get_role_token_by_uid", 3), ("get_ak_cookie", 3)):
    _method = getattr(SklandLoginAPI, _name)
    if not getattr(_method, "_network_reported", False):
        setattr(SklandLoginAPI, _name, staticmethod(_report_network(_method, _attempts)))


# ── 每晚签到 ─────────────────────────────────────────────────────



async def _save_tokens(session) -> None:
    """提交签到刷新的 token；保存不了（数据库忙、账号刚被解绑）就放弃，绝不致命：下次用到时会再刷新。
    提交失败后会话必须先回滚才能再用，所以不在这里重试。"""
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.warning(f"Nightly sign-in: refreshed tokens of one account not saved ({type(e).__name__})")


async def run_daily_sign(game) -> None:
    """一个游戏的每晚签到（/全体签到 也用它）：WORKERS 个账号同时签，每个账号用自己的会话，处理完就保存。

    插件在 scoped session 上运行两个任务，而在指令之外，这个会话是整个进程共用的同一个：
    先跑完的任务会在另一个任务还在用时把它关掉，后者因 DetachedInstanceError 而死，当天剩下的
    角色都签不上。它还只在最后才提交，所以一个刷新过的 token 会让数据库在整个运行期间都处于
    写锁定状态，那几分钟里其他所有写操作都会因 "database is locked" 失败。
    结果按账号原来的顺序写进签到缓存（/全体签到详情 照这个顺序列出）。
    """
    async with get_session() as session:
        waiting = (await members_waiting(session))[0] or set()  # 查不清谁在等时照常全部签：多签几个人无害
        accounts = [(account.id, account.owner_id) for account in await select_all_accounts(session)]
    left_out = sum(1 for _, owner_id in accounts if owner_id in waiting)  # 通过旧机器人绑定、之后一直没出现：在他们回来之前不签到
    todo = [account_id for account_id, owner_id in accounts if owner_id not in waiting]
    signed: list[list] = [[] for _ in todo]
    broken = [0]
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(len(todo)):
        queue.put_nowait(index)

    async def worker() -> None:
        while not queue.empty():
            index = queue.get_nowait()
            signed[index] = await _sign_account(todo[index], game, broken)

    await asyncio.gather(*(worker() for _ in range(min(WORKERS, len(todo)) or 1)))
    entries = [entry for account in signed for entry in account]
    sign_service.write_sign_cache(game, entries)
    logger.info(f"Nightly sign-in ({game}): {len(entries)} roles done" + (f", {broken[0]} ended with an error" if broken[0] else "") + (f"; {left_out} account(s) of members not recognised since the official bot took over left out" if left_out else ""))


async def _sign_account(account_id: int, game, broken: list[int]) -> list:
    """签一个账号下这个游戏的所有角色。一个账号出什么错（数据库忙、签到途中被解绑），别的账号照样签。"""
    entries = []
    try:
        async with get_session(expire_on_commit=False, autoflush=False) as session:
            account = await session.get(SkUser, account_id)
            if account is None:
                return entries
            for character in await get_account_characters(account_id, session):
                if character.app_code != game:
                    continue
                try:
                    entries.append(await sign_service.sign_character(account, character, game))
                except Exception as e:  # 一个角色出错不能让后面所有人当晚都签不了
                    broken[0] += 1
                    logger.warning(f"Nightly sign-in: a {game} role failed with {type(e).__name__}")
            if session.dirty:
                await _save_tokens(session)
    except Exception as e:
        broken[0] += 1
        logger.warning(f"Nightly sign-in: one account skipped after {type(e).__name__}")
    return entries


tasks._run_daily_sign = run_daily_sign
