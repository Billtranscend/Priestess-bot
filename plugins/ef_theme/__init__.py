"""所有渲染图片共用的明日方舟：终末地样式（不是 NoneBot 插件，只是一个模块）。

工业风配色：带细网格的暖浅灰纸面、黑色文字、标志性黄色作唯一强调色、直角、细线、警示斜纹，
数字和标签用窄体拉丁字体。中日韩文字仍用 WenQuanYi Zen Hei（唯一装了的中文字体）。

用法：把 `ef_theme.css()` 放在页面 <style> 最前面，后面接页面自己的规则，使用下面的
--ef-* 变量和 .ef-* 组件。
"""

from __future__ import annotations

from pathlib import Path

FONT_DIR = Path(__file__).with_name("fonts")


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


PAGE_SCALE = 1.25  # 通过 render_page 渲染的页面的设备缩放比例
PAGE_QUALITY = 65
WEBP_MAX_SIDE = 16383  # 格式上限


def to_webp(data: bytes, quality: int = 55, scale: float = 1, *, tall_height: int | None = None, tall_scale: float = 1) -> bytes:
    """把渲染出的 PNG 重新编码为 WebP：同样清晰度下大小约为 JPEG 的 40%。

    晚上这台机器往 QQ 上传会掉到约 15 KB/s，每 100 KB 都要多花几秒。
    比 tall_height 像素还高的页面再乘以 tall_scale：很长的页面（多页抽卡记录）多缩一点，少等一会。
    超出格式上限的长页面会缩小到能放下。
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        image = image.convert("RGB")
        if tall_height is not None and image.height > tall_height:
            scale *= tall_scale
        scale = min(scale, WEBP_MAX_SIDE / max(image.size))
        if scale < 1:
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        out = BytesIO()
        image.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue()


async def render_page(html: str, width: int, *, height: int = 800, scale: float = PAGE_SCALE, quality: int = PAGE_QUALITY, template_path: str | None = None) -> bytes:
    """用 htmlrender 截图页面，返回 WebP。

    以一张干员卡实测：1x 的 JPEG q85 为 380 KB；现在这样（1.25x、q65）约 210 KB，而且更清晰。
    """
    import asyncio

    from nonebot_plugin_htmlrender import html_to_pic

    extra = {"template_path": template_path} if template_path else {}
    image = await html_to_pic(html, type="png", device_scale_factor=scale, viewport={"width": width, "height": height}, **extra)
    return await asyncio.to_thread(to_webp, image, quality)


def fonts_css() -> str:
    """只有 @font-face 规则（指向本目录的绝对 file:// URL），给静态模板用。"""
    return _FONTS


def css() -> str:
    """字体 + 变量 + 共用组件；放在各页面自己的 CSS 前面。"""
    return _FONTS + _BASE


def head(kicker: str, title: str, subtitle: str = "", code: str = "") -> str:
    """深色标题板。`title` 可以用 <em> 标出黄色部分；其他参数都是纯文本。"""
    code_html = f'<div class="code">{code}</div>' if code else ""
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    return f'<div class="ef-head">{code_html}<div class="k">{kicker}</div><h1>{title}</h1>{sub}</div>'


TAIL_RATIO = 1.75  # 页面高度超过宽度的这么多倍时，在 QQ 看图器里会占满手机屏幕
TAIL_NOTES = (
    "指令必须以 <b>/</b> 开头，全部指令发 <b>/skl帮助</b> 查看",
    "数据随游戏版本自动更新，仅供参考，请以游戏内为准",
    "手机上点开长图时，这一块会被「查看原图」等按钮挡住，所以不放重要内容",
)


def foot(text: str, mark: str = "PRIESTESS SYSTEM", notes: tuple[str, ...] | list[str] | None = None, tail: bool = True, title: str = "读图说明") -> str:
    """页脚行。长页面会在它上方加一块说明板（`notes`，可以用 HTML）。

    QQ 手机端看图时，占满屏幕的图片底部会被「查看原图」/保存/转发按钮盖住。说明板占住这个位置，
    让最后几行内容不被挡；它在页面里才打开，因为那里才知道最终高度。
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
