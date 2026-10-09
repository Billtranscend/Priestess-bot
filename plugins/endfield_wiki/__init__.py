"""Endfield operator / weapon / equipment set lookup backed by AKEData, with data-update notices.

/<干员或昵称>       operator card, e.g. /提丰 /plk /小庄 (also /提丰配装: the card carries members' builds)
/<干员或昵称>专武   signature weapon card, e.g. /提丰专武
/<武器或简称>       weapon card, e.g. /寒夜幽影 /寒夜
/<套装或装备名>     equipment set card, e.g. /险关 /潮涌手甲
/资料库更新          (SUPERUSER) force a data sync now

The lookup matcher runs after every other command and only claims a message
when the text resolves to a known operator, weapon or equipment set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path

from nonebot import get_bots, get_driver, logger, on_command, on_message, require
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.exception import MatcherException
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict
from nonebot.typing import T_State

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage, message_reaction
from nonebot_plugin_apscheduler import scheduler

from plugins import ef_theme

from . import changes, data, render
from .lookup import Resolver

__plugin_meta__ = PluginMetadata(
    name="Endfield wiki",
    description="Operator and weapon cards from AKEData with fuzzy nickname lookup and update notices.",
    usage="/提丰  /提丰专武  /寒夜幽影",
    type="application",
)

DATA_DIR = store.get_plugin_data_dir()
INDEX_FILE = DATA_DIR / "index.json"
STATE_FILE = DATA_DIR / "state.json"
IMAGE_DIR = DATA_DIR / "img"
ALIASES_FILE = Path(__file__).with_name("aliases.json")
MISSES_FILE = DATA_DIR / "misses.json"  # unresolved name-like queries (text + count only), for curating aliases
SYNC_MINUTES = 30  # a conditional request; new data usually appears around 06:00-07:00 Beijing on a game update day
MANIFEST_FAILURES_BEFORE_ALERT = 3  # the data site being unreachable for a while is not worth a message
NOTIFY_GAP = 3.0
REACTION_PROCESSING, REACTION_DONE, REACTION_FAIL = "66", "144", "10060"

_sync_lock = asyncio.Lock()
_cache: dict = {"key": None, "index": None, "resolver": None}
_background: set[asyncio.Task] = set()


def _read_json(path: Path, default):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def _write_json(path: Path, value) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _resolver() -> tuple[dict, Resolver] | None:
    """Index + resolver, rebuilt when index.json or aliases.json changes on disk."""
    if not INDEX_FILE.exists():
        return None
    key = (INDEX_FILE.stat().st_mtime, ALIASES_FILE.stat().st_mtime)
    if _cache["key"] != key:
        index = _read_json(INDEX_FILE, {})
        _cache.update(key=key, index=index, resolver=Resolver(index, _read_json(ALIASES_FILE, {})))
    return _cache["index"], _cache["resolver"]


async def _react(emoji: str) -> None:
    with contextlib.suppress(Exception):
        await message_reaction(emoji)


async def _cached_image(url: str, name: str) -> str:
    IMAGE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = IMAGE_DIR / name
    if not path.exists():
        try:
            async with data.http_client() as client:
                response = await client.get(url, timeout=20)
                response.raise_for_status()
            tmp = path.with_suffix(".part")
            tmp.write_bytes(response.content)
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"Endfield wiki image unavailable: {type(e).__name__}")
            return ""
    return path.as_uri()


# ── lookup ─────────────────────────────────────────────────────────────


_MISS_PATTERN = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9.·]{2,10}$")


def _record_miss(query: str) -> None:
    """Count name-like queries nobody handled; other commands are already filtered by priority."""
    if not _MISS_PATTERN.match(query):
        return
    with contextlib.suppress(Exception):
        misses = _read_json(MISSES_FILE, {})
        misses[query] = misses.get(query, 0) + 1
        _write_json(MISSES_FILE, misses)


async def _lookup_rule(event: MessageEvent, state: T_State) -> bool:
    text = event.get_plaintext().strip()
    if not text.startswith("/") or len(text) > 30:
        return False
    loaded = _resolver()
    if not loaded:
        return False
    result = loaded[1].resolve(text[1:])
    if result is None:
        _record_miss(text[1:].strip())
        return False
    state["wiki_result"] = result
    return True


def _builds() -> tuple[dict, int]:
    """Members' build statistics kept by endfield_guide (empty when that plugin or its data is missing)."""
    with contextlib.suppress(Exception):
        from plugins import endfield_guide
        from plugins.endfield_guide import builds

        return endfield_guide.build_store.stats(endfield_guide.echo_store.optout()), builds.MIN_SAMPLE
    return {"operators": {}, "sets": {}}, 3


lookup = on_message(rule=_lookup_rule, priority=95, block=True)


@lookup.handle()
async def _(state: T_State) -> None:
    index, resolver = _resolver()
    result = state["wiki_result"]
    version = index.get("version", "")

    if result.kind == "candidates":
        await lookup.finish(f"找到多个结果：{'、'.join(result.candidates)}\n请输入更完整的名字")
    if result.kind == "no_signature":
        op = index["operators"][result.id]
        rec = "、".join(index["weapons"][w]["name"] for w in op["recommended"] if w in index["weapons"])
        await lookup.finish(f"{op['name']} 没有专属武器" + (f"，推荐武器：{rec}" if rec else ""))

    await _react(REACTION_PROCESSING)
    try:
        if result.kind == "weapon":
            weapon = index["weapons"][result.id]
            icon = await _cached_image(data.weapon_icon_url(weapon["icon"]), f"wpn_{weapon['icon']}.png")
            html = render.weapon_html(weapon, icon, version, result.note)
        elif result.kind == "equip_set":
            equip_set = index["equip_sets"][result.id]
            stats, min_sample = _builds()
            srcs = await asyncio.gather(*(_cached_image(data.equip_icon_url(p["icon"]), f"eq_{p['icon']}.png") for p in equip_set["pieces"]))
            icons = {p["id"]: src for p, src in zip(equip_set["pieces"], srcs)}
            html = render.equip_set_html(equip_set, icons, version, stats["sets"].get(equip_set["name"]), min_sample, result.note)
        else:
            op = index["operators"][result.id]
            icon = await _cached_image(data.char_icon_url(op["id"]), f"chr_{op['id']}.png")
            sig = index["weapons"].get(op.get("signature_weapon") or "", {}).get("name", "")
            stats, min_sample = _builds()
            builds = stats["operators"].get(op["name"])
            items = index.get("equip_items", {})
            worn = {p["name"]: None for build in builds["builds"] for p in build["pieces"]} if builds else {}
            names = [name for name in worn if name in items]
            srcs = await asyncio.gather(*(_cached_image(data.equip_icon_url(items[n]["icon"]), f"eq_{items[n]['icon']}.png") for n in names))
            pieces = {n: {"src": src, "attrs": items[n]["attrs"]} for n, src in zip(names, srcs)}
            html = render.operator_html(op, icon, version, sig, builds, min_sample, pieces)
        image = await ef_theme.render_page(html, render.WIDTH, template_path=DATA_DIR.as_uri())
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Endfield wiki render failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await lookup.finish("资料图生成失败，请稍后再试")
    await _react(REACTION_DONE)


# ── data sync and update notices ───────────────────────────────────────


def _bot() -> Bot | None:
    return next((b for b in get_bots().values() if isinstance(b, Bot)), None)


async def _tell_superusers(text: str) -> None:
    """A private message to every superuser; never to a group."""
    bot = _bot()
    if bot is None:
        logger.warning("Endfield wiki: no bot online, superusers not told")
        return
    for user_id in get_driver().config.superusers:
        try:
            await bot.send_private_msg(user_id=int(user_id), message=text)
        except Exception as e:
            logger.warning(f"Endfield wiki: a superuser could not be told ({type(e).__name__})")


async def _notify_all_groups(version: dict, previous: str, new_names: dict[str, list[str]]) -> None:
    """Sent as soon as the new data is in the index, whatever the hour (the owner's choice: data drops early in the morning)."""
    bot = _bot()
    if bot is None:
        logger.warning("Endfield wiki: no bot online, update notice skipped")
        return
    published = version.get("publishedAt", "")[:10]
    message = "\n".join([f"AKEData 终末地解包数据已更新\n{previous} → {version['id']}（{published}）", *changes.notice_lines(new_names)])
    groups = await bot.get_group_list()
    for group in groups:
        try:
            await bot.send_group_msg(group_id=group["group_id"], message=message)
        except Exception as e:
            logger.warning(f"Endfield wiki notice failed for one group: {type(e).__name__}")
        await asyncio.sleep(NOTIFY_GAP)
    logger.info(f"Endfield wiki update notice sent to {len(groups)} groups")


async def sync(force: bool = False) -> str:
    async with _sync_lock:
        DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = _read_json(STATE_FILE, {})
        async with data.http_client() as client:
            outdated = _read_json(INDEX_FILE, {}).get("schema", 1) < data.INDEX_SCHEMA if INDEX_FILE.exists() else True
            force = force or outdated
            manifest, etag = await data.fetch_manifest(client, None if force else state.get("etag"))
            if manifest is None:
                return "数据未变化"
            latest = next(v for v in manifest["versions"] if v["id"] == manifest["latest"])
            previous = state.get("version")
            if latest["id"] == previous and INDEX_FILE.exists() and not force:
                state["etag"] = etag
                _write_json(STATE_FILE, state)
                return f"已是最新版本 {previous}"
            _failures["building"] = latest["id"]  # from here on a failure means a new version could not be taken in
            old_index = _read_json(INDEX_FILE, {}) if INDEX_FILE.exists() else {}
            table_dir = DATA_DIR / "tables_tmp"
            data.remove_tree(table_dir)
            try:
                await data.download_tables(client, latest["tableCfgPath"], table_dir)
                counts = await asyncio.to_thread(data.build_index, table_dir, latest, INDEX_FILE)
            finally:
                data.remove_tree(table_dir)
        new_index = _read_json(INDEX_FILE, {})
        found = changes.problems(new_index)
        unseen = [item for item in found if item not in set(state.get("problems", []))]
        _write_json(STATE_FILE, {"version": latest["id"], "etag": etag, "synced_at": time.time(), "problems": found})
        logger.info(f"Endfield wiki index built for {latest['id']}: {counts}")
    if previous and previous != latest["id"]:
        await _notify_all_groups(latest, previous, changes.added(old_index, new_index))
    if unseen:
        logger.warning(f"Endfield wiki: {len(unseen)} new gaps in the cards after building {latest['id']}")
        await _tell_superusers(
            f"终末地资料库已更新到 {latest['id']}，但有 {len(unseen)} 处资料卡内容不完整，多半是游戏数据用了新的写法，需要改代码：\n"
            + "\n".join(f"· {item}" for item in unseen[:15])
            + (f"\n……共 {len(unseen)} 处" if len(unseen) > 15 else "")
        )
    return f"已同步 {latest['id']}（干员 {counts['operators']}，武器 {counts['weapons']}）"


_failures: dict = {"count": 0, "alerted": False, "building": None}


async def _sync_failed(error: Exception) -> None:
    """Tell the superusers once per run of failures: at once when a new version could not be built, later when only the check fails."""
    _failures["count"] += 1
    building = _failures["building"]
    if _failures["alerted"] or (building is None and _failures["count"] < MANIFEST_FAILURES_BEFORE_ALERT):
        return
    _failures["alerted"] = True
    reason = f"{type(error).__name__}: {str(error)[:120]}"
    current = _read_json(STATE_FILE, {}).get("version", "无")
    if building:
        text = (f"终末地资料库更新失败：发现新版本 {building}，但没有重建成功（{reason}）。\n"
                f"群里查到的仍是旧版本 {current} 的资料，新版本的群通知也还没有发。")
    else:
        text = f"终末地资料库已连续 {_failures['count']} 次检查不到 AKEData 的数据版本（{reason}），目前资料停在 {current}。"
    await _tell_superusers(text + f"\n机器人每 {SYNC_MINUTES} 分钟会自动重试，也可以发 /资料库更新 手动重试；恢复后会再告诉你。")


async def _sync_succeeded() -> None:
    recovered = _failures["alerted"]
    _failures.update(count=0, alerted=False, building=None)
    if recovered:
        await _tell_superusers(f"终末地资料库同步已恢复，当前版本 {_read_json(STATE_FILE, {}).get('version', '未知')}。")


@scheduler.scheduled_job("interval", minutes=SYNC_MINUTES, id="endfield_wiki_sync")
async def _scheduled_sync() -> None:
    _failures["building"] = None
    try:
        await sync()
    except Exception as e:
        logger.warning(f"Endfield wiki sync failed: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await _sync_failed(e)
    else:
        with contextlib.suppress(Exception):
            await _sync_succeeded()


@get_driver().on_startup
async def _startup_sync() -> None:
    async def run() -> None:
        await asyncio.sleep(30)
        await _scheduled_sync()

    task = asyncio.get_running_loop().create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


force_sync = on_command("资料库更新", rule=strict, permission=SUPERUSER, priority=5, block=True)


@force_sync.handle()
async def _() -> None:
    try:
        message = await sync(force=True)
    except Exception as e:
        message = f"同步失败：{type(e).__name__}"
    await force_sync.finish(message)
