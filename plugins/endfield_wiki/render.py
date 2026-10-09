"""HTML cards for Endfield operators, weapons and equipment sets (rendered by htmlrender), in the shared ef_theme look."""

from __future__ import annotations

from html import escape

from plugins import ef_theme

from .data import SKILL_LEVELS

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
.form { margin-top: 12px; padding: 8px 0 0 12px; border-left: 3px solid var(--ef-yellow); border-top: 1px solid var(--ef-line-2); }
.desc > .form:first-child { margin-top: 0; }
.form-h { margin-bottom: 2px; }
.form-h b { display: inline-block; padding: 0 8px; margin-right: 10px; background: var(--ef-ink); color: var(--ef-yellow); font-size: 14px; line-height: 1.7; }
.form-h .when { font-size: 13px; color: var(--ef-sub); }
table.vals { border-top: 1px solid var(--ef-line); table-layout: fixed; }
table.vals th, table.vals td { padding: 5px 8px; font-size: 14px; line-height: 1.5; border-top: 1px solid var(--ef-line-2); }
table.vals th { width: 36%%; padding-left: 16px; white-space: normal; font-weight: 400; color: #2a2a27; background: var(--ef-panel-2); }
table.vals td { width: 16%%; text-align: center; font-family: var(--ef-num); font-weight: 600; font-size: 16px; color: var(--ef-ink); border-left: 1px solid var(--ef-line-2); }
table.vals td.same { color: var(--ef-faint); font-weight: 400; }
table.vals td:last-child { background: var(--ef-yellow); color: var(--ef-ink); font-weight: 700; }
table.vals thead th { width: 16%%; background: var(--ef-ink); color: #fff; font-family: var(--ef-num); font-weight: 600; font-size: 15px; letter-spacing: 1px; text-align: center; border: 0; }
table.vals thead th:first-child { width: 36%%; text-align: left; font-family: inherit; font-size: 13px; letter-spacing: 0; color: var(--ef-yellow); }
table.vals thead th:last-child { color: var(--ef-yellow); }
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

/* equipment sets and build statistics */
.use { padding: 8px 16px 4px; }
.use .row { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 16px; }
.use .row b { flex: none; width: 116px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.use .bar { flex: 1; height: 16px; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
.use .bar u { display: block; height: 100%%; background: var(--ef-yellow); border-right: 1px solid var(--ef-ink); }
.use .row span { flex: none; width: 92px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 17px; }
.use .row span i { font-style: normal; font-family: var(--ef-cjk); font-weight: 400; font-size: 13px; color: var(--ef-sub); margin-left: 3px; }
.typ { margin: 0 16px; padding: 8px 0 4px; border-top: 2px solid var(--ef-line); font-size: 15px; }
h3 + .typ { border-top: 0; }
.typ .use { padding: 0; }
.typ .use .row b { font-size: 17px; }
.typ .pc { display: flex; gap: 10px; align-items: flex-start; padding: 7px 0; border-top: 1px dashed var(--ef-line-2); }
.typ .pc:first-of-type { border-top: 0; }
.typ .pc img, .typ .pc .ph { flex: none; width: 56px; height: 56px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); border-bottom: 3px solid var(--ef-yellow); }
.typ .pc > div { flex: 1; min-width: 0; }
.typ .pc b { font-size: 17px; }
.typ .pc small { margin-left: 8px; font-size: 13px; color: var(--ef-sub); }
.typ .rf { display: flex; flex-wrap: wrap; gap: 4px 6px; margin-top: 5px; }
.typ .rf span { font-size: 13px; line-height: 20px; padding: 0 6px; border: 1px solid var(--ef-line); background: var(--ef-panel); white-space: nowrap; }
.typ .rf span em { font-style: normal; font-family: var(--ef-num); font-weight: 700; font-size: 16px; margin-left: 5px; }
.typ .rf span.hot { background: var(--ef-yellow); border-color: var(--ef-ink); font-weight: 700; }
.hint { padding: 8px 16px 12px; font-size: 13px; line-height: 1.6; color: var(--ef-sub); }
table.eq td { padding: 8px 12px; vertical-align: middle; }
table.eq td.ic { width: 64px; padding-right: 0; }
table.eq td.ic img { display: block; width: 60px; height: 60px; object-fit: contain; background: var(--ef-panel-2); border: 1px solid var(--ef-line); border-bottom: 3px solid var(--ef-yellow); }
table.eq td.nm { width: 250px; }
table.eq td.nm b { display: block; font-size: 18px; }
table.eq td.nm small { font-size: 13px; color: var(--ef-sub); }
table.eq tr.hit td { background: rgba(255,225,0,.22); }
table.eq .at { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 15px; }
table.eq .at span { white-space: nowrap; }
table.eq .at em { font-style: normal; font-family: var(--ef-num); font-weight: 700; font-size: 17px; margin-left: 5px; }
table.eq .at i { font-style: normal; color: var(--ef-faint); margin: 0 3px; }
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


def _usage_rows(rows: list[tuple[str, int, int]], unit: str) -> str:
    """Bars for (label, count, out of)."""
    return "".join(
        f'<div class="row"><b>{escape(label)}</b><div class="bar"><u style="width:{round(count / max(total, 1) * 100)}%"></u></div>'
        f"<span>{round(count / max(total, 1) * 100)}%<i>{count} {unit}</i></span></div>"
        for label, count, total in rows
    )


REFINE_MARGIN = 0.1  # attributes within this of a piece's most refined one are marked as the ones members go for


def _builds_box(builds: dict | None, min_sample: int, pieces: dict[str, dict]) -> str:
    """What bound members run on this operator; there is no official recommendation in the game data.

    One block per set, most used first: its share, then the piece members most often wear in each slot. A set
    after the first that fewer than `min_sample` members run keeps its share only.
    `pieces` maps a piece name to {"src": icon, "attrs": [attribute names]} for the pieces listed.
    """
    if not builds or builds["n"] < min_sample:
        body = f'<div class="hint">已成套的群友不足 {min_sample} 人，暂无统计。</div>'
        return _sec("群友配装", body)
    blocks, refined = "", False
    for rank, build in enumerate(builds["builds"]):
        typical = ""
        for piece in build["pieces"] if rank == 0 or build["count"] >= min_sample else ():
            info = pieces.get(piece["name"], {})
            icon = f'<img src="{escape(info["src"])}">' if info.get("src") else '<div class="ph"></div>'
            chips = ""
            if piece.get("refine") and info.get("attrs"):  # loose pieces have two attributes, set pieces three
                refined = True
                levels = piece["refine"][: len(info["attrs"])]
                chips = '<div class="rf">' + "".join(
                    f'<span{" class=hot" if level >= 1 and level >= max(levels) - REFINE_MARGIN else ""}>{escape(attr)}<em>{level:.1f}</em></span>'
                    for attr, level in zip(info["attrs"], levels)
                ) + "</div>"
            typical += f'<div class="pc">{icon}<div><b>{escape(piece["name"])}</b><small>{escape(piece["slot"])}</small>{chips}</div></div>'
        share = _usage_rows([(build["set"], build["count"], builds["n"])], "人")
        blocks += f'<div class="typ"><div class="use">{share}</div>{typical}</div>'
    refine_hint = "词条后的数字是穿这件装备的群友对该词条的平均精锻次数（满 3 次），黄底是这件装备上群友精锻最多的词条。" if refined else ""
    body = (
        f"{blocks}"
        f'<div class="hint">每个套装下面是穿这套的群友在各部位最常用的装备。{refine_hint}统计自已绑定群友 Lv70 以上、已凑齐 3 件套的该干员，每天更新；不是官方推荐。发送 /套装名 查看套装详情。</div>'
    )
    return _sec("群友配装", body, f'{builds["n"]} 人已成套')


def equip_set_html(equip_set: dict, icons: dict[str, str], version: str, usage: dict | None, min_sample: int, asked: str = "") -> str:
    parts: dict[str, list[dict]] = {}
    for piece in equip_set["pieces"]:
        parts.setdefault(piece["part"], []).append(piece)
    tables = ""
    for part, pieces in parts.items():
        rows = ""
        for piece in pieces:
            attrs = "".join(
                f'<span>{escape(a["name"])}<em>{a["base"]}</em>' + (f'<i>→</i><em>{a["max"]}</em>' if a["max"] else "") + "</span>"
                for a in piece["attrs"]
            )
            icon = f'<img src="{escape(icons[piece["id"]])}">' if icons.get(piece["id"]) else ""
            rows += (
                f'<tr{" class=hit" if piece["name"] == asked else ""}><td class="ic">{icon}</td>'
                f'<td class="nm"><b>{escape(piece["name"])}</b><small>{"★" * piece["rarity"]} · Lv{piece["level"]} · 防御力 {piece["defense"]}</small></td>'
                f'<td><div class="at">{attrs}</div></td></tr>'
            )
        tables += _sec(escape(part), f'<table class="eq">{rows}</table>', f"{len(pieces)} 件可选")
    if usage and usage["n"] >= min_sample:
        use_body = (
            f'<div class="use">{_usage_rows(usage["operators"], "人")}</div>'
            '<div class="hint">百分比为该干员已成套的群友中使用本套装的比例；统计自已绑定群友 Lv70 以上的干员，每天更新。</div>'
        )
        use_box = _sec("群友给谁穿", use_body, f'{usage["n"]} 套')
    else:
        use_box = _sec("群友给谁穿", f'<div class="hint">穿这套的群友不足 {min_sample} 人，暂无统计。</div>')
    head = ef_theme.head(
        "Arknights: Endfield · Equipment Set",
        escape(equip_set["name"]),
        f'<span class="eng">装备套装 · {escape(equip_set["domain"])}</span>',
        _stars(equip_set["rarity"]),
    )
    body = f"""
<div class="wrap">
  <div class="side">
    <div class="ef-sec">
      <h3>套装信息</h3>
      <div class="tiles">
        <div class="tile"><small>产地</small><b>{escape(equip_set["domain"]) or "—"}</b></div>
        <div class="tile"><small>穿戴等级</small><b class="n">Lv{equip_set["level"]}</b></div>
        <div class="tile"><small>生效件数</small><b><span class="ef-num" style="font-weight:700;font-size:28px">{equip_set["count"]}</span> 件</b></div>
        <div class="tile"><small>可选装备</small><b><span class="ef-num" style="font-weight:700;font-size:28px">{len(equip_set["pieces"])}</span> 件</b></div>
      </div>
      {f'<div class="note">查询的装备：{escape(asked)}</div>' if asked else ""}
    </div>
    {_sec(f'套装效果<span class="ef-tag y">{equip_set["count"]} 件</span>', f'<div class="body desc">{equip_set["effect"]}</div>')}
    {use_box}
  </div>
  <div class="main">
    <div class="mh"><b>装备列表</b><span>Equipment</span></div>
    {tables}
    <div class="hint" style="padding-left:0">属性为 未精锻 → 精锻满级 的数值；每个干员可穿 1 护甲、1 护手、2 配件，任意 {equip_set["count"]} 件同套装即生效。</div>
  </div>
</div>"""
    foot = ef_theme.foot(f'数据来源 AKEData · 装备套装/{escape(equip_set["name"])}<br>数据版本 {escape(version)}')
    return _page(head, body, foot)


def _skill_table(rows: list[dict]) -> str:
    """The skill's named values at its last four levels; the column of the top level stands out."""
    if not rows:
        return ""
    head = "".join(f"<th>{escape(level)}</th>" for level in SKILL_LEVELS)
    body = "".join(
        f'<tr><th>{escape(row["label"])}</th>'
        + "".join(f'<td{" class=\"same\"" if i and value == row["values"][i - 1] else ""}>{escape(value)}</td>' for i, value in enumerate(row["values"]))
        + "</tr>"
        for row in rows
    )
    return f'<table class="vals"><thead><tr><th>技能等级</th>{head}</tr></thead><tbody>{body}</tbody></table>'


def _skill_form(form: dict) -> str:
    """What a skill does in one of the operator's two forms."""
    when = f'<span class="when">{form["when"]}</span>' if form.get("when") else ""
    return f'<div class="form"><div class="form-h"><b>{escape(form["name"])}</b>{when}</div>{form["desc"]}</div>'


def operator_html(
    op: dict, icon_src: str, version: str, signature_name: str, builds: dict | None = None, min_sample: int = 3, pieces: dict | None = None
) -> str:
    stats = "".join(f'<div class="tile"><small>{escape(k)}</small><b class="n">{v}</b></div>' for k, v in op["stats"])
    skills = "".join(
        _sec(
            f'{escape(s["name"])}<span class="ef-tag d">{escape(s["type"])}</span>',
            f'<div class="body desc">{s["desc"]}{"".join(_skill_form(form) for form in s.get("forms", ()))}</div>{_skill_table(s.get("table") or [])}',
        )
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
    {_builds_box(builds, min_sample, pieces or {})}
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
