"""文字版帮助页：在图片不是最好的第一回复时，/skl帮助 用它来回答。

图片包含全部内容，但要几秒才能发到会话里。在能给消息挂按钮的机器人上（官方机器人），
/skl帮助 先说明自动运行的功能，并把页面的各分类做成按钮；点开分类就是该分类指令的文字说明，
图片本身也是一个按钮。所有内容都从本部署对应的 help.html 读取（render.page）：
指令只在一个地方写。

这里不导入 NoneBot：纯文本处理，测试会单独加载它。
"""

from __future__ import annotations

import html as markup
import re
from dataclasses import dataclass, field

MENU_COMMAND = "/帮助"
PICTURE = "完整帮助图"  # 按钮的文字；也是让指令发送图片的参数
PICTURE_WORDS = {PICTURE, "帮助图", "完整帮助", "图片", "图", "全部", "完整"}
NOTES = "使用须知"  # 非指令内容的按钮：怎么叫机器人、要注意什么
NOTES_WORDS = {NOTES, "须知", "安全提醒", "注意事项", "说明"}


@dataclass
class Section:
    title: str
    about: str = ""  # 标题旁的小字
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # 指令、作用、备注


@dataclass
class Guide:
    heading: str = ""
    tasks_title: str = ""
    tasks: list[tuple[str, list[str]]] = field(default_factory=list)  # 一栏的标题，以及其下自动运行的任务
    notes: list[tuple[str, list[str]]] = field(default_factory=list)  # 标题和它下面的各行
    alias: str = ""
    sections: list[Section] = field(default_factory=list)


def words(fragment: str) -> str:
    """页面片段去掉标记后的文字。"""
    text = re.sub(r"</span>\s*<span class=\"time\">", "、", fragment)  # 并排的两个时间
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = markup.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(line for line in (re.sub(r"\s+", " ", line).strip() for line in text.split("\n")) if line)


def parse(page: str) -> Guide:
    """把页面（render.page 给出的 help.html）拆解开。"""
    guide = Guide()
    body = page[page.find("<body") :]
    title = re.search(r"<h1>(.*?)</h1>", body, flags=re.S)
    guide.heading = words(title.group(1)) if title else "指令帮助"
    for block in re.finditer(r'<div class="(ef-sec[^"]*|alias)">(.*?)(?=<div class="(?:ef-sec|alias)|<div class="ef-foot|</body>)', body, flags=re.S):
        kinds, inner = block.group(1).split(), block.group(2)
        if "alias" in kinds:
            guide.alias = words(re.sub(r'<div class="legend">.*?</div>', "", inner, flags=re.S))  # 表格各列的说明只留在图片里：文字版没有表格
            continue
        h3 = re.search(r"<h3>(.*?)</h3>", inner, flags=re.S)
        if h3 is None:
            continue
        small = re.search(r"<small[^>]*>(.*?)</small>", h3.group(1), flags=re.S)
        heading = words(re.sub(r"<small.*?</small>", "", h3.group(1), flags=re.S))
        rest = inner[h3.end() :]
        if "sec" in kinds:
            section = Section(heading, words(small.group(1)) if small else "")
            for row in re.findall(r"<tr>(.*?)</tr>", rest, flags=re.S):
                cells = dict(re.findall(r'<td class="(cmd|fn|note)">(.*?)</td>', row, flags=re.S))
                if cells.get("cmd"):
                    section.rows.append((words(cells["cmd"]), words(cells.get("fn", "")), words(cells.get("note", ""))))
            if section.rows:
                guide.sections.append(section)
        elif "auto" in kinds:
            guide.tasks_title = heading
            for kind, marked, bare in re.findall(r'<div class="(colh|dim|gap)">(.*?)</div>|<div>(.*?)</div>', rest, flags=re.S):
                if kind == "colh":
                    guide.tasks.append((words(marked), []))
                elif kind != "dim" and guide.tasks:  # 任务下方的小字只留在图片里
                    guide.tasks[-1][1].append(words(marked or bare))
        else:
            lines = [words(marked or bare) for _, marked, bare in re.findall(r'<div class="(dim)">(.*?)</div>|<div>(.*?)</div>', rest, flags=re.S)]
            guide.notes.append((heading, [line for line in lines if line]))
    return guide


def overview(guide: Guide) -> str:
    """第一条回复：机器人自动做的事，以及提示点按钮查看各分类。"""
    out = [f"**{guide.heading}**", f"点下面的分类查看指令；「{PICTURE}」是全部内容的一张图。"]
    if guide.tasks:
        out += ["", f"**{guide.tasks_title or '自动任务'}**"]
        for column, tasks in guide.tasks:
            out.append(column)
            out += [f"- {task}" for task in tasks]
    return "\n".join(out)


def section_words(section: Section) -> str:
    out = [f"**{section.title}**" + (f"（{section.about}）" if section.about else "")]
    for command, does, note in section.rows:
        out.append(f"- **{command}** {does}" + (f"。{note}" if note else ""))
    return "\n".join(out)


def notes_words(guide: Guide) -> str:
    out: list[str] = []
    for heading, lines in guide.notes:
        out += ["", f"**{heading}**"] + [f"- {line}" for line in lines]
    if guide.alias:
        out += ["", guide.alias]
    return "\n".join(out).strip()


def navigation(guide: Guide, shown: str) -> str:
    """显示不了按钮时，分类文字的最后一行：代替「返回分类」「完整帮助图」两个按钮。"""
    others = [section.title for section in guide.sections if section.title != shown]
    if (guide.notes or guide.alias) and shown != NOTES:
        others.append(NOTES)
    return f"其他分类：{'、'.join(others)}（发 {MENU_COMMAND} 分类名）；完整帮助图发 {MENU_COMMAND}"


def plain(text: str) -> str:
    """同样的文字，给不显示 markdown 的会话用。"""
    return text.replace("**", "")


def find(guide: Guide, asked: str) -> Section | None:
    """群友说的分类：按完整标题、标题开头或标题中的一部分匹配。"""
    asked = asked.strip()
    if not asked:
        return None
    for fits in (lambda title: title == asked, lambda title: title.startswith(asked), lambda title: asked in title):
        for section in guide.sections:
            if fits(section.title):
                return section
    return None


def menu(guide: Guide) -> list[tuple[str, str]]:
    """总览下面的按钮：每个分类一个，加上非指令内容和图片。"""
    buttons = [(section.title, f"{MENU_COMMAND} {section.title}") for section in guide.sections]
    if guide.notes or guide.alias:
        buttons.append((NOTES, f"{MENU_COMMAND} {NOTES}"))
    return [*buttons, (PICTURE, f"{MENU_COMMAND} {PICTURE}")]


BACK = [("返回分类", MENU_COMMAND), (PICTURE, f"{MENU_COMMAND} {PICTURE}")]
PLACEHOLDERS = ("名", "序号", "ID")  # 页面里表示“自己填”的词，如 /干员名、/战绩详情 ID
PAUSED = ("暂停", "已暂停")
HELP_COMMANDS = {MENU_COMMAND, "/skl帮助", "/help"}
BARE_ANSWERS = {"/zmd导入小黑盒", "/mrfz导入小黑盒"}  # 要带参数，但不带参数发送时会回复用法：按钮发不带参数的指令


def _bare(command: str) -> str:
    """BARE_ANSWERS 里的指令去掉要自己填的参数，其余原样。"""
    head = command.split(" ", 1)[0]
    return head if head in BARE_ANSWERS else command


def tappable(command: str, does: str) -> bool:
    """能做成按钮的指令：以 / 开头、不带参数、不是要自己填的占位写法，也没有暂停。

    带参数的指令都有不带参数的入口可以点进去（/skl角色 里切换角色，/战争回响、/影拓丰碑 里看敌人），
    所以分类下面只放这些入口，行数不多。
    """
    command = _bare(command)
    return (command.startswith("/") and " " not in command and not any(word in command for word in PLACEHOLDERS)
            and not does.startswith(PAUSED) and command not in HELP_COMMANDS)


def words_by_page(text: str, pages: list) -> list[str]:
    """把分类的文字按按钮消息拆开：每条消息只说明它自己的按钮，不会把整段再发一遍。

    pages：quick_buttons.pages_of 排好的各条消息的按钮。不能点的指令（带参数的）跟着它前面那个能点的。
    """
    head, *lines = text.split("\n")
    if len(pages) < 2:
        return [text]
    on_page = {command: number for number, page in enumerate(pages) for row in page for _, command in row}
    parts: list[list[str]] = [[] for _ in pages]
    number = 0
    for line in lines:
        command = re.match(r"- \*\*(.+?)\*\*", line)
        if command and _bare(command.group(1)) in on_page:
            number = on_page[_bare(command.group(1))]
        parts[number].append(line)
    return ["\n".join([f"{head}（{index + 1}/{len(pages)}）", *part]) for index, part in enumerate(parts)]


def section_buttons(section: Section) -> list[tuple[str, str]]:
    """分类下面的按钮：该分类里能直接点的指令，再加返回和图片。"""
    return [(_bare(command), _bare(command)) for command, does, _ in section.rows if tappable(command, does)] + BACK


def _ended(guide: Guide, text: str, shown: str, buttons: bool) -> str:
    return text if buttons else f"{text}\n{navigation(guide, shown)}"


def answer(guide: Guide, asked: str, buttons: bool) -> tuple[str, list[tuple[str, str]]] | None:
    """对后面跟着 `asked` 的 /skl帮助 该说什么，以及下面挂的按钮；该发图片时返回 None。

    指名要图片时发图片；没有按钮的机器人收到不带参数的 /skl帮助 时也照旧发图片。
    分类和使用须知在任何机器人上都是文字；没有按钮时末尾加一行 navigation。
    """
    asked = asked.strip()
    if asked in PICTURE_WORDS:
        return None
    if asked in NOTES_WORDS and (guide.notes or guide.alias):
        return _ended(guide, notes_words(guide), NOTES, buttons), BACK
    section = find(guide, asked)
    if section is not None:
        return _ended(guide, section_words(section), section.title, buttons), section_buttons(section)
    if not buttons or not guide.sections:
        return None
    return overview(guide), menu(guide)
