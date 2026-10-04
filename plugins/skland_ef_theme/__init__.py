"""Endfield-themed copies of nonebot_plugin_skland's /zmd开盒 and /zmd抽卡记录 templates.

site-packages stays untouched: render.template_to_pic (already wrapped by skland_compact_images)
is wrapped once more and, for the two Endfield templates only, `template_path` points at
./templates. The rendered page then lives in ./templates, so relative "../images/..." paths
would break: the card template sets a <base> back to the package's templates folder, and the
gacha template uses no relative paths. Each override is pinned to the sha256 of the upstream
files it was derived from; after an upstream update the original template is used again.

The gacha page also gets two things upstream has no data for (see `_gacha_extras`): the gifts the
game hands out at pull milestones, which are not pulls and must not be read as such, and the date
the records start at, because the official API drops older pulls.
"""

import contextlib
import functools
import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nonebot import logger, require

require("nonebot_plugin_skland")
require("plugins.skland_compact_images")

from nonebot_plugin_skland import render

from plugins import ef_theme
from nonebot_plugin_skland.config import TEMPLATES_DIR

LOCAL_DIR = Path(__file__).with_name("templates")
# template -> upstream files it depends on, with the sha256 they had when the local copy was made
UPSTREAM = {
    "endfield_card.html.jinja2": {
        "endfield_card.html.jinja2": "b2b6958d3280b55fb20a8cf1fa510c54ce7ece3dcce097076d0c1b914a749264",
        "endfield_macros.html.jinja2": "59f8728282438f40573172f4fe776abbfb17d420fff38e52dc5da6555e5194a5",
        "index.css": "e84e0dadcadc3372892ef8cd413302f5779a67e2ffb8df9b8a45e47f6c25a83b",
    },
    "ef_gacha.html.jinja2": {
        "ef_gacha.html.jinja2": "237dc08954b0f481817af9da24acc456add8ca3c2126132821d5ea55a8759987",
        "ef_gacha_macros.html.jinja2": "77d7d9a41998c705b742d53929e43e0cde2a368cc79278f3473c1773263c91f7",
        "index.css": "e84e0dadcadc3372892ef8cd413302f5779a67e2ffb8df9b8a45e47f6c25a83b",
    },
}


def _unchanged(files: dict[str, str]) -> bool:
    return all(hashlib.sha256((TEMPLATES_DIR / name).read_bytes()).hexdigest() == digest for name, digest in files.items())


ACTIVE = frozenset(name for name, files in UPSTREAM.items() if _unchanged(files))
for name in UPSTREAM.keys() - ACTIVE:
    logger.warning(f"Skland Endfield theme disabled for {name}: upstream template changed, re-audit needed")

CN = timezone(timedelta(hours=8))


def _gift_rules() -> dict:
    """Pool id -> milestone gifts, from the wiki index (plugins.endfield_wiki.data.build_gacha_gifts)."""
    with contextlib.suppress(Exception):
        from plugins import endfield_wiki

        loaded = endfield_wiki._resolver()
        return ((loaded[0] if loaded else {}).get("calendar") or {}).get("gifts") or {}
    return {}


def _gacha_extras(record) -> dict:
    """Template values for ef_gacha: gifts per pool, and where the records start.

    ef_gifts: {pool id: {n: [gift]}}: the gifts triggered by the n-th paid pull of that pool.
    """
    rules = _gift_rules()
    gifts: dict[str, dict[int, list]] = {}
    first, imported = None, 0
    for pool in record.all_pools:
        for group in pool.records:
            first = group.gacha_ts if first is None else min(first, group.gacha_ts)
            imported += sum(1 for pull in group.pulls if pull.seq_id < 0)  # rows rebuilt by heybox_import
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


_previous = render.template_to_pic
_signature = inspect.signature(_previous)
if getattr(_previous, "_ef_theme", False):
    raise RuntimeError("Skland Endfield theme already installed")


@functools.wraps(_previous)
async def themed_template_to_pic(*args, **kwargs):
    bound = _signature.bind(*args, **kwargs)
    if bound.arguments.get("template_name") in ACTIVE:
        bound.arguments["template_path"] = str(LOCAL_DIR)
        templates = {**(bound.arguments.get("templates") or {}), "ef_fonts": ef_theme.fonts_css()}
        if bound.arguments["template_name"] == "ef_gacha.html.jinja2":
            try:
                templates.update(_gacha_extras(templates["record"]))
            except Exception as e:  # the page still renders, only without the marks
                logger.warning(f"Skland Endfield theme: gacha extras unavailable: {type(e).__name__}: {e}")
        bound.arguments["templates"] = templates
    return await _previous(*bound.args, **bound.kwargs)


themed_template_to_pic._ef_theme = True
render.template_to_pic = themed_template_to_pic
