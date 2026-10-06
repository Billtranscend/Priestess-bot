"""Weekly Skland binding health check (Monday 04:00 Asia/Shanghai) and sign-in network retries.

For every bound Skland account (a member may have several):
  1. verify the login (binding list request; refresh cred_token / re-grant cred when expired);
     a login that cannot be renewed -> that account is unbound. Any other answer (an error
     code from a busy API) is not a verdict: the account is left alone until the next run;
  2. re-sync the role list from Skland with the plugin's own account sync (roles unbound on
     Skland's side are dropped);
  3. probe every Arknights / Endfield role with the read-only attendance query; a role the API
     rejects twice in a row (e.g. 当前用户未经授权) is removed;
  4. the account itself stays as long as any role works: it is unbound only when its login is
     gone or every real role it has was rejected;
  5. a member left without a default role for a game they still have roles in gets one again
     (Skland's own default role if it says so, the oldest role otherwise). The same repair runs
     at startup.
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
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")

from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_scoped_session, get_session
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.api.login import skland_app_code
from nonebot_plugin_skland.account import sync_account
from nonebot_plugin_skland.db_handler import get_account_characters
from nonebot_plugin_skland.exception import RequestException
from nonebot_plugin_skland.model import Character, CharacterDefault, GachaRecord, SkUser
from nonebot_plugin_skland.player_data import ark_card_data
from nonebot_plugin_skland.schemas import CRED
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
RECHECK_GAP = 8  # seconds before a rejected role is asked about a second time
NO_ROLE = "终末地绑定下没有角色（仅移除这条空记录）"
EXPIRED = "登录已过期"  # the only account state that unbinds
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
            return EXPIRED, None
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


async def _remove_role(char: Character, session, export: dict) -> None:
    records = (await session.scalars(select(GachaRecord).where(GachaRecord.character_id == char.id))).all()
    export["skland_gacha_record"] += [_row(r) for r in records]
    export["skland_characters"].append(_row(char))
    # Explicit deletes: correct whether or not the database enforces ON DELETE CASCADE.
    await session.execute(delete(GachaRecord).where(GachaRecord.character_id == char.id))
    await session.execute(delete(CharacterDefault).where(CharacterDefault.character_id == char.id))
    await session.execute(delete(Character).where(Character.id == char.id))
    await session.flush()


async def _remove_user(user: SkUser, session, export: dict) -> None:
    """Unbind one Skland account with its roles and their records."""
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


async def ensure_defaults(session, owner_ids: list[int] | None = None) -> int:
    """Give every member a default role for each game they have roles in; returns how many were set.

    Upstream only picks a default by itself when a game's first role is bound. A default that
    disappears with its role (removed above, or lost in the 0.7.2 migration when two roles were
    both marked default) would leave the member's commands asking for `sk char set`.
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


def _write_export(export: dict) -> str:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = BACKUP_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(export, ensure_ascii=False, default=str), "utf-8")
    os.chmod(path, 0o600)
    return path.name


def _drop_queue(user_ids: list[int]) -> None:
    """Forget queued gacha syncs of members (owner ids) who have no account left."""
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
        touched: set[int] = set()  # owners that lost an account or a role
        started = time.time()
        session = get_scoped_session()
        try:
            for account_id in (await session.scalars(select(SkUser.id))).all():
                user = await session.get(SkUser, account_id)
                if user is None:
                    continue
                owner_id = user.owner_id
                stats["accounts"] += 1
                try:
                    state, _ = await _login(user)
                    if state == EXPIRED:
                        stats["invalid_accounts"] += 1
                        reasons[f"账号：{state}"] += 1
                        if apply:
                            touched.add(owner_id)
                            await _remove_user(user, session, export)
                        await session.commit()
                        continue
                    if state != "ok":
                        # Any other answer (an error code, a busy API) says nothing about the login:
                        # the account and its records stay, the next run looks again.
                        stats["unclear_accounts"] += 1
                        reasons[f"账号：{state}（未处理，下次再查）"] += 1
                        await session.commit()
                        continue
                    if apply:
                        await session.commit()  # a renewed token, before the sync opens its own transaction
                        synced = await sync_account(account_id, session)
                        if not synced.success:
                            stats["sync_failed"] += 1
                            logger.info("Skland health check: role list not refreshed for one account")
                        user = await session.get(SkUser, account_id)
                        if user is None:
                            continue
                    roles = [c for c in await get_account_characters(account_id, session) if c.app_code in CHECKED_APPS]
                    alive = rejected = 0
                    for char in roles:
                        stats["roles"] += 1
                        role_state = await _role_state(user, char)
                        if role_state not in ("ok", NO_ROLE):
                            # Skland sometimes answers a healthy role with a passing error code;
                            # a role (and its records) is only given up when the answer repeats.
                            await asyncio.sleep(RECHECK_GAP)
                            role_state = await _role_state(user, char)
                        if role_state == "ok":
                            alive += 1
                        else:
                            rejected += role_state != NO_ROLE  # an empty Endfield binding is not a rejected role
                            stats["invalid_roles"] += 1
                            reasons[f"角色：{role_state}"] += 1
                            if apply:
                                touched.add(owner_id)
                                await _remove_role(char, session, export)
                        await asyncio.sleep(GAP)
                    # The account stays while any role works; it goes only when every real role was rejected.
                    if rejected and not alive:
                        stats["empty_accounts"] += 1
                        reasons["账号：全部角色都被接口拒绝"] += 1
                        if apply:
                            touched.add(owner_id)
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
            removed_owners: list[int] = []
            if apply and touched:
                stats["defaults_set"] = await ensure_defaults(session, sorted(touched))
                left = set(await session.scalars(select(SkUser.owner_id).where(SkUser.owner_id.in_(touched)).distinct()))
                removed_owners = sorted(touched - left)
                await session.commit()
        finally:
            await session.close()
        backup = ""
        if apply and any(export.values()):
            backup = await asyncio.to_thread(_write_export, export)
            await asyncio.to_thread(_drop_queue, removed_owners)
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
    skipped = result.get("network_skipped", 0) + result.get("errors", 0) + result.get("unclear_accounts", 0)
    if skipped:
        lines.append(f"因网络、接口报错或异常跳过 {skipped} 个账号（未改动，下次再查）")
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
