"""pErithacus 关键词触发器的两处补丁。

上游的触发器在最高优先级处理每一条消息：查一遍词条表，还把消息里的图片、语音、视频、文件整个下载下来算哈希。
1. 不可能命中时不跑：触发器多一条规则（NoneBot 先判断规则，再写“Event will be handled by”日志），
   没有未删除的词条时跳过；消息带媒体、而没有一条有效词条（含别名）带媒体时也跳过，因为带媒体的消息
   只可能精准匹配到带同一媒体的词条（模糊和正则只看纯文字消息），下载也就省了。
   词条概况缓存 KEYWORDS_SECONDS 秒；/pe 指令跑完马上作废，增删改立即生效。
2. 下载放宽超时、出错时不下载：上游用 httpx 默认的 5 秒超时、不处理网络错误，QQ 图片 CDN 一慢触发器就报错。
   换成超时更长的版本，网络出错时退回不下载的 dump，这样的消息只是匹配不到媒体词条。

3. /pe add、del、edit 的词条切分：上游的 handle_main_args 只去掉开头的 "pe add "，不认指令前缀 / 和中文子命令，
   "/pe 添加 词条 内容" 的词条名就成了 "/pe"。交给它之前先去掉指令前缀、把中文子命令换成英文。

只替换触发器模块里的 dump_msg，/pe add、edit 保存内容时仍是上游原本的行为。
上游版本或这几个源文件的哈希对不上时，对应的补丁不打，启动时记一条警告，上游照原样运行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import httpx
from nonebot import get_driver, logger, require
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from sqlalchemy import select

require("nonebot_plugin_perithacus")
require("nonebot_plugin_orm")

from nonebot_plugin_alconna import Text, UniMessage, UniMsg
from nonebot_plugin_alconna.uniseg.segment import Media
from nonebot_plugin_orm import get_session
from nonebot_plugin_perithacus import cmd_add as pe_cmd_add
from nonebot_plugin_perithacus import cmd_delete as pe_cmd_delete
from nonebot_plugin_perithacus import cmd_edit as pe_cmd_edit
from nonebot_plugin_perithacus import command as pe_command
from nonebot_plugin_perithacus import handle_args as pe_handle_args
from nonebot_plugin_perithacus import lib as pe_lib
from nonebot_plugin_perithacus import trigger as pe_trigger
from nonebot_plugin_perithacus.database import Index

__plugin_meta__ = PluginMetadata(
    name="pErithacus trigger resilience",
    description="Skips the keyword trigger when no live keyword can match; longer timeout and graceful fallback for its media downloads.",
    usage="Loaded automatically.",
    type="application",
)

_EXPECTED_VERSION = "1.4.4"
_EXPECTED_SHA256 = {
    "trigger.py": "94039f924950ca199acb089f5b645533bf7e27a6b5c96ac6c053d39012e26b10",
    "lib.py": "25ecd7d3130fa3c41ce1a5bab836f8f5f941cca3b792e6e933245836e9f0812d",
    # 第 1 处补丁靠这两个文件：触发器 on_every_message 的定义，以及词条怎么匹配（带媒体的消息只能精准匹配）
    "command.py": "f1abc9616eb3f86852ed63c12b991133217a5083220e93485baafa6cd1ca696f",
    "database.py": "6cf99038fe2ffaa70c5517c3ae158fc0ec581c08a4561d322e6cab450f56b6ad",
}
# 第 3 处补丁靠这几个文件：handle_main_args 怎么切分，以及 add、del、edit 从哪里取它
_ARGS_SHA256 = {
    "handle_args.py": "86b112f8b59f8461afffaa1dde678a4d5dd43a5726cb76a27fba561ecd9a2f6c",
    "cmd_add.py": "21b5f15017464ad603eb285e7466b9b2fe222d47443020e39d17099177163a54",
    "cmd_delete.py": "455338dd17ef5255dd6e87812c3fd8e8e9f9ba4a7eab3abea74c0a3a8bf5129f",
    "cmd_edit.py": "82b7fdb77c3149750f344b8ca41b8410c22aed9bddf21b664e3e61bc532888f1",
}
_SUBCOMMANDS = {"add": "添加", "del": "删除", "edit": "修改"}  # 上游 Alconna 里的中文别名
_DOWNLOAD_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
KEYWORDS_SECONDS = 60
_upstream_dump_msg = pe_lib.dump_msg
_upstream_main_args = pe_handle_args.handle_main_args


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _upstream_matches() -> bool:
    installed = version("nonebot-plugin-pErithacus")
    if installed != _EXPECTED_VERSION:
        logger.warning(f"pErithacus {installed} is not {_EXPECTED_VERSION}; trigger patches skipped")
        return False
    folder = Path(pe_trigger.__file__).parent
    for name, expected in _EXPECTED_SHA256.items():
        if _sha256(folder / name) != expected:
            logger.warning(f"pErithacus {name} hash changed; trigger patches skipped")
            return False
    if pe_trigger.dump_msg is not _upstream_dump_msg:
        logger.warning("pErithacus trigger.dump_msg already replaced; trigger patches skipped")
        return False
    return True


async def resilient_trigger_dump_msg(msg, *, media_save_dir=False) -> str:
    if not isinstance(msg, UniMessage):
        msg = UniMessage(msg)
    if msg.has(Media):
        try:
            await pe_lib.pe_download(msg, timeout=_DOWNLOAD_TIMEOUT)
        except httpx.HTTPError as e:
            logger.debug(f"pErithacus trigger media download skipped: {type(e).__name__}")
    return pe_lib.pe_uni_dump(msg, media_save_dir=media_save_dir, json=True)


@dataclass(frozen=True)
class Keywords:
    """有效（未删除）词条的概况。"""

    any: bool
    media: bool


AS_UPSTREAM = Keywords(any=True, media=True)  # 读不到词条表时：照上游原样跑
_known: tuple[float, Keywords] | None = None
_version = 0  # 每次作废加一：作废之前开始的查询，结果不再存
_lock = asyncio.Lock()


def _has_media(dumped: str) -> bool:
    """存下的消息（UniMessage dump 出的 JSON）里有没有图片、语音、视频或文件；看不懂时当作有。"""
    try:
        return bool(pe_lib.load_media(dumped).select(Media))
    except Exception:
        return True


def _mentions_media(keyword: str, alias: str | None) -> bool:
    """词条名或某个别名里有媒体。别名列是 dump 出的 JSON 字符串组成的 JSON 数组。"""
    try:
        aliases = json.loads(alias) if alias else []
    except ValueError:
        aliases = []  # 上游也解析不了，这些别名本来就匹配不到
    return any(_has_media(text) for text in [keyword, *aliases])


async def _live_rows(session) -> list[tuple[str, str | None]]:
    """未删除词条的（词条名，别名）。"""
    return [(keyword, alias) for keyword, alias in (await session.execute(select(Index.keyword, Index.alias).where(~Index.deleted))).all()]


async def _scan() -> Keywords:
    async with get_session() as session:
        rows = await _live_rows(session)
    return Keywords(any=bool(rows), media=any(_mentions_media(*row) for row in rows))


def forget_keywords() -> None:
    """词条可能变了：下一条消息重新查。"""
    global _known, _version
    _known, _version = None, _version + 1


async def live_keywords() -> Keywords:
    """有效词条的概况，最多是 KEYWORDS_SECONDS 秒前查的。"""
    global _known
    async with _lock:
        if _known is not None and time.monotonic() - _known[0] < KEYWORDS_SECONDS:
            return _known[1]
        started = _version
        try:
            found = await _scan()
        except Exception as e:
            logger.warning(f"pErithacus keywords not read ({type(e).__name__}); the trigger runs as upstream")
            found = AS_UPSTREAM
        if started == _version:
            _known = (time.monotonic(), found)
        return found


async def _worth_matching(msg: UniMsg) -> bool:
    """触发器值得跑：有有效词条；消息带媒体时，还要有带媒体的有效词条。"""
    keywords = await live_keywords()
    return keywords.any and (keywords.media or not msg.select(Media))


async def _after_pe_command(matcher: Matcher) -> None:
    """/pe 指令可能增删改了词条（已提交）：缓存作废。"""
    if isinstance(matcher, pe_command.pe):
        forget_keywords()


def as_upstream_expects(msg: UniMessage, sub_command: str) -> UniMessage:
    """"/pe 添加 词条 内容" 变成上游认得的 "pe add 词条 内容"：去掉指令前缀，中文子命令换成英文。"""
    if not msg or not isinstance(msg[0], Text):
        return msg
    text = msg[0].text.lstrip()
    for start in sorted(get_driver().config.command_start, key=len, reverse=True):
        if start and text.startswith(start):
            text = text[len(start):]
            break
    words = "|".join(re.escape(w) for w in (sub_command, _SUBCOMMANDS.get(sub_command, sub_command)))
    text = re.sub(rf"^(?:pe|perithacus)\s+(?:{words})(?=\s)", f"pe {sub_command}", text)
    return UniMessage([Text(text), *msg[1:]])


async def _main_args(msg: UniMessage, sub_command: str):
    return await _upstream_main_args(as_upstream_expects(msg, sub_command), sub_command)


def _args_match() -> bool:
    folder = Path(pe_handle_args.__file__).parent
    for name, expected in _ARGS_SHA256.items():
        if _sha256(folder / name) != expected:
            logger.warning(f"pErithacus {name} hash changed; /pe keyword parsing left as upstream")
            return False
    return all(module.handle_main_args is _upstream_main_args for module in (pe_cmd_add, pe_cmd_delete, pe_cmd_edit))


if _args_match():
    for _module in (pe_cmd_add, pe_cmd_delete, pe_cmd_edit):
        _module.handle_main_args = _main_args

if _upstream_matches():
    pe_trigger.dump_msg = resilient_trigger_dump_msg
    pe_command.on_every_message.rule = pe_command.on_every_message.rule & Rule(_worth_matching)
    run_postprocessor(_after_pe_command)
    logger.info("pErithacus trigger patches active")
