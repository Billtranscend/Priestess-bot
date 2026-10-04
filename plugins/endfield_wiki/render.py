"""HTML cards for Endfield operators and weapons (rendered by htmlrender), in the shared ef_theme look."""

from __future__ import annotations

from html import escape

from plugins import ef_theme

WIDTH = 1400

CSS = """
body { width: %dpx; padding: 26px; }
.wrap { display: flex; gap: 16px; align-items: flex-start; }
.side { width: 420px; flex: none; }
.main { flex: 1; min-width: 0; }
.mh { display: flex; align-items: baseline; gap: 12px; margin-top: 16px; padding: 4px 0 8px; border-bottom: 4px solid var(--ef-ink); }
.mh b { font-size: 30px; font-weight: 900; }
.mh b:before { content: ""; display: inline-block; width: 8px; height: 26px; margin-right: 12px; vertical-align: -3px; background: var(--ef-yellow); box-shadow: 0 0 0 1px var(--ef-ink); }
.mh span { margin-left: auto; font-family: var(--ef-num); font-weight: 600; font-size: 15px; letter-spacing: 3px; color: var(--ef-sub); text-transform: uppercase; }
.ef-head .code b.stars { color: var(--ef-yellow); font-family: var(--ef-cjk); font-size: 24px; letter-spacing: 3px; margin-top: 4px; }
.ef-head .eng { font-family: var(--ef-num); font-weight: 500; letter-spacing: 1px; font-size: 20px; color: #b9b9b2; }

/* spec tiles */
.tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--ef-line-2); border-top: 1px solid var(--ef-line-2); }
.tile { background: var(--ef-panel); padding: 9px 14px 10px; min-width: 0; }
.tile.wide { grid-column: span 2; }
.tile small { display: block; font-size: 13px; color: var(--ef-sub); }
.tile b { display: block; font-size: 21px; margin-top: 2px; }
.tile b.n { font-family: var(--ef-num); font-weight: 700; font-size: 28px; line-height: 1.1; letter-spacing: .5px; }
.tile.hl b { display: inline-block; background: var(--ef-yellow); padding: 0 6px; margin-left: -6px; }
.note { padding: 9px 14px; font-size: 15px; font-weight: 700; background: var(--ef-yellow); border-top: 1px solid var(--ef-ink); }
.art { position: relative; display: flex; justify-content: center; align-items: center; min-height: 200px; margin: 14px;
  background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
.art:before, .art:after { content: ""; position: absolute; width: 14px; height: 14px; border: 0 solid var(--ef-ink); }
.art:before { left: -1px; top: -1px; border-left-width: 3px; border-top-width: 3px; }
.art:after { right: -1px; bottom: -1px; border-right-width: 3px; border-bottom-width: 3px; }
.art img { display: block; max-width: 360px; max-height: 360px; }
.deco { padding: 0 16px 14px; font-size: 15px; line-height: 1.75; color: var(--ef-sub); }

/* skill / talent panels */
.ef-sec > h3 .ef-tag { font-weight: 400; }
.body { padding: 12px 16px 14px; }
.desc { font-size: 15px; line-height: 1.75; color: #2a2a27; }
.up { font-family: var(--ef-num); font-weight: 700; font-size: 17px; color: var(--ef-ink); }
.down { font-family: var(--ef-num); font-weight: 700; font-size: 17px; color: var(--ef-red); }
.key { font-weight: 700; color: var(--ef-ink); box-shadow: inset 0 -7px 0 var(--ef-yellow); }
.term { color: var(--ef-ink); text-decoration: underline dotted var(--ef-ink); text-underline-offset: 3px; }
.info { color: var(--ef-faint); }
table { width: 100%%; border-collapse: collapse; }
td, th { border-top: 1px solid var(--ef-line-2); padding: 6px 16px; font-size: 15px; vertical-align: top; text-align: left; }
tr:first-child td, tr:first-child th { border-top: 0; }
th { width: 74px; padding-right: 0; line-height: 1.75; color: var(--ef-sub); font-weight: 700; white-space: nowrap; }
th.lv { font-family: var(--ef-num); font-weight: 600; font-size: 16px; letter-spacing: 1px; color: var(--ef-ink); }
th.pt { width: 86px; }
th.pt span { display: inline-block; min-width: 22px; margin-left: 4px; text-align: center; font-family: var(--ef-num); font-weight: 700; font-size: 16px;
  background: var(--ef-ink); color: var(--ef-yellow); }
td b { color: var(--ef-ink); }
table.lvs tr:last-child th, table.lvs tr:last-child td { background: var(--ef-panel-2); }
""" % WIDTH


def _stars(rarity: int) -> str:
    return f'RARITY<b class="stars">{"★" * rarity}</b>'


def _img(src: str) -> str:
    return f'<div class="art"><img src="{escape(src)}"></div>' if src else ""


def _sec(title: str, body: str, note: str = "") -> str:
    small = f'<small class="cjk">{note}</small>' if note else ""
    return f'<div class="ef-sec"><h3>{title}{small}</h3>{body}</div>'


def _page(head: str, body: str, foot: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{ef_theme.css()}{CSS}</style></head>'
        f'<body class="ef">{head}{body}{foot}</body></html>'
    )


def _levels(skill: dict) -> str:
    return '<table class="lvs">' + "".join(
        f'<tr><th class="lv">Lv{i}</th><td class="desc">{lv}</td></tr>' for i, lv in enumerate(skill["levels"], 1)
    ) + "</table>"


def weapon_html(weapon: dict, icon_src: str, version: str, note: str = "") -> str:
    stat_skills, passive = weapon["skills"][:-1], weapon["skills"][-1:]
    if weapon["rarity"] < 5 or len(weapon["skills"]) < 3:
        stat_skills, passive = weapon["skills"], []

    stat_boxes = "".join(_sec(escape(s["name"]), _levels(s)) for s in stat_skills)
    passive_box = "".join(_sec(f'{escape(s["name"])}<span class="ef-tag y">被动</span>', _levels(s)) for s in passive)
    owner = escape(weapon["owner"]) if weapon["owner"] else "—"
    head = ef_theme.head(
        "Arknights: Endfield · Weapon",
        escape(weapon["name"]),
        f'<span class="eng">{escape(weapon["eng"])}</span>',
        _stars(weapon["rarity"]),
    )
    body = f"""
<div class="wrap">
  <div class="side">
    <div class="ef-sec">
      <h3>武器信息</h3>
      <div class="tiles">
        <div class="tile"><small>类型</small><b>{escape(weapon["type"])}</b></div>
        <div class="tile{" hl" if weapon["owner"] else ""}"><small>专属干员</small><b>{owner}</b></div>
        <div class="tile"><small>满级攻击（Lv{weapon["max_lv"]}）</small><b class="n">{weapon["max_atk"]}</b></div>
        <div class="tile"><small>稀有度</small><b><span class="ef-num" style="font-weight:700;font-size:28px">{weapon["rarity"]}</span> 星</b></div>
      </div>
      {f'<div class="note">{escape(note)}</div>' if note else ""}
      {_img(icon_src)}
      <div class="deco"{' style="padding-top:14px"' if not icon_src else ""}>{escape(weapon["deco"])}</div>
    </div>
  </div>
  <div class="main">
    <div class="mh"><b>武器数据详表</b><span>Weapon Data</span></div>
    <div style="display:flex;gap:16px;align-items:flex-start">
      <div style="flex:1;min-width:0">{stat_boxes}</div>
      <div style="flex:1.6;min-width:0">{passive_box}</div>
    </div>
  </div>
</div>"""
    foot = ef_theme.foot(f'数据来源 AKEData · 武器/{escape(weapon["name"])}<br>数据版本 {escape(version)}')
    return _page(head, body, foot)


def operator_html(op: dict, icon_src: str, version: str, signature_name: str) -> str:
    stats = "".join(f'<div class="tile"><small>{escape(k)}</small><b class="n">{v}</b></div>' for k, v in op["stats"])
    skills = "".join(
        _sec(f'{escape(s["name"])}<span class="ef-tag d">{escape(s["type"])}</span>', f'<div class="body desc">{s["desc"]}</div>')
        for s in op["skills"]
    )
    talents = "".join(
        _sec(f'{escape(t["name"])}<span class="ef-tag">天赋</span>', f'<div class="body desc">{t["desc"]}</div>')
        for t in op["talents"]
    )
    potentials = "".join(
        f'<tr><th class="pt">潜能<span>{p["level"]}</span></th><td class="desc"><b>{escape(p["name"])}</b>　{p["desc"]}</td></tr>'
        for p in op["potentials"]
    )
    head = ef_theme.head(
        "Arknights: Endfield · Operator",
        escape(op["name"]),
        f'<span class="eng">{escape(op["eng"])}</span>',
        _stars(op["rarity"]),
    )
    body = f"""
<div class="wrap">
  <div class="side">
    <div class="ef-sec">
      <h3>干员信息</h3>
      <div class="tiles">
        <div class="tile hl"><small>属性</small><b>{escape(op["element"])}</b></div>
        <div class="tile"><small>职业</small><b>{escape(op["profession"])}</b></div>
        <div class="tile"><small>武器类型</small><b>{escape(op["weapon_type"])}</b></div>
        <div class="tile"><small>主 / 副能力</small><b>{escape(op["main_attr"])} / {escape(op["sub_attr"])}</b></div>
        <div class="tile wide"><small>专武</small><b>{escape(signature_name) or "无"}</b></div>
      </div>
      {_img(icon_src)}
    </div>
    <div class="ef-sec">
      <h3>Lv90 基础属性</h3>
      <div class="tiles">{stats}</div>
    </div>
  </div>
  <div class="main">
    <div class="mh"><b>干员资料</b><span>Operator Profile</span></div>
    {skills}
    {talents}
    {_sec("潜能", f'<table style="margin:6px 0">{potentials}</table>') if potentials else ""}
  </div>
</div>"""
    foot = ef_theme.foot(f'数据来源 AKEData · 干员/{escape(op["name"])}（技能为满级数值）<br>数据版本 {escape(version)}')
    return _page(head, body, foot)
