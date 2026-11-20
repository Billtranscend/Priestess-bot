"""/zmd抽卡记录 和 /zmd抽卡记录更新：本项目的终末地抽卡页面。

nonebot-plugin-skland 0.7.2 把页面换成了每次查询都同步、在浏览器里切成固定高度分页的版本。
本项目保留成员熟悉的行为：

  /zmd抽卡记录        显示已保存的记录（快，不请求游戏服务器）；
  /zmd抽卡记录更新    先拉取官方记录，再显示；
  /zmd抽卡记录 武器池  只看一类卡池（限定池、武器池、其他卡池；选项 -k）；

用本地的终末地模板绘制（抽数里程碑的赠礼、免费十连、记录开始的日期）。只有数据访问方式跟随
0.7.2：记录属于某个角色（`character_id`），这个角色是成员的默认角色，或者用 -r 选的角色。

一张图只放一类卡池：
  限定池和武器池     各自一张图，放得下就一列卡池；太高了就分成两列，再到三列，
                      之后再分到更多张图里（限定池 1/2、2/2 ……）；
  其他卡池            常驻池（一次性的新手池放在它下面）以及以后出现的其他类型，
                      每类一列，一张图三列。

限定池包含所有可能出限定干员的卡池：限定寻访、辉光庆典（联合寻访）和重构寻访，最新的在前。
限定寻访里当期 UP 干员算 UP；辉光庆典和重构寻访里每个限定干员都算 UP（plugins/skland_limited.py），
卡池卡片和限定池的统计数字都是这样算。

重构寻访（复刻卡池，卡池 id 为 "rerun_chr_..."）这种卡池类型，上游既不拉取，也不认识这个分类：
这里在上游自己的同步之后，由 /zmd抽卡记录更新 拉取它，分组后的记录给它单独一个列表，而不是算进常驻池。
复刻武器池（"rerun_wpn_..."）同样从常驻池挪到武器池里。
“放得下”是指成图的像素高度（MAX_HEIGHT_PX）。各卡池的高度先在浏览器里测量：页面排好版但不绘制。

通过能在消息下面挂按钮的机器人（官方 QQ 机器人，plugins/quick_buttons.py），成员自己的记录一次只显示
一类。不带参数的指令什么都不画：它回答记录里每一类卡池各一个按钮，另有一个拉取最新记录的按钮；
选中的那一类会被画出来，其他类的按钮跟在下面。其他机器人照旧一次收到所有图片。

上游的处理函数作为模块属性被替换（上游每次调用都会重新查找它），它的 `efgacha` 子指令找回了
0.7.2 去掉的 `-u` 选项，并新增 `-k`。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from nonebot import logger
from nonebot.adapters import Bot
from nonebot.exception import MatcherException
from nonebot.matcher import current_matcher
from playwright.async_api import TimeoutError as PageTimeout
from nonebot_plugin_alconna import Args, At, CustomNode, Match, Option, UniMessage, command_manager
from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_orm import async_scoped_session
from nonebot_plugin_skland import render
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.commands import endfield as upstream_endfield
from nonebot_plugin_skland.commands.selection import check_user_character
from nonebot_plugin_skland.compact import open_html_page, template_to_html
from nonebot_plugin_skland.config import TEMPLATES_DIR, config
from nonebot_plugin_skland.data_source import ef_gacha_pool_data
from nonebot_plugin_skland.db_handler import get_character_gacha_records
from nonebot_plugin_skland.exception import SklandException
from nonebot_plugin_skland.filters import format_timestamp_md
from nonebot_plugin_skland.matcher import skland_command
from nonebot_plugin_skland.model import SkUser
from nonebot_plugin_skland.schemas.endfield.gacha.pool import EfGachaPoolInfo
from nonebot_plugin_skland.schemas.endfield.gacha.statistics import EfGroupedGachaRecord
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.services.auth import CredentialState, refresh_credentials
from nonebot_plugin_skland.services.gacha import group_ef_gacha_records, sync_ef_gacha_records
from nonebot_plugin_skland.utils.message import send_reaction
from nonebot_plugin_user import UserSession, get_user
from sqlalchemy import update as sql_update

from plugins import ef_theme, skland_limited
from plugins.quick_buttons import can_show_buttons, plain, send_buttons
from plugins.shared import SLOW_UPLOAD, heybox_hint, upload_timed_out
from plugins.skland_pools import POOL_CONTENT_URL

LOCAL_DIR = Path(__file__).with_name("templates")
TEMPLATE = "ef_gacha.html.jinja2"
COLUMN_WIDTH, COLUMN_GAP, PAGE_PADDING = 250, 10, 18  # CSS 像素；卡池卡片总是这么宽
MAX_COLUMNS = 3
# 按列数决定的设备缩放：窄图画得大一些，在手机上仍然清晰。
SCALE = {1: 2.5, 2: 2.0, 3: 1.5}
# 发送的图片最大高度，按绘制后的像素算。WebP 每边上限 16383 像素；卡池很多的成员的页面
# 几周来都差不多这么高，发到 QQ 没问题。
MAX_HEIGHT_PX = 15000
# 其他卡池那张图的各列，每列是叠放其中的 (类型, 标题) 列表；这里没列出的类型，以它的卡池名单独一列。
OTHER_COLUMNS = ((("standard", "常驻池"), ("beginner", "新手池")),)
CN = timezone(timedelta(hours=8))
SEVERAL_PICTURES = "抽卡记录将按卡池类型分成多张图片发送"  # plugins.forward_reply_owner 会识别这一行
NO_RECORDS = "暂无抽卡记录，请先发送 /zmd抽卡记录更新 拉取数据"
NO_TOKEN = "这个角色所属的森空岛账号没有保存登录凭证，无法拉取抽卡记录。请重新发送 /skl绑定 扫码"
FETCHED = "已拉取最新记录，新增 {} 条"
SYNC_FAILED = "抽卡记录拉取失败，请稍后再试。如果一直失败，可能是登录凭证过期了，重新发送 /skl绑定 扫码即可"


# ── 上游没有数据的模板值 ───────────────────────────


def _gift_rules() -> dict:
    """卡池 id -> 里程碑赠礼，来自 wiki 索引（plugins.endfield_wiki.data.build_gacha_gifts）。"""
    with contextlib.suppress(Exception):
        from plugins import endfield_wiki

        loaded = endfield_wiki._resolver()
        return ((loaded[0] if loaded else {}).get("calendar") or {}).get("gifts") or {}
    return {}


def gacha_extras(record) -> dict:
    """每个卡池的赠礼，以及记录从哪里开始。

    ef_gifts：{卡池 id: {n: [赠礼]}}：该卡池第 n 次付费抽卡触发的赠礼。
    """
    rules = _gift_rules()
    gifts: dict[str, dict[int, list]] = {}
    first, imported = None, 0
    for pool in record.all_pools:
        for group in pool.records:
            first = group.gacha_ts if first is None else min(first, group.gacha_ts)
            imported += sum(1 for pull in group.pulls if pull.seq_id < 0)  # 由 heybox_import 重建的行
        paid = pool.paid_pulls
        for rule in rules.get(pool.pool_id, []):
            at, n = rule["at"], 0
            while at <= paid:
                gifts.setdefault(pool.pool_id, {}).setdefault(at, []).append({**rule["items"][n % len(rule["items"])], "at": at})
                if not rule["every"]:
                    break
                at, n = at + rule["every"], n + 1
    return {
        "ef_gifts": gifts,
        "ef_since": datetime.fromtimestamp(first, CN).strftime("%Y-%m-%d") if first else "",
        "ef_imported": imported,
    }


# ── 记录：上游的分组，加以修正 ─────────────────────────


class Record(EfGroupedGachaRecord):
    """上游分好组的记录（重构寻访已单独成组），限定池的统计改为三类合在一起。"""

    # 限定池的统计数字：限定寻访、辉光庆典和重构寻访合在一起；共享的小保底沿用上游的（只算限定寻访）

    @property
    def limited_pools(self) -> list[EfGachaPoolInfo]:
        return sorted(self.special_pools + self.joint_pools + self.rerun_pools, key=_newest, reverse=True)

    @property
    def special_total_six(self) -> int:
        return sum(pool.total_six_stars for pool in self.limited_pools)

    @property
    def special_total_spook(self) -> int:
        return sum(pool.total_six_spook or 0 for pool in self.limited_pools)  # 不知道 UP 的卡池（None）不计歪

    @property
    def special_up_count(self) -> int:
        return skland_limited.up_count(self.limited_pools)

    @property
    def special_up_avg(self) -> float | None:
        return skland_limited.up_avg(self.special_pools, self.joint_pools + self.rerun_pools)

    # 武器池：沿用 0.7.2 的算法和属性名（模板在用），见 skland_limited

    @property
    def weapon_total_spook(self) -> int:
        return sum(p.total_six_stars - skland_limited.up_count([p]) for p in self.weapon_pools)

    @property
    def weapon_up_count(self) -> int:
        return skland_limited.up_count(self.weapon_pools)

    @property
    def weapon_up_avg(self) -> float:
        return skland_limited.weapon_up_avg(self.weapon_pools)

    @property
    def weapon_pity(self) -> int:
        return skland_limited.weapon_pity(self.weapon_pools)

    @property
    def rerun_pity(self) -> int:
        """重构寻访的 80 抽保底，像限定寻访一样在它的各个卡池之间继承。"""
        return skland_limited.pity(self.rerun_pools)


def _newest(pool) -> int:
    return max((group.gacha_ts for group in pool.records), default=0)


def regroup(grouped: EfGroupedGachaRecord) -> Record:
    """上游的分组换成 Record（同样的各组卡池）。"""
    return Record(**{name: list(getattr(grouped, name)) for name in type(grouped).model_fields})


# ── 图片 ───────────────────────────────────────────────────────


@dataclass
class Section:
    title: str
    pools: list


@dataclass
class Column:
    sections: list[Section]


@dataclass
class Sheet:
    """一张图。"""

    kind: str  # "special" | "weapon" | "other"
    title: str
    columns: list[Column]
    tiles: tuple[str, ...] = ()
    part: str = ""  # 一类卡池需要好几张图时为 "1/2" 这样的编号
    scale: float = 0.0  # 设备缩放；没有指定时按列数决定（同一组图片共用一个）
    label: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.label = f"{self.title} {self.part}".strip()
        self.scale = self.scale or SCALE[min(len(self.columns), MAX_COLUMNS)]

    @property
    def width(self) -> int:
        return len(self.columns) * COLUMN_WIDTH + (len(self.columns) - 1) * COLUMN_GAP

    @property
    def page_width(self) -> int:
        return self.width + 2 * PAGE_PADDING

    @property
    def col_width(self) -> int:
        return COLUMN_WIDTH

    @property
    def padding(self) -> int:
        return PAGE_PADDING


def _kind(pool) -> str:
    """武器按实际抽到的归类，所有能出限定干员的卡池都算限定池；其余用上游的分类。"""
    if pool.pool_type == "weapon":
        return "weapon"
    return "special" if skland_limited.is_limited(pool) else pool.pool_category


def _split(heights: list[float], columns: int) -> list[list[int]]:
    """把连续的卡池分成 `columns` 段，让最高的一段尽可能矮。"""
    def runs(cap: float) -> list[list[int]]:
        out, total = [[]], 0.0
        for i, h in enumerate(heights):
            if out[-1] and total + COLUMN_GAP + h > cap:
                out.append([])
                total = 0.0
            total += h + (COLUMN_GAP if len(out[-1]) else 0)
            out[-1].append(i)
        return out

    low, high = max(heights), sum(heights) + COLUMN_GAP * len(heights)
    for _ in range(40):
        middle = (low + high) / 2
        low, high = (low, middle) if len(runs(middle)) <= columns else (middle, high)
    return runs(high)


def arrange(heights: list[float], chrome: float) -> list[list[list[int]]]:
    """一类卡池的：图片 -> 列 -> 卡池编号。

    `heights`：各卡池卡片的高度（CSS 像素），按显示顺序；`chrome`：页面上其他所有部分。
    图片能保持在 MAX_HEIGHT_PX 以下就一列，否则两列，再否则三列；再多的卡池接着放到更多的
    三列图片里。单个卡池比整张图还高时，仍然单独占一列。
    """
    for columns in range(1, MAX_COLUMNS + 1):
        runs = _split(heights, columns)
        tallest = max(sum(heights[i] for i in run) + COLUMN_GAP * (len(run) - 1) for run in runs)
        if (chrome + tallest) * SCALE[len(runs)] <= MAX_HEIGHT_PX or len(heights) == 1:
            return [runs]
    room = MAX_HEIGHT_PX / SCALE[MAX_COLUMNS] - chrome
    runs, total = [[]], 0.0
    for i, h in enumerate(heights):
        if runs[-1] and total + COLUMN_GAP + h > room:
            runs.append([])
            total = 0.0
        total += h + (COLUMN_GAP if runs[-1] else 0)
        runs[-1].append(i)
    return [runs[i : i + MAX_COLUMNS] for i in range(0, len(runs), MAX_COLUMNS)]


def _templates(record, sheet: Sheet, *, avatar_url: str, nickname: str, role_id: str, extras: dict) -> dict:
    return {
        "avatar_url": avatar_url,
        "record": record,
        "character": SimpleNamespace(nickname=nickname, role_id=role_id),
        "view": sheet,
        "ef_fonts": ef_theme.fonts_css(),
        **extras,
    }


# 干员和武器图片来自 AKEData：新版本上线当天它就有新图，而插件自己的来源（endfieldtools.dev）
# 在 1.6 上线当天对祀和玉中身还返回 404。地址用资料库的拼法，和资料卡始终一致。


def item_icon(item_id: str) -> str:
    """干员（chr_…）或武器（wpn_…）的图片；武器的图片名取自 wiki 索引（有几个和 id 不同）。"""
    from plugins import endfield_wiki  # 用到时才导入：不依赖插件的加载顺序
    from plugins.endfield_wiki import data as wiki_data

    if not item_id.startswith("wpn_"):
        return wiki_data.char_icon_url(item_id)
    icon = item_id
    with contextlib.suppress(Exception):
        icon = endfield_wiki._resolver()[0]["weapons"].get(item_id, {}).get("icon") or item_id
    return wiki_data.weapon_icon_url(icon)


# 武器池可能没有横幅图（up6_image 为空：1.6 的月映申领和细枝申领）。这时游戏自己的记录页面用两部分拼出来，
# 我们也一样：默认的横条（weapon-banner-background），再把卡池的 up6_banner（一张 254x254 的武器图，
# 内容 API 也会返回）按那个页面 CSS 的位置叠上去（left 68.98%、top -21.92%、横条的 22.07% x 173.97%）。
# 保存在 data/skland_ef_theme/pool_art。
WEAPON_BANNER_BG = "https://web.hycdn.cn/endfield/webview/_gacha/assets/img/weapon-banner-background.108fcc.png"
BANNER_SIZE = (1151, 146)
POOL_ART: dict[str, str] = {}  # 卡池 id -> 横幅图的 data: URL


def _art_dir() -> Path:
    import nonebot_plugin_localstore as store

    path = store.get_data_dir("skland_ef_theme") / "pool_art"
    path.mkdir(parents=True, exist_ok=True)
    return path


def compose_banner(background: bytes, artwork: bytes) -> bytes:
    """按游戏记录页面的画法拼出的横幅图。"""
    import io

    from PIL import Image

    w, h = BANNER_SIZE
    strip = Image.open(io.BytesIO(background)).convert("RGBA").resize((w, h), Image.LANCZOS)
    size = (round(w * 0.220678), round(h * 1.739726))
    weapon = Image.open(io.BytesIO(artwork)).convert("RGBA").resize(size, Image.LANCZOS)
    top = round(h * 0.219178)  # 图片从横条上方这么远的地方开始
    strip.alpha_composite(weapon.crop((0, top, size[0], top + h)), (round(w * 0.689835), 0))
    out = io.BytesIO()
    strip.save(out, "WEBP", quality=92)
    return out.getvalue()


async def _fetch(client, url: str, cache: Path | None = None) -> bytes:
    if cache and cache.exists():
        return cache.read_bytes()
    response = await client.get(url)
    response.raise_for_status()
    if cache:
        cache.with_suffix(".tmp").write_bytes(response.content)
        cache.with_suffix(".tmp").replace(cache)
    return response.content


async def _pool_art(pools, server_id: str) -> None:
    """为没有横幅图的武器池填好 POOL_ART。"""
    import base64

    import httpx

    wanted = [p for p in pools if not p.up6_img and p.pool_type == "weapon" and p.pool_id not in POOL_ART]
    if not wanted:
        return
    folder = _art_dir()
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), follow_redirects=True) as client:
        for pool in wanted:
            cached = folder / f"{pool.pool_id}.webp"
            if not cached.exists():
                response = await client.get(POOL_CONTENT_URL, params={"pool_id": pool.pool_id, "server_id": server_id, "lang": "zh-cn"})
                data = (response.json().get("data") or {}).get("pool") or {}
                if data.get("up6_image"):
                    pool.up6_img = data["up6_image"]  # 卡池表生成之后才公布的
                    continue
                if not data.get("up6_banner"):
                    continue
                background = await _fetch(client, WEAPON_BANNER_BG, folder / "weapon-banner-background.png")
                banner = await asyncio.to_thread(compose_banner, background, await _fetch(client, data["up6_banner"]))
                cached.with_suffix(".tmp").write_bytes(banner)
                cached.with_suffix(".tmp").replace(cached)
                logger.info(f"Endfield pool banner drawn for {pool.pool_id} from the official up6_banner")
            POOL_ART[pool.pool_id] = "data:image/webp;base64," + base64.b64encode(cached.read_bytes()).decode()


def pool_art(pool_id: str) -> str:
    return POOL_ART.get(pool_id, "")


FILTERS = {"format_timestamp_md": format_timestamp_md, "ef_charId_to_avatarUrl": item_icon, "ef_pool_art": pool_art}
_MEASURE = """async () => {
  await document.fonts.ready;
  const pools = [...document.querySelectorAll('.pool')].map(e => e.getBoundingClientRect().height);
  return {total: document.documentElement.scrollHeight, pools};
}"""


async def measure(record, sheet: Sheet, **context) -> tuple[list[float], float]:
    """（每个卡池卡片的高度，页面其余部分的高度），所有卡池放在同一列时测量。"""
    html = await template_to_html(str(LOCAL_DIR), TEMPLATE, filters=FILTERS, **_templates(record, sheet, **context))
    viewport = {"width": sheet.page_width, "height": 1}
    async with open_html_page(html, template_path=LOCAL_DIR.as_uri(), wait_until="domcontentloaded", device_scale_factor=1, viewport=viewport, base_url=TEMPLATES_DIR.as_uri()) as page:
        sizes = await page.evaluate(_MEASURE)
    heights = [float(h) for h in sizes["pools"]]
    return heights, float(sizes["total"]) - sum(heights) - COLUMN_GAP * max(0, len(heights) - 1)


# 按高度排版的两类：(类型, 图片标题, 统计块)；其余类型都放进 OTHER_TITLE 那几张图
MEASURED = (("special", "限定池", ("special",)), ("weapon", "武器池", ("weapon", "arsenal")))
OTHER_TITLE = "其他卡池"


def _by_kind(record, begin: int | None, limit: int | None) -> dict[str, list]:
    """类型 -> 卡池，按显示顺序。"""
    by_kind: dict[str, list] = {}
    for pool in record.all_pools:
        by_kind.setdefault(_kind(pool), []).append(pool)
    if "special" in by_kind:  # 限定寻访、辉光庆典和重构寻访来自三个列表
        by_kind["special"].sort(key=_newest, reverse=True)
    if begin is not None or limit is not None:  # -b / -l 仍然按每类分别数卡池
        by_kind = {kind: pools[begin or 0 : limit] for kind, pools in by_kind.items()}
    return by_kind


def kinds_of(record, *, begin: int | None = None, limit: int | None = None) -> list[str]:
    """记录里有的卡池类型（图片的标题），按显示顺序；和 plan 画出的一样，但不用浏览器。"""
    by_kind = _by_kind(record, begin, limit)
    kinds = [title for kind, title, _ in MEASURED if by_kind.pop(kind, None)]
    return kinds + [OTHER_TITLE] if any(by_kind.values()) else kinds


async def _measured_sheets(record, kind: str, title: str, tiles: tuple[str, ...], pools: list, **context) -> list[Sheet]:
    """限定池或武器池：先在浏览器里量出各卡池的高度，再分列、分图。"""
    try:
        heights, chrome = await measure(record, Sheet(kind, title, [Column([Section(title, pools)])], tiles), **context)
        pictures = arrange(heights, chrome) if len(heights) == len(pools) else [[list(range(len(pools)))]]
    except Exception as e:  # 一列很高也仍然是一张正确的图
        logger.warning(f"Endfield gacha page: layout not measured ({type(e).__name__}: {e})")
        pictures = [[list(range(len(pools)))]]
    sheets = []
    for n, runs in enumerate(pictures, 1):
        columns = [Column([Section(title if not i else f"{title} 续", [pools[j] for j in run])]) for i, run in enumerate(runs)]
        several = len(pictures) > 1  # 这种情况下每张图都按三列定尺寸，最后一张也是
        sheets.append(Sheet(kind, title, columns, tiles if n == 1 else (), f"{n}/{len(pictures)}" if several else "", SCALE[MAX_COLUMNS] if several else 0.0))
    return sheets


def _other_sheets(by_kind: dict[str, list]) -> list[Sheet]:
    """其余类型：OTHER_COLUMNS 里的叠成一列，其他每类一列，一张图三列。"""
    others: list[tuple[tuple[str, ...], Column]] = []  # (列里的类型, 列)
    for stacked in OTHER_COLUMNS:
        sections = [(kind, Section(title, by_kind.pop(kind))) for kind, title in stacked if by_kind.get(kind)]
        if sections:
            others.append((tuple(kind for kind, _ in sections), Column([section for _, section in sections])))
    others += [((), Column([Section(pools[0].pool_name, pools)])) for pools in by_kind.values() if pools]
    groups = [others[i : i + MAX_COLUMNS] for i in range(0, len(others), MAX_COLUMNS)]
    return [
        Sheet("other", OTHER_TITLE, [column for _, column in group], tuple(kind for kinds, _ in group for kind in kinds), f"{n}/{len(groups)}" if len(groups) > 1 else "")
        for n, group in enumerate(groups, 1)
    ]


async def plan(record, *, begin: int | None = None, limit: int | None = None, kind: str | None = None, **context) -> list[Sheet]:
    """这份记录的图片，按发送顺序排列；给了 `kind`（图片标题）时只排那一类，也只测量那一类。"""
    by_kind = _by_kind(record, begin, limit)
    sheets: list[Sheet] = []
    for key, title, tiles in MEASURED:
        pools = by_kind.pop(key, [])
        if pools and kind in (None, title):
            sheets += await _measured_sheets(record, key, title, tiles, pools, **context)
    return sheets + _other_sheets(by_kind) if kind in (None, OTHER_TITLE) else sheets


async def render_sheet(record, sheet: Sheet, **context) -> bytes:
    # render.template_to_pic 在调用时才查找：skland_compact_images 会把图片转成 WebP。
    return await render.template_to_pic(
        template_path=str(LOCAL_DIR),
        template_name=TEMPLATE,
        templates=_templates(record, sheet, **context),
        filters=FILTERS,
        pages={"viewport": {"width": sheet.page_width, "height": 1}, "base_url": TEMPLATES_DIR.as_uri()},
        device_scale_factor=sheet.scale,
        screenshot_timeout=config.render_timeout,
    )


async def render_kinds(
    record, *, avatar_url: str, nickname: str, role_id: str, begin: int | None = None, limit: int | None = None, kind: str | None = None,
) -> tuple[list[tuple[Sheet, bytes]], list[str]]:
    """记录的图片及其页面数据（全部，或只有 `kind` 那一类），以及记录里有的卡池类型（KINDS），按显示顺序排列。"""
    try:
        extras = gacha_extras(record)
    except Exception as e:  # 页面照样渲染，只是没有那些标记
        logger.warning(f"Endfield gacha page: extras unavailable: {type(e).__name__}: {e}")
        extras = {"ef_gifts": {}, "ef_since": "", "ef_imported": 0}
    context = {"avatar_url": avatar_url, "nickname": nickname, "role_id": role_id, "extras": extras}
    sheets = await plan(record, begin=begin, limit=limit, kind=kind, **context)
    limiter = asyncio.Semaphore(3)

    async def bounded(sheet: Sheet) -> bytes:
        async with limiter:
            return await render_sheet(record, sheet, **context)

    return list(zip(sheets, await asyncio.gather(*(bounded(sheet) for sheet in sheets)))), kinds_of(record, begin=begin, limit=limit)


# ── 指令 ────────────────────────────────────────────────────────


def _update_requested() -> bool:
    matcher = current_matcher.get(None)
    result = getattr(matcher.state.get(ALCONNA_RESULT), "result", None) if matcher else None
    return bool(result and result.find("efgacha.update"))


KINDS = (*(title for _, title, _ in MEASURED), OTHER_TITLE)  # 图片的标题，也是 -k 接受的值


def _kind_requested() -> str | None:
    matcher = current_matcher.get(None)
    result = getattr(matcher.state.get(ALCONNA_RESULT), "result", None) if matcher else None
    value = result.query("efgacha.kind.kind") if result else None
    return str(value) if value else None


def kind_menu(kinds: list[str], role: str, total: int, fetched: int | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    """还没画任何东西之前给成员的选项：记录里每类卡池一个按钮，外加一个拉取最新记录的按钮，
    刚拉取过时（`fetched` 是新增的记录数）除外。"""
    title = f"**抽卡记录** {FETCHED.format(fetched)}。选一个卡池查看" if fetched is not None else f"**抽卡记录** 共 {total} 抽，选一个卡池查看"
    rows = [[(kind, f"/zmd抽卡记录 {kind}{role}") for kind in kinds]]
    if fetched is None:
        rows.append([("拉取最新记录", f"/zmd抽卡记录更新{role}")])
    return title, rows


async def _pool_marks(gacha_data, server_id: str) -> None:
    """有 UP 的卡池的 UP 干员 / 武器：先查本地表，查不到再问游戏的 API。"""
    pools = gacha_data.special_pools + gacha_data.joint_pools + gacha_data.weapon_pools + gacha_data.rerun_pools
    for pool in pools:
        local_pool = ef_gacha_pool_data.get_pool(pool.pool_id)
        if local_pool:
            pool.up_six_chars = local_pool.up_six_char_ids
            pool.up6_img = local_pool.up6_image or local_pool.rotate_image
            pool.up6_name = local_pool.up_six_display_name
            continue
        try:
            content = await SklandAPI.get_ef_gacha_content(pool.pool_id, server_id)
            pool.up_six_chars = content.pool.up_six_char_ids
            pool.up6_img = content.pool.up6_image or content.pool.rotate_image
            pool.up6_name = content.pool.up_six_display_name
        except Exception as e:
            logger.warning(f"Endfield pool metadata unavailable: {type(e).__name__}")
    try:
        await _pool_art(pools, server_id)
    except Exception as e:  # 卡片画出来没有横幅图
        logger.warning(f"Endfield pool art unavailable: {type(e).__name__}")


async def ef_gacha_history_handler(user_session: UserSession, *args, **kwargs) -> None:
    """出图或发送意外出错时，至少告诉成员没成功（不能一声不响）。森空岛的错误照旧交给上游；
    截图超时由 skland_compact_images 说过了，这里不再重复。"""
    try:
        await _ef_gacha_history(user_session, *args, **kwargs)
    except (MatcherException, SklandException, PageTimeout):
        raise
    except Exception as e:
        if upload_timed_out(e):  # 图已经做好，只是上传太久
            logger.warning("Endfield gacha page: picture upload timed out")
            await UniMessage.text(SLOW_UPLOAD).send(reply_to=True)
            return
        logger.warning(f"Endfield gacha page failed: {type(e).__name__}")
        send_reaction(user_session, "fail")
        await UniMessage.text("抽卡记录图片生成失败，请稍后再试").send(reply_to=True)


async def _role_card(session, credentials: CredentialState, account: tuple[int, int], remote_user_id: str, role_id: str, server_id: str) -> tuple[str, float | None]:
    """（头像，建号时间），取自森空岛的角色卡；令牌刷新过就存回去。取不到时为（""，None）。"""

    @refresh_credentials
    async def card(state: CredentialState):
        return await SklandAPI.endfield_card(CRED(cred=state.cred, token=state.cred_token), user_id=remote_user_id, role_id=role_id, server_id=server_id)

    original = (credentials.cred, credentials.cred_token)
    avatar_url, created = "", None
    try:
        if remote_user_id and role_id:
            base = (await card(credentials)).base
            avatar_url = base.avatarUrl
            if base.createTime.isdigit():
                created = int(base.createTime) / (1000 if int(base.createTime) > 10**11 else 1)
    except Exception as e:  # 头像只是装饰；成员要的是记录
        logger.warning(f"Endfield profile unavailable: {type(e).__name__}")
    finally:
        if (credentials.cred, credentials.cred_token) != original:
            account_id, owner_id = account
            await session.execute(
                sql_update(SkUser)
                .where(SkUser.id == account_id, SkUser.owner_id == owner_id)
                .values(cred=credentials.cred, cred_token=credentials.cred_token)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    return avatar_url, created


async def _ef_gacha_history(
    user_session: UserSession,
    session: async_scoped_session,
    begin: Match[int],
    limit: Match[int],
    target: Match[At | int],
    bot: Bot,
    *,
    role_index: int | None = None,
) -> None:
    update = _update_requested()
    if target.available:
        target_platform_id = target.result.target if isinstance(target.result, At) else target.result
        target_id = (await get_user(user_session.platform, str(target_platform_id))).id
    else:
        target_id = user_session.user_id
    selected = await check_user_character(target_id, user_session, session, app_code="endfield", role_index=role_index)
    if selected is None:  # 原因已经发给成员了
        return
    user, character = selected
    # 从这里开始只用普通值：在发出任何请求之前读事务就结束了。
    account_id, owner_id, remote_user_id = user.id, user.owner_id, user.skland_user_id
    credentials = CredentialState(user.access_token, user.cred, user.cred_token)
    character_id, uid = character.id, character.uid
    role_id, server_id, nickname = character.role_id, character.channel_master_id, character.nickname
    await session.rollback()
    send_reaction(user_session, "processing")

    new_count = 0
    if update:
        if not credentials.access_token:
            send_reaction(user_session, "fail")
            await UniMessage(NO_TOKEN).send(at_sender=True)
            return
        try:
            _, new_count = await sync_ef_gacha_records(
                session, character_id=character_id, uid=uid, server_id=server_id, access_token=credentials.access_token
            )
        except SklandException as e:
            await session.rollback()
            logger.warning(f"Endfield gacha update failed: {type(e).__name__}")
            send_reaction(user_session, "fail")
            await UniMessage(SYNC_FAILED).send(at_sender=True)
            return
    records = await get_character_gacha_records(character_id, session)
    earliest, imported = min((r.gacha_ts for r in records), default=None), any((r.pos or 0) < 0 for r in records)  # 会话结束前先取出来
    gacha_data = regroup(group_ef_gacha_records(records))
    await session.rollback()
    if not gacha_data.total_pulls:
        await UniMessage.text("已拉取，暂时没有抽卡记录" if update else NO_RECORDS).send(reply_to=True)
        send_reaction(user_session, "done")
        return
    kind = _kind_requested()
    # 能提供按钮的地方，成员自己的记录一次只显示一类卡池，
    # 不带参数的指令回答可选的类型：选中之前什么都不画，也用不到卡池图和头像。
    one_at_a_time = can_show_buttons() and not target.available
    menu = one_at_a_time and kind is None
    # 小黑盒提醒只对看自己记录的人说，要不要说还得看建号时间（角色卡上）
    hinting = not target.available and earliest is not None and not imported
    role_card = functools.partial(_role_card, session, credentials, (account_id, owner_id), remote_user_id, role_id, server_id)
    card = await role_card() if hinting or not menu else None
    avatar_url, created = card or ("", None)
    hint = heybox_hint(earliest, imported, "endfield", created, character_id) if hinting else ""
    role = f" 角色{role_index}" if role_index is not None else ""
    span = dict(begin=begin.result if begin.available else None, limit=limit.result if limit.available else None)
    if menu:
        kinds = kinds_of(gacha_data, **span)
        title, rows = kind_menu(kinds, role, gacha_data.total_pulls, new_count if update else None)
        # 无文字兜底：只有能显示按钮的机器人走到这里；按钮被拒时下面改画第一类，其他类在图后用文字列出
        if kinds and await send_buttons(title + (f"\n{plain(hint)}" if hint else ""), rows):
            logger.info(f"Endfield gacha: kinds offered {kinds}, {gacha_data.total_pulls} pulls, {new_count} new")
            send_reaction(user_session, "done")
            return
        kind = kinds[0] if kinds else None  # 按钮被拒绝了：画第一类，其他类在下面用文字列出
        if card is None and kinds:
            avatar_url, _ = await role_card()
    await _pool_marks(gacha_data, server_id)
    skland_limited.mark(gacha_data.limited_pools)
    pages, kinds = await render_kinds(gacha_data, avatar_url=avatar_url, nickname=nickname, role_id=role_id, **span, kind=kind)
    if not pages:
        await UniMessage.text(f"没有「{kind}」的抽卡记录，可以看的有：{'、'.join(kinds)}" if kind and kinds else NO_RECORDS).send(reply_to=True)
        send_reaction(user_session, "done")
        return
    if len(pages) == 1:
        await UniMessage.image(raw=pages[0][1]).send(at_sender=True)
    elif user_session.platform == "QQClient":
        await UniMessage.text(SEVERAL_PICTURES).send(reply_to=True)
        await UniMessage.reference(*(CustomNode(bot.self_id, f"{nickname} | {sheet.label}", UniMessage.image(raw=image)) for sheet, image in pages)).send()
    else:
        for _, image in pages:
            await UniMessage.image(raw=image).send()
    if one_at_a_time:
        shown = pages[0][0].title
        others = [(title, f"/zmd抽卡记录 {title}{role}") for title in kinds if title != shown]
        said = (["其他卡池：" + "　".join(command for _, command in others)] if others else []) + ([hint] if hint else [])
        await send_buttons(f"**抽卡记录** 当前：{shown}" + (f"\n{plain(hint)}" if hint else ""), [others, [("拉取最新记录", f"/zmd抽卡记录更新{role}")]], fallback="\n".join(said))
    elif said := ([FETCHED.format(new_count)] if update and not can_show_buttons() else []) + ([hint] if hint else []):  # 没有按钮标题可写新增条数
        await UniMessage.text("\n".join(said)).send()
    logger.info(
        f"Endfield gacha page sent: {gacha_data.total_pulls} pulls "
        f"(characters {gacha_data.char_total_pulls} + weapons {gacha_data.weapon_total_pulls}), {new_count} new, "
        f"pictures {[f'{sheet.label} x{len(sheet.columns)}' for sheet, _ in pages]}"
    )
    send_reaction(user_session, "done")


# ── 安装 ───────────────────────────────────────────────────────

_upstream_handler = upstream_endfield.ef_gacha_history_handler
if getattr(_upstream_handler, "_local_gacha_page", False):
    raise RuntimeError("Endfield gacha page already installed")
ef_gacha_history_handler._local_gacha_page = True
upstream_endfield.ef_gacha_history_handler = ef_gacha_history_handler

_efgacha = next(option for option in skland_command.options if getattr(option, "name", "") == "efgacha")
if not any("update" in getattr(option, "aliases", ()) or getattr(option, "name", "") == "--update" for option in _efgacha.options):
    with command_manager.update(skland_command):
        _efgacha.options.append(Option("-u|--update|update", help_text="先从官方接口拉取最新记录再展示"))
if not any(getattr(option, "name", "") == "--kind" for option in _efgacha.options):
    with command_manager.update(skland_command):
        _efgacha.options.append(Option("-k|--kind|kind", Args["kind", str], help_text="只看一种卡池：限定池、武器池、其他卡池"))


def verify() -> None:
    """替换和 -u 选项都还在（启动时调用）。"""
    if upstream_endfield.ef_gacha_history_handler is not ef_gacha_history_handler:
        raise RuntimeError("Endfield gacha page handler was replaced")
    prefix = next(iter(skland_command.prefixes), "")
    if not skland_command.parse(f"{prefix}skland efgacha -u").find("efgacha.update"):
        raise RuntimeError("Endfield gacha -u option is not active")
    if skland_command.parse(f"{prefix}skland efgacha -k {KINDS[1]}").query("efgacha.kind.kind") != KINDS[1]:
        logger.warning("Endfield gacha -k option is not active: a kind of pool cannot be asked for by name")
