"""Page looks for the calendar: each game's pictures follow that game's own interface.

Endfield builds on the shared ef_theme (light industrial panels, black and yellow) and adds the
ENDFIELD watermark, corner marks and English captions. Arknights gets a dark look after its menus:
near-black panels, white type, thin rules with corner marks, a blue accent, condensed capitals for
the English labels. Both build the same blocks (header, section card, tag, rows, the
opening-notice plate), so the calendar code only picks a theme.

An activity row can carry the activity's own banner between its name and its deadline, as the
official version calendars do, so the subject is recognisable at a glance. Endfield's banners are
the game's tab pictures (288 x 124, already fading out to the left); Arknights' are the announcement
banners (about 3:1), faded here.
"""

from __future__ import annotations

from html import escape

from plugins import ef_theme


CAPTIONS = {"正在开放": "OPEN NOW", "即将开启": "UPCOMING", "卡池": "HEADHUNTING", "同时进行中": "IN PROGRESS"}  # English captions of the section cards


class EndfieldTheme:
    body_class = "ef"
    css_rows = """
body { width: %dpx; padding: 26px; }
/* header watermark, corner marks and captions on top of the shared theme */
.ef-head .wm { position: absolute; right: 20px; bottom: 14px; font-family: var(--ef-num); font-weight: 700; font-size: 100px; line-height: .78; letter-spacing: 8px;
  color: rgba(255,255,255,.07); white-space: nowrap; }
.ef-head .k, .ef-head h1, .ef-head p, .ef-head .code { z-index: 1; }
.ef-head .k, .ef-head h1, .ef-head p { position: relative; }
.ef-sec { position: relative; }
.ef-sec:before { content: ""; position: absolute; left: -1px; top: -1px; width: 16px; height: 16px; border-left: 3px solid var(--ef-ink); border-top: 3px solid var(--ef-ink); }
.ef-sec:after { content: ""; position: absolute; right: -1px; bottom: -1px; width: 16px; height: 16px; border-right: 3px solid var(--ef-yellow-2); border-bottom: 3px solid var(--ef-yellow-2); }
.ef-sec > h3 i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 14px; letter-spacing: 4px; color: var(--ef-sub); }
.row { display: flex; align-items: center; gap: 16px; padding: 12px 16px; border-top: 1px solid var(--ef-line-2); }
.row:first-of-type { border-top: 0; }
.nm { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.nm b { font-size: 21px; font-weight: 900; }
.nm small { flex-basis: 100%%; font-size: 15px; color: var(--ef-sub); }
.nm .ef-tag { flex: none; }
.dl { flex: none; text-align: right; }
.dl b { display: block; font-size: 19px; font-weight: 700; }
.dl b.perm { font-size: 16px; color: var(--ef-sub); font-weight: 400; }
.dl span { display: inline-block; margin-top: 3px; font-size: 14px; color: var(--ef-sub); }
.dl span.hot { color: #fff; background: var(--ef-red); padding: 0 7px; font-weight: 700; }
/* the activity's banner between name and deadline; the row's left edge takes the activity's colour */
.row.pic { padding-top: 0; padding-bottom: 0; min-height: 100px; }
/* the tab picture is small (288 x 124): it is enlarged to reach towards the middle of the row, which crops a little off its top and bottom */
.row .art { flex: none; align-self: stretch; width: 420px; min-height: 100px; margin-left: -90px; background: right center / 100%% auto no-repeat; }
.row .nm { position: relative; z-index: 1; }
.row.tint { border-left: 6px solid var(--c); padding-left: 10px; }
/* pool banners are wide strips with the subject on the right: show that end, faded on the left */
.row .art.pool { width: 460px; background-size: auto 100%%; -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 34%%); }
/* opening notice */
.hero { position: relative; overflow: hidden; margin-top: 16px; background: var(--ef-panel); border: 2px solid var(--ef-ink); border-left: 12px solid var(--ef-yellow); padding: 18px 22px 20px; }
.hero .art { position: absolute; right: 16px; top: 8px; width: 270px; height: 116px; background: right center / auto 100%% no-repeat; }
.hero.pic:after { content: none; }
.hero.pic h2 { padding-right: 280px; }
.hero:after { content: "EVENT"; position: absolute; right: 12px; top: -28px; font-family: var(--ef-num); font-weight: 700; font-size: 124px; letter-spacing: 6px; color: rgba(0,0,0,.05); }
.hero .hk, .hero h2, .hero .end, .hero .from { position: relative; z-index: 1; }
.hero .hk { display: flex; align-items: center; gap: 10px; font-size: 15px; color: var(--ef-sub); }
.hero .hk i { font-style: normal; font-weight: 900; font-size: 15px; padding: 1px 10px; background: var(--ef-ink); color: var(--ef-yellow); letter-spacing: 2px; }
.hero h2 { margin-top: 8px; font-size: 46px; font-weight: 900; line-height: 1.2; }
.hero .end { margin-top: 14px; display: flex; align-items: stretch; border: 2px solid var(--ef-ink); }
.hero .end span { flex: none; display: flex; align-items: center; padding: 0 18px; background: var(--ef-ink); color: var(--ef-yellow); font-size: 20px; font-weight: 900; letter-spacing: 4px; }
.hero .end b { flex: 1; padding: 8px 18px; background: var(--ef-yellow); font-family: var(--ef-num), var(--ef-cjk); font-weight: 700; font-size: 38px; line-height: 1.2; letter-spacing: 1px; }
.hero .end em { flex: none; display: flex; align-items: center; padding: 0 18px; font-style: normal; font-size: 18px; font-weight: 700; background: #fff; border-left: 2px solid var(--ef-ink); }
.hero .from { margin-top: 10px; font-size: 16px; color: var(--ef-sub); }
"""

    def css(self, width: int) -> str:
        return ef_theme.css() + self.css_rows % width

    def head(self, kicker: str, title: str, subtitle: str, code: str) -> str:
        code_html = f'<div class="code">{code}</div>' if code else ""
        return f'<div class="ef-head"><span class="wm">ENDFIELD</span>{code_html}<div class="k">{kicker}</div><h1>{title}</h1><p>{subtitle}</p></div>'

    def foot(self, text: str) -> str:
        return ef_theme.foot(text)

    def card(self, title: str, body: str, note: str = "") -> str:
        caption = f"<i>{CAPTIONS[title]}</i>" if title in CAPTIONS else ""
        small = f'<small class="cjk">{note}</small>' if note else ""
        return f'<div class="ef-sec"><h3>{title}{caption}{small}</h3>{body}</div>'

    def tag(self, text: str, strong: bool = False) -> str:
        return f'<span class="ef-tag{" y" if strong else ""}">{escape(text)}</span>' if text else ""

    def empty(self, text: str) -> str:
        return f'<div class="ef-empty">{text}</div>'


class ArknightsTheme:
    body_class = "ak"
    css_all = """
:root {
  --ak-bg: #0e1012; --ak-panel: #181b1f; --ak-panel-2: #20242a; --ak-line: #3a4048; --ak-line-2: #2a2f36;
  --ak-ink: #f3f4f5; --ak-sub: #a2a8b0; --ak-faint: #6c727a;
  --ak-blue: #23ade5; --ak-orange: #ff9b2f; --ak-red: #e9473d;
  --ak-cjk: "WenQuanYi Zen Hei", sans-serif;
  --ak-num: "EF Num", "Liberation Sans Narrow", "WenQuanYi Zen Hei", sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body.ak { width: %dpx; padding: 26px; font-family: var(--ak-cjk); color: var(--ak-ink); background-color: var(--ak-bg);
  background-image: repeating-linear-gradient(-45deg, rgba(255,255,255,.028) 0 1px, transparent 1px 11px); }

/* header plate */
.ak-head { position: relative; overflow: hidden; padding: 22px 28px 24px 34px; background: linear-gradient(100deg, #000 0%%, #101316 55%%, #1b2026 100%%);
  border: 1px solid var(--ak-line); border-left: 0; }
.ak-head:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 8px; background: var(--ak-blue); }
.ak-head:after { content: "RHODES ISLAND"; position: absolute; right: 18px; bottom: -22px; font-family: var(--ak-num); font-weight: 700; font-size: 92px;
  letter-spacing: 6px; color: rgba(255,255,255,.05); white-space: nowrap; }
.ak-head .k { font-family: var(--ak-num); font-weight: 600; font-size: 15px; letter-spacing: 5px; color: var(--ak-blue); text-transform: uppercase; }
.ak-head h1 { position: relative; z-index: 1; margin-top: 6px; font-size: 40px; font-weight: 900; letter-spacing: 2px; }
.ak-head h1 em { font-style: normal; color: var(--ak-blue); }
.ak-head p { position: relative; z-index: 1; margin-top: 8px; font-size: 17px; color: var(--ak-sub); }
.ak-head .code { position: absolute; z-index: 1; right: 26px; top: 20px; text-align: right; font-family: var(--ak-num); font-weight: 600; font-size: 14px;
  letter-spacing: 3px; color: var(--ak-sub); }
.ak-head .code b { display: block; font-size: 34px; line-height: 1.1; color: #fff; letter-spacing: 1px; }

/* section card: thin frame with a white corner mark */
.ak-sec { position: relative; margin-top: 16px; background: var(--ak-panel); border: 1px solid var(--ak-line); }
.ak-sec:before { content: ""; position: absolute; left: -1px; top: -1px; width: 16px; height: 16px; border-left: 3px solid #fff; border-top: 3px solid #fff; }
.ak-sec:after { content: ""; position: absolute; right: -1px; bottom: -1px; width: 16px; height: 16px; border-right: 3px solid var(--ak-blue); border-bottom: 3px solid var(--ak-blue); }
.ak-sec > h3 { display: flex; align-items: baseline; gap: 12px; padding: 12px 18px 10px 20px; font-size: 21px; font-weight: 900; letter-spacing: 1px;
  background: var(--ak-panel-2); border-bottom: 1px solid var(--ak-line); }
.ak-sec > h3 i { font-style: normal; font-family: var(--ak-num); font-weight: 600; font-size: 14px; letter-spacing: 4px; color: var(--ak-blue); }
.ak-sec > h3 small { margin-left: auto; font-size: 14px; font-weight: 400; letter-spacing: 0; color: var(--ak-sub); }
.ak-tag { display: inline-block; font-size: 13px; line-height: 20px; padding: 0 8px; border: 1px solid var(--ak-sub); color: var(--ak-ink); white-space: nowrap; }
.ak-tag.y { background: var(--ak-blue); border-color: var(--ak-blue); color: #04121a; font-weight: 700; }
.ak-empty { padding: 14px 20px; color: var(--ak-faint); font-size: 16px; }

/* rows */
.row { display: flex; align-items: center; gap: 16px; padding: 12px 18px 12px 20px; border-top: 1px solid var(--ak-line-2); }
.row:first-of-type { border-top: 0; }
.nm { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.nm b { font-size: 21px; font-weight: 900; }
.nm small { flex-basis: 100%%; font-size: 15px; color: var(--ak-sub); }
.nm .ak-tag { flex: none; }
.dl { flex: none; text-align: right; }
.dl b { display: block; font-size: 19px; font-weight: 700; }
.dl b.perm { font-size: 16px; color: var(--ak-sub); font-weight: 400; }
.dl span { display: inline-block; margin-top: 3px; font-size: 14px; color: var(--ak-sub); }
.dl span.hot { color: #fff; background: var(--ak-red); padding: 0 7px; font-weight: 700; }
/* the activity's announcement banner between name and deadline, fading into the panel */
.row.pic { padding-top: 0; padding-bottom: 0; min-height: 100px; }
.row .art { flex: none; align-self: stretch; width: 372px; min-height: 100px; margin-left: -60px; background: center / cover no-repeat;
  -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 30%%, #000 93%%, transparent 100%%); }
.row .nm { position: relative; z-index: 1; }
.row.tint { border-left: 6px solid var(--c); padding-left: 14px; }
/* pool banners are tall posters: a band across the operators' faces */
.row .art.pool { background-position: center 30%%; }

/* opening notice */
.hero { position: relative; overflow: hidden; margin-top: 16px; padding: 20px 24px 22px 28px; background: #000; border: 1px solid var(--ak-line); border-left: 8px solid var(--ak-blue); }
.hero .art { position: absolute; right: 0; top: 0; width: 520px; height: 167px; background: center / cover no-repeat;
  -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 30%%), linear-gradient(180deg, #000 70%%, transparent 100%%); -webkit-mask-composite: source-in; }
.hero.pic:after { content: none; }
.hero.pic h2 { padding-right: 330px; }
.hero:after { content: "EVENT"; position: absolute; right: 14px; top: -26px; font-family: var(--ak-num); font-weight: 700; font-size: 120px; letter-spacing: 6px;
  color: rgba(255,255,255,.055); }
.hero .hk { position: relative; z-index: 1; display: flex; align-items: center; gap: 10px; font-size: 15px; color: var(--ak-sub); }
.hero .hk i { font-style: normal; font-weight: 900; font-size: 15px; padding: 1px 10px; background: #fff; color: #000; letter-spacing: 2px; }
.hero h2 { position: relative; z-index: 1; margin-top: 10px; font-size: 46px; font-weight: 900; line-height: 1.2; color: #fff; }
.hero .end { position: relative; z-index: 1; margin-top: 16px; display: flex; align-items: stretch; border: 2px solid #fff; }
.hero .end span { flex: none; display: flex; align-items: center; padding: 0 18px; background: #fff; color: #000; font-size: 20px; font-weight: 900; letter-spacing: 4px; }
.hero .end b { flex: 1; padding: 8px 18px; font-family: var(--ak-num), var(--ak-cjk); font-weight: 700; font-size: 38px; line-height: 1.2; letter-spacing: 1px; color: #fff; }
.hero .end em { flex: none; display: flex; align-items: center; padding: 0 18px; font-style: normal; font-size: 18px; font-weight: 700; background: var(--ak-orange); color: #000; }
.hero .from { position: relative; z-index: 1; margin-top: 10px; font-size: 16px; color: var(--ak-sub); }

.ak-foot { margin-top: 14px; display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; font-size: 13px; line-height: 1.7; color: var(--ak-faint); }
.ak-foot .mark { font-family: var(--ak-num); font-weight: 600; letter-spacing: 3px; color: var(--ak-sub); white-space: nowrap; }
.ak-foot .mark:before { content: ""; display: inline-block; width: 18px; height: 8px; margin-right: 8px; vertical-align: 1px;
  background: repeating-linear-gradient(-45deg, var(--ak-blue) 0 3px, transparent 3px 6px); }
"""

    def css(self, width: int) -> str:
        return ef_theme.fonts_css() + self.css_all % width

    def head(self, kicker: str, title: str, subtitle: str, code: str) -> str:
        code_html = f'<div class="code">{code}</div>' if code else ""
        return f'<div class="ak-head">{code_html}<div class="k">{kicker}</div><h1>{title}</h1><p>{subtitle}</p></div>'

    def foot(self, text: str) -> str:
        return f'<div class="ak-foot"><span>{text}</span><span class="mark">PRIESTESS SYSTEM</span></div>'

    def card(self, title: str, body: str, note: str = "") -> str:
        caption = f"<i>{CAPTIONS[title]}</i>" if title in CAPTIONS else ""
        small = f"<small>{note}</small>" if note else ""
        return f'<div class="ak-sec"><h3>{title}{caption}{small}</h3>{body}</div>'

    def tag(self, text: str, strong: bool = False) -> str:
        return f'<span class="ak-tag{" y" if strong else ""}">{escape(text)}</span>' if text else ""

    def empty(self, text: str) -> str:
        return f'<div class="ak-empty">{text}</div>'


ENDFIELD = EndfieldTheme()
ARKNIGHTS = ArknightsTheme()
