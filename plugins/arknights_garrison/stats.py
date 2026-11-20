"""卫戍榜的数字：每个成员一行，取自上游算好的 GarrisonView。不依赖 nonebot，测试可以直接加载。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Standing:
    """卫戍榜上的一行（每天统计时为每个成员保存的数字）。"""

    owner: int  # 机器人自己的用户 id
    name: str  # 游戏内的名字（不在本群时显示它）
    trophies: int
    total: int
    wins: int
    band: str  # 最常用的策略

    @property
    def rate(self) -> int:
        return round(100 * self.wins / self.total) if self.total else 0


def standing(owner: int, view) -> Standing:
    """GarrisonView -> 榜上的一行。"""
    trophies = next((m.value for m in view.metrics if m.label == "奖杯数量"), "0")
    return Standing(
        owner=owner,
        name=view.nickname,
        trophies=int(trophies) if str(trophies).isdigit() else 0,
        total=sum(m.total for m in view.modes),
        wins=sum(m.wins for m in view.modes),
        band=view.strategies[0].name if view.strategies else "",
    )


def ranked(standings: list[Standing]) -> list[Standing]:
    """奖杯多的在前；奖杯一样时完成局数多的在前，再一样时场次少的在前。没打过的不上榜。"""
    return sorted((s for s in standings if s.total > 0), key=lambda s: (-s.trophies, -s.wins, s.total, s.name))
