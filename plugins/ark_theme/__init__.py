"""Shared Arknights look for this project's own Arknights pictures (not a NoneBot plugin, just a module).

The counterpart of ef_theme for 明日方舟: the game's terminal style instead of Endfield's paper and
yellow. Dark graphite panels, white type, hairlines, Rhodes Island blue as the accent, the game's
rarity colours, condensed Latin labels. Layout parts mirror ef_theme (head plate, tiles, section
panels, footer) so a /mrfz picture sits next to a /zmd picture without looking foreign. Fonts and
the WebP renderer are ef_theme's.

Usage: put `ark_theme.css()` first in the page <style>, then the page's own rules using the --ak-*
variables and the .ak-* components below; render with `ef_theme.render_page`.
"""

from __future__ import annotations

from plugins import ef_theme

BLUE = "#1ca7e6"
RARITY_COLOR = {6: "#ff8a1f", 5: "#ffd21f", 4: "#c7a4ff", 3: "#3fb2ff", 2: "#dbe64a", 1: "#b5b5b5"}

_BASE = """
:root {
  --ak-bg: #19191b; --ak-panel: #242427; --ak-panel-2: #2d2d31; --ak-line: #3a3a3f; --ak-line-2: #4a4a50;
  --ak-ink: #eeeeee; --ak-sub: #a6a6ac; --ak-faint: #707077;
  --ak-blue: #1ca7e6; --ak-blue-2: #0b6f9e; --ak-yellow: #ffd21f; --ak-red: #e5533d; --ak-green: #56c271;
  --ak-cjk: "WenQuanYi Zen Hei", sans-serif;
  --ak-num: "EF Num", "Liberation Sans Narrow", "WenQuanYi Zen Hei", sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body.ak { font-family: var(--ak-cjk); color: var(--ak-ink); background-color: var(--ak-bg);
  background-image: repeating-linear-gradient(135deg, rgba(255,255,255,.018) 0 2px, transparent 2px 9px); }
.ak-num { font-family: var(--ak-num); font-variant-numeric: tabular-nums; letter-spacing: .3px; }

/* head plate: black terminal panel, blue spine, corner brackets, hairline footer */
.ak-head { position: relative; background: #101012; border: 1px solid var(--ak-line); padding: 20px 26px 26px 34px; overflow: hidden; }
.ak-head:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 8px; background: var(--ak-blue); }
.ak-head:after { content: ""; position: absolute; left: 34px; right: 26px; bottom: 12px; height: 1px;
  background: linear-gradient(90deg, var(--ak-blue) 0 120px, var(--ak-line-2) 120px); }
.ak-head .k { font-family: var(--ak-num); font-weight: 600; font-size: 14px; letter-spacing: 4px; color: var(--ak-blue); text-transform: uppercase; }
.ak-head h1 { font-size: 38px; font-weight: 900; margin-top: 6px; letter-spacing: 1px; color: #fff; }
.ak-head h1 em { font-style: normal; color: var(--ak-blue); }
.ak-head p { margin-top: 6px; font-size: 16px; color: var(--ak-sub); }
.ak-head .code { position: absolute; right: 26px; top: 18px; text-align: right; font-family: var(--ak-num); font-weight: 600;
  font-size: 13px; letter-spacing: 3px; color: var(--ak-faint); line-height: 1.5; }
.ak-head .code b { display: block; font-size: 30px; color: #fff; letter-spacing: 1px; }
.ak-corner { position: absolute; width: 14px; height: 14px; border-color: var(--ak-sub); border-style: solid; }
.ak-corner.tl { left: 16px; top: 8px; border-width: 1px 0 0 1px; } .ak-corner.br { right: 8px; bottom: 20px; border-width: 0 1px 1px 0; }

/* section panel */
.ak-sec { margin-top: 16px; background: var(--ak-panel); border: 1px solid var(--ak-line); }
.ak-sec > h3 { display: flex; align-items: center; gap: 10px; padding: 10px 16px; font-size: 20px; font-weight: 900; color: #fff;
  border-bottom: 1px solid var(--ak-line-2); background: linear-gradient(90deg, #2c2c30, var(--ak-panel) 60%); }
.ak-sec > h3:before { content: ""; width: 4px; height: 20px; background: var(--ak-blue); }
.ak-sec > h3 small { margin-left: auto; font-family: var(--ak-num); font-weight: 600; font-size: 13px; letter-spacing: 2px; color: var(--ak-faint); text-transform: uppercase; }
.ak-sec > h3 small.cjk { font-family: var(--ak-cjk); font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--ak-sub); }

/* small parts */
.ak-tag { display: inline-block; font-size: 12px; line-height: 18px; padding: 0 7px; border: 1px solid var(--ak-line-2); color: var(--ak-sub); background: var(--ak-panel-2); }
.ak-tag.b { border-color: var(--ak-blue); color: #fff; background: var(--ak-blue-2); }
.ak-empty { padding: 14px 16px; color: var(--ak-faint); font-size: 15px; }
.ak-foot { margin-top: 14px; display: flex; justify-content: space-between; align-items: center; gap: 16px; font-size: 13px; color: var(--ak-faint); line-height: 1.6; }
.ak-foot .mark { font-family: var(--ak-num); font-weight: 600; letter-spacing: 3px; color: var(--ak-sub); white-space: nowrap; }
.ak-foot .mark:before { content: ""; display: inline-block; width: 18px; height: 8px; margin-right: 8px; vertical-align: 1px;
  background: repeating-linear-gradient(-45deg, var(--ak-blue) 0 4px, transparent 4px 8px); border: 1px solid var(--ak-blue); }
"""


def css() -> str:
    """Fonts + variables + shared components; prepend to each page's own CSS."""
    return ef_theme.fonts_css() + _BASE


def head(kicker: str, title: str, subtitle: str = "", code: str = "") -> str:
    """Terminal head plate. `title` may contain <em> for the blue part; others are plain text."""
    code_html = f'<div class="code">{code}</div>' if code else ""
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    return (f'<div class="ak-head"><span class="ak-corner tl"></span><span class="ak-corner br"></span>{code_html}'
            f'<div class="k">{kicker}</div><h1>{title}</h1>{sub}</div>')


def foot(text: str, mark: str = "PRIESTESS SYSTEM") -> str:
    return f'<div class="ak-foot"><span>{text}</span><span class="mark">{mark}</span></div>'
