"""Weekly Skland binding health check (Monday 04:00 Asia/Shanghai) and sign-in network retries.

For every bound Skland account:
  1. verify the login (binding list request; refresh cred_token / re-grant cred when expired);
     a login that cannot be renewed -> the whole account is unbound;
  2. re-sync the role list from Skland (roles unbound on Skland's side are dropped);
  3. probe every Arknights / Endfield role with the read-only attendance query; a role the API
     rejects (e.g. 当前用户未经授权) or an Endfield binding without a role is removed;
  4. the account itself stays as long as any role works: it is unbound only when its login is
     gone or every real role it has was rejected.
Network failures are never treated as invalid: after 3 tries the account is skipped for this run.
Everything removed is exported first to backups/skland-health/ (0600).

/森空岛体检         (superuser) dry run, reports what would be removed
/森空岛体检 清理    (superuser) run and remove

The nightly sign-in jobs use httpx's 5 s default timeout and report a timeout as a failed role;
ark_sign / endfield_sign / refresh_token are wrapped to retry network errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from nonebot import get_bots, get_driver, logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot
from nonebot.compat import type_validate_python
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")

from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_scoped_session
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.api.login import skland_app_code
from nonebot_plugin_skland.db_handler import delete_character_gacha_records, select_user_characters
from nonebot_plugin_skland.exception import RequestException
from nonebot_plugin_skland.model import Character, GachaRecord, SkUser
from nonebot_plugin_skland.player_data import ark_card_data
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.schemas.binding import BindingApp
from sqlalchemy import delete, select

__plugin_meta__ = PluginMetadata(
    name="Skland health check",
    description="每周一 04:00 清理失效的森空岛绑定，并为签到增加网络重试。",
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
GAP = 0.6
NO_ROLE = "终末地绑定下没有角色（仅移除这条空记录）"
ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "backups" / "skland-health"
AUTO_GACHA_QUEUE = ROOT / "data" / "skland_auto_gacha" / "queue.sqlite3"

checkup = on_command("森空岛体检", rule=strict, permission=SUPERUSER, priority=5, block=True)
_lock = asyncio.Lock()


class NetworkError(Exception):
    """Skland could not be reached after retries; says nothing about the account."""


# ── HTTP with explicit error classes ────────────────────────────────────


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


async def _regrant(user: SkUser) -> bool:
    if not user.access_token:
        return False
    grant = await _http(
        "POST", GRANT_URL, json={"appCode": skland_app_code, "token": user.access_token, "type": 0}, headers={**SklandLoginAPI._headers}
    )
    code = (grant.get("data") or {}).get("code")
    if grant.get("status") != 0 or not code:
        return False
    payload = await _http("POST", CRED_URL, json={"code": code, "kind": 1}, headers={**SklandLoginAPI._headers})
    data = payload.get("data") or {}
    if payload.get("code") != 0 or not data.get("cred") or not data.get("token"):
        return False
    user.cred, user.cred_token = data["cred"], data["token"]
    return True


async def _login(user: SkUser) -> tuple[str, list | None]:
    """("ok", binding list) or (reason, None). Renews cred_token / cred when Skland asks for it."""
    code = None
    for _ in range(3):
        payload = await _signed_get(BINDING_URL, user)
        code = payload.get("code")
        if code == 0:
            return "ok", (payload.get("data") or {}).get("list") or []
        if code == 10000 and await _refresh_token(user):
            continue
        if code in (10000, 10002):
            if await _regrant(user):
                continue
            return "登录已过期", None
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


# ── database changes ────────────────────────────────────────────────────


def _row(obj) -> dict:
    return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}


async def _sync_roles(user: SkUser, binding: list, session) -> None:
    """Same merge as nonebot_plugin_skland.utils.bind_characters, from the binding list already fetched.

    Endfield bindings that have no role yet are not stored: they can never sign in.
    """
    apps = type_validate_python(list[BindingApp], binding)
    bound = {char.uid for app in apps for char in app.bindingList}
    for character in await select_user_characters(user, session):
        if character.uid not in bound:
            await delete_character_gacha_records(character, session)
            await session.delete(character)
    for app in apps:
        for character in app.bindingList:
            if character.roles:
                for role in character.roles:
                    await session.merge(
                        Character(
                            id=user.id, uid=character.uid, role_id=role.roleId, nickname=role.nickname, app_code=app.appCode,
                            channel_master_id=role.serverId, isdefault=len(character.roles) == 1 or role.isDefault,
                        )
                    )
            elif app.appCode != "endfield":
                await session.merge(
                    Character(
                        id=user.id, uid=character.uid, nickname=character.nickName, app_code=app.appCode,
                        channel_master_id=character.channelMasterId, isdefault=len(app.bindingList) == 1 or character.isDefault,
                    )
                )
    await session.flush()


async def _remove_role(char: Character, session, export: dict) -> None:
    records = (await session.scalars(select(GachaRecord).where(GachaRecord.uid == char.id, GachaRecord.char_uid == char.uid))).all()
    export["skland_gacha_record"] += [_row(r) for r in records]
    export["skland_characters"].append(_row(char))
    await delete_character_gacha_records(char, session)
    await session.delete(char)
    await session.flush()


async def _remove_user(user: SkUser, session, export: dict) -> None:
    records = (await session.scalars(select(GachaRecord).where(GachaRecord.uid == user.id))).all()
    export["skland_gacha_record"] += [_row(r) for r in records]
    export["skland_characters"] += [_row(c) for c in await select_user_characters(user, session)]
    export["skland_user"].append(_row(user))
    await session.execute(delete(GachaRecord).where(GachaRecord.uid == user.id))
    await session.execute(delete(Character).where(Character.id == user.id))
    await session.delete(user)
    await session.flush()
    with contextlib.suppress(Exception):
        await ark_card_data.invalidate_user(user.id)


def _write_export(export: dict) -> str:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = BACKUP_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(export, ensure_ascii=False, default=str), "utf-8")
    os.chmod(path, 0o600)
    return path.name


def _drop_queue(user_ids: list[int]) -> None:
    if not user_ids or not AUTO_GACHA_QUEUE.exists():
        return
    with contextlib.closing(sqlite3.connect(AUTO_GACHA_QUEUE, timeout=15)) as queue, queue:
        queue.execute(f"delete from pending where user_id in ({','.join('?' * len(user_ids))})", user_ids)


# ── the check ───────────────────────────────────────────────────────────


async def run(apply: bool) -> dict:
    """Check every account; with `apply`, also re-sync roles and remove what is invalid."""
    async with _lock:
        stats, reasons = Counter(), Counter()
        export = {"skland_user": [], "skland_characters": [], "skland_gacha_record": []}
        removed_users: list[int] = []
        started = time.time()
        session = get_scoped_session()
        try:
            for user_id in (await session.scalars(select(SkUser.id))).all():
                user = await session.get(SkUser, user_id)
                if user is None:
                    continue
                stats["accounts"] += 1
                try:
                    state, binding = await _login(user)
                    if state != "ok":
                        stats["invalid_accounts"] += 1
                        reasons[f"账号：{state}"] += 1
                        if apply:
                            removed_users.append(user.id)
                            await _remove_user(user, session, export)
                        await session.commit()
                        continue
                    if apply:
                        await _sync_roles(user, binding, session)
                    roles = [c for c in await select_user_characters(user, session) if c.app_code in CHECKED_APPS]
                    alive = rejected = 0
                    for char in roles:
                        stats["roles"] += 1
                        role_state = await _role_state(user, char)
                        if role_state == "ok":
                            alive += 1
                        else:
                            rejected += role_state != NO_ROLE  # an empty Endfield binding is not a rejected role
                            stats["invalid_roles"] += 1
                            reasons[f"角色：{role_state}"] += 1
                            if apply:
                                await _remove_role(char, session, export)
                        await asyncio.sleep(GAP)
                    # The account stays while any role works; it goes only when every real role was rejected.
                    if rejected and not alive:
                        stats["empty_accounts"] += 1
                        reasons["账号：全部角色都被接口拒绝"] += 1
                        if apply:
                            removed_users.append(user.id)
                            await _remove_user(user, session, export)
                    await session.commit()  # also persists renewed tokens; short transactions keep SQLite unlocked
                except NetworkError:
                    stats["network_skipped"] += 1
                    await session.rollback()
                except Exception as e:
                    stats["errors"] += 1
                    logger.warning(f"Skland health check: account skipped after {type(e).__name__}: {e}")
                    await session.rollback()
                await asyncio.sleep(GAP)
        finally:
            await session.close()
        backup = ""
        if apply and any(export.values()):
            backup = await asyncio.to_thread(_write_export, export)
            await asyncio.to_thread(_drop_queue, removed_users)
        result = {
            "apply": apply, "seconds": round(time.time() - started), "backup": backup, "reasons": dict(reasons), **dict(stats),
            "removed_gacha_records": len(export["skland_gacha_record"]) if apply else 0,
        }
        logger.info(f"Skland health check: {result}")
        return result


def summary(result: dict) -> str:
    removed_accounts = result.get("invalid_accounts", 0) + result.get("empty_accounts", 0)
    verb = "已解除" if result["apply"] else "将会解除"
    lines = [
        f"森空岛账号体检{'' if result['apply'] else '（仅检查，未改动）'}",
        f"检查 {result.get('accounts', 0)} 个账号、{result.get('roles', 0)} 个角色，用时 {result['seconds']} 秒",
        f"{verb} {removed_accounts} 个失效账号、{result.get('invalid_roles', 0)} 个失效角色",
    ]
    lines += [f"· {reason} ×{count}" for reason, count in sorted(result["reasons"].items(), key=lambda kv: -kv[1])]
    if result.get("network_skipped") or result.get("errors"):
        lines.append(f"因网络或异常跳过 {result.get('network_skipped', 0) + result.get('errors', 0)} 个账号（未改动，下次再查）")
    if result["apply"] and result["backup"]:
        lines.append(f"删除前已备份：backups/skland-health/{result['backup']}（含 {result['removed_gacha_records']} 条抽卡记录）")
    if not result["apply"] and (removed_accounts or result.get("invalid_roles")):
        lines.append("发送 /森空岛体检 清理 执行解除")
    return "\n".join(lines)


@scheduler.scheduled_job("cron", day_of_week="mon", hour=4, minute=0, timezone="Asia/Shanghai", id="skland_health", misfire_grace_time=3600)
async def _weekly() -> None:
    result = await run(apply=True)
    if not (result.get("invalid_accounts") or result.get("empty_accounts") or result.get("invalid_roles") or result.get("network_skipped")):
        return
    bot = next((b for b in get_bots().values() if isinstance(b, Bot)), None)
    for superuser in get_driver().config.superusers if bot else ():
        with contextlib.suppress(Exception):
            await bot.send_private_msg(user_id=int(superuser), message=summary(result))


@checkup.handle()
async def _(arg: Message = CommandArg()) -> None:
    apply = arg.extract_plain_text().strip() == "清理"
    if _lock.locked():
        await checkup.finish("体检正在进行中，请稍后再试")
    await checkup.send("开始检查全部森空岛账号，约需几分钟…")
    await checkup.finish(summary(await run(apply)))


# ── sign-in network retries ─────────────────────────────────────────────


def _retry_network(bound):
    """Retry a Skland call when it failed on the network (the plugin reports those as RequestException)."""

    @functools.wraps(bound)
    async def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return await bound(*args, **kwargs)
            except RequestException as e:
                if attempt == 2 or not isinstance(e.__cause__ or e.__context__, httpx.HTTPError):
                    raise
                await asyncio.sleep(2 * (attempt + 1))

    wrapper._network_retry = True
    return wrapper


for _owner, _name in ((SklandAPI, "ark_sign"), (SklandAPI, "endfield_sign"), (SklandLoginAPI, "refresh_token")):
    _method = getattr(_owner, _name)
    if not getattr(_method, "_network_retry", False):
        setattr(_owner, _name, staticmethod(_retry_network(_method)))
