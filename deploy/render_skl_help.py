"""不重启机器人，立即为本部署渲染 /skl帮助 图片。

只要 plugins/skl_help/help.html 或 .env 里的 HELP_BISON 有变化，机器人启动时会自己重新渲染，
/skl帮助 每次请求时读取这张图；这个脚本用来立刻看到 help.html 的改动效果。改了 HELP_BISON 的话应该重启机器人：
运行中的机器人按启动时的设置判断，会用自己渲染的版本覆盖这一张。

    .venv/bin/python deploy/render_skl_help.py            # 机器人发送的那张图
    .venv/bin/python deploy/render_skl_help.py out.webp   # 另存一份，不动机器人的图

浏览器的位置和机器人一样：设了 PLAYWRIGHT_BROWSERS_PATH 就用它，否则用 Playwright 默认的位置
（docs/deploy-napcat.md 第 3 步装在 ~/.cache/ms-playwright）。
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
os.chdir(ROOT)  # NoneBot 从工作目录读取 .env
sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402

nonebot.init()

# 按路径加载：导入插件包会在机器人之外注册它的指令
spec = importlib.util.spec_from_file_location("skl_help_render", ROOT / "plugins" / "skl_help" / "render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

options = render.settings(nonebot.get_driver().config)
target = render.render(render.page(**options), output)
kind = "official bot" if options["official"] else "NapCat"
note = (", migration note" if options["official"] and options["migration"] else "") + (", Bison commands" if options["bison"] else "")
print(f"{target} {target.stat().st_size // 1024} KB ({kind}{note}, name {options['name']})")
