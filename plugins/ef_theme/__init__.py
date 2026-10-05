"""Shared Arknights: Endfield look for every rendered image (not a NoneBot plugin, just a module).

Industrial palette: warm light-grey paper with a fine grid, black ink, the signature yellow as the
only accent, square corners, thin rules, hazard stripes, condensed Latin type for numbers/labels.
CJK text stays on WenQuanYi Zen Hei (the only CJK font installed).

Usage: put `ef_theme.css()` first in the page <style>, then the page's own rules using the
--ef-* variables and the .ef-* components below.
"""

from __future__ import annotations

from pathlib import Path

FONT_DIR = Path(__file__).with_name("fonts")

YELLOW = "#ffe100"
INK = "#151515"

_FONTS = "".join(
    f'@font-face {{ font-family: "EF Num"; font-weight: {weight}; src: url("{(FONT_DIR / f"BarlowCondensed-{name}.ttf").as_uri()}"); }}\n'
    for weight, name in ((500, "Medium"), (600, "SemiBold"), (700, "Bold"))
)

_BASE = """
:root {
  --ef-bg: #e3e3df; --ef-grid: rgba(20,20,20,.045);
  --ef-panel: #f5f5f2; --ef-panel-2: #ebebe6; --ef-line: #cbcbc4; --ef-line-2: #deded8;
  --ef-ink: #151515; --ef-sub: #66665f; --ef-faint: #96968e;
  --ef-yellow: #ffe100; --ef-yellow-2: #f2d500; --ef-dark: #1b1c1e; --ef-dark-2: #2b2c2f;
  --ef-silver: #b7bcc3; --ef-copper: #c2662c;
  --ef-red: #e0452b; --ef-green: #2a9358; --ef-blue: #2f6fbe;
  --ef-cjk: "WenQuanYi Zen Hei", sans-serif;
  --ef-num: "EF Num", "Liberation Sans Narrow", "WenQuanYi Zen Hei", sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body.ef { font-family: var(--ef-cjk); color: var(--ef-ink); background-color: var(--ef-bg);
  background-image: linear-gradient(var(--ef-grid) 1px, transparent 1px), linear-gradient(90deg, var(--ef-grid) 1px, transparent 1px);
  background-size: 24px 24px; }
.ef-num { font-family: var(--ef-num); font-variant-numeric: tabular-nums; letter-spacing: .2px; }

/* page header: dark plate, yellow spine, hazard strip */
.ef-head { position: relative; background: var(--ef-dark); color: #f4f4f0; padding: 20px 26px 22px 34px; overflow: hidden; }
.ef-head:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 10px; background: var(--ef-yellow); }
.ef-head:after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 7px;
  background: repeating-linear-gradient(-45deg, var(--ef-yellow) 0 12px, var(--ef-dark) 12px 24px); }
.ef-head .k { font-family: var(--ef-num); font-weight: 600; font-size: 15px; letter-spacing: 4px; color: var(--ef-yellow); text-transform: uppercase; }
.ef-head h1 { font-size: 40px; font-weight: 900; margin-top: 6px; letter-spacing: 1px; }
.ef-head h1 em { font-style: normal; color: var(--ef-yellow); }
.ef-head p { margin-top: 6px; font-size: 17px; color: #b9b9b2; }
.ef-head .code { position: absolute; right: 24px; top: 18px; text-align: right; font-family: var(--ef-num); font-weight: 600;
  font-size: 14px; letter-spacing: 2px; color: #8d8d86; line-height: 1.5; }
.ef-head .code b { display: block; font-size: 30px; color: #f4f4f0; letter-spacing: 1px; }

/* section panel */
.ef-sec { margin-top: 16px; background: var(--ef-panel); border: 1px solid var(--ef-line); }
.ef-sec > h3 { display: flex; align-items: center; gap: 10px; padding: 10px 16px; font-size: 21px; font-weight: 900;
  border-bottom: 2px solid var(--ef-ink); background: var(--ef-panel); }
.ef-sec > h3:before { content: ""; width: 6px; height: 20px; background: var(--ef-yellow); box-shadow: 0 0 0 1px var(--ef-ink); }
.ef-sec > h3 small { margin-left: auto; font-family: var(--ef-num); font-weight: 600; font-size: 14px; letter-spacing: 1px; color: var(--ef-sub); }
.ef-sec > h3 small.cjk { font-family: var(--ef-cjk); font-weight: 400; letter-spacing: 0; }

/* small parts */
.ef-tag { display: inline-block; font-size: 13px; padding: 1px 8px; border: 1px solid var(--ef-ink); background: var(--ef-panel); }
.ef-tag.y { background: var(--ef-yellow); } .ef-tag.d { background: var(--ef-ink); color: #f4f4f0; }
.ef-rank { display: inline-block; width: 26px; height: 26px; line-height: 26px; text-align: center; font-family: var(--ef-num);
  font-weight: 700; font-size: 17px; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
.ef-rank.r1 { background: var(--ef-yellow); border-color: var(--ef-ink); }
.ef-rank.r2 { background: var(--ef-silver); color: var(--ef-ink); border-color: var(--ef-ink); } .ef-rank.r3 { background: var(--ef-copper); color: #fff; border-color: var(--ef-ink); }
.ef-empty { padding: 14px 16px; color: var(--ef-faint); font-size: 16px; }
.ef-foot { margin-top: 14px; display: flex; justify-content: space-between; align-items: center; gap: 16px; font-size: 13px; color: var(--ef-sub); line-height: 1.6; }
.ef-foot .mark { font-family: var(--ef-num); font-weight: 600; letter-spacing: 3px; color: var(--ef-ink); white-space: nowrap; }
.ef-foot .mark:before { content: ""; display: inline-block; width: 18px; height: 8px; margin-right: 8px; vertical-align: 1px;
  background: repeating-linear-gradient(-45deg, var(--ef-yellow) 0 4px, var(--ef-ink) 4px 8px); }
.ef-tail { position: relative; margin-top: 16px; min-height: 250px; padding: 22px 26px 24px 34px; overflow: hidden; background: var(--ef-dark); color: #b9b9b2; }
.ef-tail[hidden] { display: none; }
.ef-tail:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 10px; background: var(--ef-yellow); }
.ef-tail:after { content: ""; position: absolute; left: 0; right: 0; top: 0; height: 6px;
  background: repeating-linear-gradient(-45deg, var(--ef-yellow) 0 12px, var(--ef-dark) 12px 24px); }
.ef-tail .k { font-family: var(--ef-num); font-weight: 600; font-size: 14px; letter-spacing: 4px; color: var(--ef-yellow); text-transform: uppercase; }
.ef-tail .t { margin: 4px 0 10px; font-size: 22px; font-weight: 900; color: #f4f4f0; }
.ef-tail .ln { position: relative; padding-left: 16px; font-size: 15px; line-height: 1.95; }
.ef-tail .ln:before { content: ""; position: absolute; left: 0; top: 12px; width: 6px; height: 6px; background: var(--ef-yellow); }
.ef-tail .ln b { font-weight: 700; color: #f4f4f0; }
.ef-tail .wm { position: absolute; right: 18px; bottom: -26px; font-family: var(--ef-num); font-weight: 700; font-size: 132px; line-height: 1;
  letter-spacing: 6px; color: rgba(255,255,255,.055); white-space: nowrap; }
"""


def shrink(data: bytes, scale: float, quality: int = 72, subsampling: int = 2) -> bytes:
    """Downscale + re-encode a rendered image as JPEG; upload time to QQ grows with the size.

    subsampling 2 = 4:2:0 (smallest); 0 = 4:4:4, which keeps small coloured text sharp for ~15% more bytes.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        image = image.convert("RGB")
        if scale != 1:
            image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        out = BytesIO()
        image.save(out, "JPEG", quality=quality, optimize=True, progressive=True, subsampling=subsampling)
    return out.getvalue() if out.tell() < len(data) else data


PAGE_SCALE = 1.25  # device scale factor of the pages rendered through render_page
PAGE_QUALITY = 65
WEBP_MAX_SIDE = 16383  # format limit


def to_webp(data: bytes, quality: int = 55, scale: float = 1) -> bytes:
    """Re-encode a rendered PNG as WebP: about 40% of the bytes of a JPEG that looks as sharp.

    QQ uploads from this host drop to ~15 KB/s in the evening, so every 100 KB costs several seconds.
    Pages longer than the format allows are scaled down to fit.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        image = image.convert("RGB")
        scale = min(scale, WEBP_MAX_SIDE / max(image.size))
        if scale < 1:
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        out = BytesIO()
        image.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue()


async def render_page(html: str, width: int, *, height: int = 800, scale: float = PAGE_SCALE, quality: int = PAGE_QUALITY, template_path: str | None = None) -> bytes:
    """Screenshot a page with htmlrender and return it as WebP.

    Measured on an operator card: JPEG q85 at 1x was 380 KB; this (1.25x, q65) is ~210 KB and sharper.
    """
    import asyncio

    from nonebot_plugin_htmlrender import html_to_pic

    extra = {"template_path": template_path} if template_path else {}
    image = await html_to_pic(html, type="png", device_scale_factor=scale, viewport={"width": width, "height": height}, **extra)
    return await asyncio.to_thread(to_webp, image, quality)


def fonts_css() -> str:
    """@font-face rules only (absolute file:// URLs of this checkout), for static templates."""
    return _FONTS


def css() -> str:
    """Fonts + variables + shared components; prepend to each page's own CSS."""
    return _FONTS + _BASE


def head(kicker: str, title: str, subtitle: str = "", code: str = "") -> str:
    """Dark header plate. `title` may contain <em> for the yellow part; others are plain text."""
    code_html = f'<div class="code">{code}</div>' if code else ""
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    return f'<div class="ef-head">{code_html}<div class="k">{kicker}</div><h1>{title}</h1>{sub}</div>'


TAIL_RATIO = 1.75  # a page taller than this many widths fills a phone screen in QQ's viewer
TAIL_NOTES = (
    "指令必须以 <b>/</b> 开头，全部指令发 <b>/skl帮助</b> 查看",
    "数据随游戏版本自动更新，仅供参考，请以游戏内为准",
    "手机上点开长图时，这一块会被「查看原图」等按钮挡住，所以不放重要内容",
)


def foot(text: str, mark: str = "PRIESTESS SYSTEM", notes: tuple[str, ...] | list[str] | None = None, tail: bool = True, title: str = "读图说明") -> str:
    """Footer line. On tall pages a plate of notes is put above it (`notes`, HTML allowed).

    QQ's mobile viewer lays its 查看原图 / save / forward buttons over the bottom of a picture
    that fills the screen. The plate takes that spot so the last rows of content stay readable;
    it is switched on in the page itself, where the final height is known.
    """
    line = f'<div class="ef-foot"><span>{text}</span><span class="mark">{mark}</span></div>'
    if not tail:
        return line
    lines = "".join(f'<div class="ln">{note}</div>' for note in (notes or TAIL_NOTES))
    plate = f'<div class="ef-tail" hidden><div class="k">Notes</div><div class="t">{title}</div>{lines}<span class="wm">ENDFIELD</span></div>'
    switch = (
        "<script>(function(){var t=document.querySelector('.ef-tail');"
        f"if(t&&document.documentElement.scrollHeight>window.innerWidth*{TAIL_RATIO})t.hidden=false;}})();</script>"
    )
    return plate + line + switch
