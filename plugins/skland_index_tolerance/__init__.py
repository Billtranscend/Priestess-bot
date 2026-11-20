"""成员在卡片上选账号时，把 "01" 当作账号 1。

/skl解绑 的账号卡片把账号编号为 01、02……，而 nonebot-plugin-skland 0.7.2 拿回复去和 "1"、"2"……
比较：成员照着卡片输入，却被告知编号无效。这里给该插件提示的校验函数套一层，让带前导零
或全角数字写的编号也算作它本身的数字。上游之后对回复的处理（int()）本来就两种都能应付。
"""

from __future__ import annotations

from nonebot import require
from nonebot.plugin import PluginMetadata

from .tolerant import tolerant

require("nonebot_plugin_skland")

from nonebot_plugin_skland.commands import bind as _bind  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="Skland index tolerance",
    description="选择账号时，「01」这样带前导零的序号按「1」处理。",
    usage="无指令，自动生效。",
    type="application",
)

_prompt_until = _bind.prompt_until


async def _prompt_until_tolerant(message, checker, *args, **kwargs):
    return await _prompt_until(message, tolerant(checker), *args, **kwargs)


_bind.prompt_until = _prompt_until_tolerant
