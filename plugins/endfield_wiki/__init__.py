"""基于 AKEData 的终末地干员 / 武器 / 装备套装查询，附带数据更新通知。

/<干员或昵称>       干员卡，例如 /提丰 /plk /小庄（/提丰配装 也行：卡上有群友的配装统计）
/<干员或昵称>专武   专武卡，例如 /提丰专武
/<武器或简称>       武器卡，例如 /寒夜幽影 /寒夜
/<套装或装备名>     装备套装卡，例如 /险关 /潮涌手甲
/资料库更新          （SUPERUSER）立即强制同步数据

查询匹配器排在所有其他指令之后，只有当文字能解析为已知的干员、武器或装备套装时
才接下这条消息。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

from nonebot import get_driver, logger, on_command, on_message, require
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.exception import MatcherException
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict
from nonebot.typing import T_State

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_apscheduler import scheduler

from plugins import ef_theme

from . import changes, data, render
from .lookup import Resolver
from plugins.shared import REACTION_DONE, REACTION_FAIL, REACTION_PROCESSING, first_bot, tell_superusers, write_json, react as _react

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
GOAL_RETRY_SECONDS = 6 * 3600
SYNC_MINUTES = 10  # 只是一个小请求；游戏更新日新数据通常在北京时间 06:00-07:00 左右出现
WHO = "Endfield wiki"  # 私聊超级用户失败时写日志用
MANIFEST_FAILURES_BEFORE_ALERT = 3  # 数据站点暂时连不上不值得发消息
NOTIFY_GAP = 3.0

_sync_lock = asyncio.Lock()
_notice_lock = asyncio.Lock()  # 一轮更新通知要发两三分钟，这期间再同步（例如手动 /资料库更新）不会重发
_cache: dict = {"mtime": None, "index": None, "key": None, "resolver": None}
_background: set[asyncio.Task] = set()


def _read_json(path: Path, default):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def _index() -> dict | None:
    """磁盘上的 index.json，变化时重新读。同步和卡池表只要它：解析器建不起来（例如旧结构的索引）也照样能用。"""
    if not INDEX_FILE.exists():
        return None
    mtime = INDEX_FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        _cache.update(mtime=mtime, index=_read_json(INDEX_FILE, {}), key=None)
    return _cache["index"]


def _resolver() -> tuple[dict, Resolver] | None:
    """索引 + 解析器；磁盘上的 index.json 或 aliases.json 变化时重建。"""
    index = _index()
    if index is None:
        return None
    key = (_cache["mtime"], ALIASES_FILE.stat().st_mtime)
    if _cache["key"] != key:
        _cache.update(key=key, resolver=Resolver(index, _read_json(ALIASES_FILE, {})))
    return index, _cache["resolver"]


_rich_css_cache: dict[str, str] = {}


async def rich_css(index: dict) -> str:
    """这份索引对应的游戏文字颜色和术语图标的 CSS（图标与其他卡片图片一样缓存）；
    endfield_guide 的关卡页和敌人页也会用到。"""
    key = f'{index.get("version", "")}|{index.get("schema", "")}'  # 同一版本因 schema 升级重建时样式也会变
    if key not in _rich_css_cache:
        styles, terms = index.get("rich_styles", {}), index.get("rich_terms", {})
        names = sorted({s["icon"] for s in [*styles.values(), *terms.values()] if s.get("icon")})
        srcs = await asyncio.gather(*(_cached_image(data.term_icon_url(n), f"term_{n}.png") for n in names))
        css = render.rich_css(styles, dict(zip(names, srcs)), terms)
        if all(srcs):  # 有图标没下载到：这次先用，不留着，下一张卡再试
            _rich_css_cache.clear()
            _rich_css_cache[key] = css
        return css
    return _rich_css_cache[key]




async def _cached_image(url: str, name: str) -> str:
    IMAGE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = IMAGE_DIR / name
    if not path.exists():
        try:
            async with data.http_client() as client:
                response = await client.get(url, timeout=20)
                response.raise_for_status()
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.part")  # 两个请求同时下载同一张也不会互相删掉
            tmp.write_bytes(response.content)
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"Endfield wiki image unavailable: {type(e).__name__}")
            return ""
    return path.as_uri()


# ── 查询 ───────────────────────────────────────────────────────────────


async def _lookup_rule(event: MessageEvent, state: T_State) -> bool:
    text = event.get_plaintext().strip()
    if not text.startswith("/") or len(text) > 30:
        return False
    loaded = _resolver()
    if not loaded:
        return False
    result = loaded[1].resolve(text[1:])
    if result is None:
        return False
    state["wiki_result"] = result
    return True


def _builds() -> tuple[dict, int]:
    """endfield_guide 保存的群友配装统计（该插件或其数据缺失时为空）。"""
    with contextlib.suppress(Exception):
        from plugins import endfield_guide
        from plugins.endfield_guide import builds

        return endfield_guide.build_store.stats(endfield_guide.echo_store.optout()), builds.MIN_SAMPLE
    return {"operators": {}, "sets": {}}, 3


def _loose_usage(series: dict) -> dict | None:
    """谁穿着某个散件系列的装备，依据 endfield_guide 保存的群友配装。"""
    with contextlib.suppress(Exception):
        from plugins import endfield_guide

        return endfield_guide.build_store.wearers({p["name"] for p in series["pieces"]}, endfield_guide.echo_store.optout())
    return None


def _partners(operator_id: str) -> dict | None:
    """该干员在群友高难通关中的队友，由 endfield_guide 保存（没有数据时为 None）。"""
    with contextlib.suppress(Exception):
        from plugins import endfield_guide

        return endfield_guide.team_partners().get(operator_id)
    return None


lookup = on_message(rule=_lookup_rule, priority=95, block=True)


@lookup.handle()
async def _(state: T_State) -> None:
    index, _ = _resolver()
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
        render.RICH_CSS = await rich_css(index)
        if result.kind == "weapon":
            weapon = index["weapons"][result.id]
            icon = await _cached_image(data.weapon_icon_url(weapon["icon"]), f"wpn_{weapon['icon']}.png")
            html = render.weapon_html(weapon, icon, version, result.note)
        elif result.kind == "equip_set":
            equip_set = index["equip_sets"][result.id]
            stats, min_sample = _builds()
            srcs = await asyncio.gather(*(_cached_image(data.equip_icon_url(p["icon"]), f"eq_{p['icon']}.png") for p in equip_set["pieces"]))
            icons = {p["id"]: src for p, src in zip(equip_set["pieces"], srcs)}
            usage = _loose_usage(equip_set) if equip_set.get("loose") else stats["sets"].get(equip_set["name"])
            html = render.equip_set_html(equip_set, icons, version, usage, min_sample, result.note)
        else:
            op = index["operators"][result.id]
            icon = await _cached_image(data.char_icon_url(op["id"]), f"chr_{op['id']}.png")
            sig = index["weapons"].get(op.get("signature_weapon") or "", {}).get("name", "")
            stats, min_sample = _builds()
            builds = stats["operators"].get(op["name"])
            items = index.get("equip_items", {})
            listed = (builds.get("by_potential") if op.get("rarity") == 6 else builds.get("builds")) or [] if builds else []
            worn = {p["name"]: None for build in listed for p in build.get("pieces", [])}
            names = [name for name in worn if name in items]
            srcs = await asyncio.gather(*(_cached_image(data.equip_icon_url(items[n]["icon"]), f"eq_{items[n]['icon']}.png") for n in names))
            pieces = {n: {"src": src, "attrs": items[n]["attrs"]} for n, src in zip(names, srcs)}
            ship = [slot["icon"] for slot in op.get("logistics", []) if slot.get("icon")]
            ship_srcs = await asyncio.gather(*(_cached_image(data.logistics_icon_url(i), f"ship_{i}.png") for i in ship))
            team = _partners(op["id"])
            mates = sorted({cid for cid, _ in (team or {}).get("mates", [])} | {cid for ids, _ in (team or {}).get("teams", []) for cid in ids} - {op["id"]})
            mate_srcs = await asyncio.gather(*(_cached_image(data.char_icon_url(c), f"chr_{c}.png") for c in mates))
            names = {cid: o["name"] for cid, o in index["operators"].items()}
            html = render.operator_html(op, icon, version, sig, builds, min_sample, pieces, dict(zip(ship, ship_srcs)),
                                        team, {c: {"src": s, "name": names.get(c, "")} for c, s in zip(mates, mate_srcs)})
        image = await ef_theme.render_page(html, render.WIDTH, template_path=DATA_DIR.as_uri())
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:
        logger.warning(f"Endfield wiki render failed: {type(e).__name__}")
        await _react(REACTION_FAIL)
        await lookup.finish("资料图生成失败，请稍后再试")
    await _react(REACTION_DONE)


# ── 数据同步和更新通知 ─────────────────────────────────────────────────


async def _notify_all_groups(version: dict, previous: str, new_names: dict[str, list[str]]) -> bool:
    """新数据一进索引就发送，不管几点（数据都是一大早出来的）。发完了（或者不用发）时返回 True；
    机器人不在线、群列表读不到、一个群都没发出去时返回 False，下一轮同步再发。

    只在版本新增了干员或武器时才发：只新增套装或关卡，
    或只改了数值和文字的版本，照常收录，但不在群里通知。
    """
    if not (new_names.get("干员") or new_names.get("武器")):
        logger.info(f"Endfield wiki: {version['id']} adds no operator or weapon, no group notice")
        return True
    bot = first_bot()
    if bot is None:
        logger.warning("Endfield wiki: no bot online, update notice postponed")
        return False
    published = version.get("publishedAt", "")[:10]
    message = "\n".join([f"AKEData 终末地解包数据已更新\n{previous} → {version['id']}（{published}）", *changes.notice_lines(new_names)])
    try:
        groups = await bot.get_group_list()
    except Exception as e:
        logger.warning(f"Endfield wiki: group list not read ({type(e).__name__}), update notice postponed")
        return False
    sent = refused = 0
    for group in groups:
        try:
            await (first_bot() or bot).send_group_msg(group_id=group["group_id"], message=message)  # 发送期间连接可能已经重建
            sent += 1
        except Exception as e:
            if getattr(e, "retcode", None) == UNPROMPTED_REFUSED:
                refused += 1
                continue  # 被拒的群什么也没收到，不用为它等发送间隔
            logger.warning(f"Endfield wiki notice failed for one group: {type(e).__name__}")
        await asyncio.sleep(NOTIFY_GAP)
    logger.info(f"Endfield wiki update notice sent to {sent} of {len(groups)} groups ({refused} do not take pushes)")
    return bool(sent or refused) or not groups


UNPROMPTED_REFUSED = 40_034_105  # 平台：群里没开“机器人主动在群聊内发言”


async def _deliver_pending() -> None:
    """state 里还没发出去的更新通知（版本收录之后才记下的），发出去后删掉。已经在发的时候直接跳过。"""
    if _notice_lock.locked():
        return
    async with _notice_lock:
        state = _read_json(STATE_FILE, {})
        pending = state.get("pending_notice")
        if not pending:
            return
        if await _notify_all_groups(pending["version"], pending["previous"], pending["new_names"]):
            async with _sync_lock:
                state = _read_json(STATE_FILE, {})
                if state.get("pending_notice") == pending:
                    state.pop("pending_notice")
                    write_json(STATE_FILE, state)


async def sync(force: bool = False) -> str:
    """检查并收录新版本；之后把还没发出去的更新通知发掉（上一轮没发成的也在这时补发）。"""
    result = await _sync(force)
    await _deliver_pending()
    return result


async def _sync(force: bool) -> str:
    async with _sync_lock:
        DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = _read_json(STATE_FILE, {})
        async with data.http_client() as client:
            current = _index()  # 与查资料共用的缓存：每 10 分钟检查一次，不必每次重新解析整份索引
            outdated = current is None or current.get("schema", 1) < data.INDEX_SCHEMA
            current = current or {}
            # 增辉条件的文件和数据表分开发布，可能晚一些才出。
            late_goals = changes.goal_gaps(current) and time.time() - state.get("synced_at", 0) >= GOAL_RETRY_SECONDS
            force = force or outdated or bool(late_goals)
            manifest = await data.fetch_manifest(client)
            latest = next(v for v in manifest["versions"] if v["id"] == manifest["latest"])
            previous = state.get("version")
            if latest["id"] == previous and not force:  # 没有索引时 outdated 已经要求重建
                return f"已是最新版本 {previous}"
            if previous and latest.get("publishedAt", "") < state.get("published", "") and not force:
                # CDN 偶尔给出过期的清单：不退回旧版本（否则回到新版本时又会把同一批新内容通知一遍）
                return f"清单里的 {latest['id']} 比已收录的 {previous} 旧，不处理"
            _failures["building"] = latest["id"]  # 从这里开始失败就意味着新版本没能收录
            old_index = current
            table_dir = DATA_DIR / "tables_tmp"
            data.remove_tree(table_dir)
            building = INDEX_FILE.with_name("index.building.json")
            try:
                await data.download_tables(client, latest["tableCfgPath"], table_dir)
                counts = await asyncio.to_thread(data.build_index, table_dir, latest, building)
                # 先看一眼再替换：上游改了字段名时，可能建出一份几乎是空的索引，那样所有查询都会没反应
                before = len(old_index.get("operators") or {})
                if not counts["operators"] or counts["operators"] < before * 0.8:
                    raise ValueError(f"the new index has {counts['operators']} operators (was {before}); the old one is kept")
                building.replace(INDEX_FILE)
            finally:
                data.remove_tree(table_dir)
                building.unlink(missing_ok=True)
        new_index = _index()  # 顺便把新索引放进缓存，下一次查询不用再读
        found = changes.problems(new_index)
        unseen = [item for item in found if item not in set(state.get("problems", []))]
        saved = {"version": latest["id"], "published": latest.get("publishedAt", ""), "synced_at": time.time(), "problems": found}
        if previous and previous != latest["id"]:  # 通知先记下来，发出去之后才删：发送失败也不会丢
            saved["pending_notice"] = {"version": latest, "previous": previous, "new_names": changes.added(old_index, new_index)}
        elif state.get("pending_notice"):
            saved["pending_notice"] = state["pending_notice"]
        write_json(STATE_FILE, saved)
        _failures["building"] = None  # 新版本已经收录：之后再出错（例如发通知）不是“没能重建”
        logger.info(f"Endfield wiki index built for {latest['id']}: {counts}")
    if unseen:
        logger.warning(f"Endfield wiki: {len(unseen)} new gaps in the cards after building {latest['id']}")
        await tell_superusers(
            f"终末地资料库已更新到 {latest['id']}，但有 {len(unseen)} 处资料卡内容不完整，多半是游戏数据用了新的写法，需要改代码：\n"
            + "\n".join(f"· {item}" for item in unseen[:15])
            + (f"\n……共 {len(unseen)} 处" if len(unseen) > 15 else ""),
            WHO,
        )
    return f"已同步 {latest['id']}（干员 {counts['operators']}，武器 {counts['weapons']}）"


_failures: dict = {"count": 0, "alerted": False, "building": None, "at": 0.0}
FAILING_RETRY = 3600  # 连续失败三次以上后，多久才再试一次


async def _sync_failed(error: Exception) -> None:
    """每轮连续失败只告诉超级用户一次：新版本没能生成时立即通知，只是检查失败时过几次再通知。"""
    _failures["count"] += 1
    _failures["at"] = time.time()
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
    await tell_superusers(text + f"\n机器人每 {SYNC_MINUTES} 分钟会自动重试，也可以发 /资料库更新 手动重试；恢复后会再告诉你。", WHO)


async def _sync_succeeded() -> None:
    recovered = _failures["alerted"]
    _failures.update(count=0, alerted=False, building=None)
    if recovered:
        await tell_superusers(f"终末地资料库同步已恢复，当前版本 {_read_json(STATE_FILE, {}).get('version', '未知')}。", WHO)


@scheduler.scheduled_job("interval", minutes=SYNC_MINUTES, id="endfield_wiki_sync")
async def _scheduled_sync() -> None:
    # 连续失败时放慢：每次失败都要重新下载几十张表，数据一直坏着的话不必每 10 分钟下一遍
    if _failures["count"] >= 3 and time.time() - _failures.get("at", 0) < FAILING_RETRY:
        return
    _failures["building"] = None
    try:
        logger.info(f"Endfield wiki check: {await sync()}")  # 每次检查都留一行日志：之后能查到某个版本是什么时候第一次看到的
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
