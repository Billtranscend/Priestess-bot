"""通过旧机器人绑定、但官方机器人从未认出的成员：保留一段时间，不提供服务。

官方平台不显示 QQ 号，所以在官方机器人看来，通过旧机器人做的绑定不属于它见过的任何人。
旧机器人还在群里时，可以靠监听消息把两边对上；它退出所有群后这条路就断了，
剩下的只能靠成员自己：在与机器人的私聊里重新扫码登录森空岛。

规则：从旧机器人离开那天起，这样的成员不再参与每晚签到（他们由此察觉），
账号和记录保留 KEEP_DAYS 天（一个游戏版本持续 40 到 42 天），到期仍未回来的，
和其他失效绑定一样解绑。

这里不涉及 nonebot 和数据库：只是单纯的判断，可以直接当作纯函数测试。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

KEEP_DAYS = 45
DAY = 86400


def waiting_owners(binds: Iterable[tuple[str, int]], recognised: Callable[[str], bool], superusers: set[str]) -> set[int]:
    """官方机器人一个号码都不认识的所有者（bind id）。

    binds：每个有森空岛账号的成员的（平台上的号码，它对应的身份）。成员的任意一个号码被认识，
    就算认识这个成员；超级用户始终算认识。
    """
    binds = list(binds)
    known = {owner for number, owner in binds if number in superusers or recognised(number)}
    return {owner for _, owner in binds} - known


def overdue(gone_at: float | None, now: float, keep_days: int = KEEP_DAYS) -> bool:
    """回来的期限是否已过？旧机器人还在时（gone_at 为 None）永远不算过期。"""
    return gone_at is not None and now - gone_at >= keep_days * DAY


def days_left(gone_at: float | None, now: float, keep_days: int = KEEP_DAYS) -> int | None:
    """距离等待中的绑定被移除还有几整天（到期后为 0）；没有在计时时为 None。"""
    if gone_at is None:
        return None
    return max(0, int((gone_at + keep_days * DAY - now + DAY - 1) // DAY))
