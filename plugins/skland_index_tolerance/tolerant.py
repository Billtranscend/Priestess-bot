"""套上的校验本身，与被套用的插件无关（这样可以单独试）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def tolerant(check: Callable[[Any], bool]) -> Callable[[Any], bool]:
    """`check` 的包装：回复是一个数字时，只要按普通写法 `check` 会通过，它也通过（"01" -> "1"）。"""

    def wrapped(message: Any) -> bool:
        if check(message):
            return True
        text = message.extract_plain_text().strip()
        if not text.isdecimal() or len(text) > 4:
            return False
        plain = str(int(text))
        return plain != text and check(type(message)(plain))

    return wrapped
