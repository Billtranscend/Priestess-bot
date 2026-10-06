"""Which Skland role a member's features use, for nonebot-plugin-skland 0.7.2's data model.

A member (a nonebot-plugin-user id, `SkUser.owner_id`) may bind several Skland accounts, each
with several roles. Boards, statistics and reminders of this project use one role per member and
game: the member's default role (`skland_character_default`), which `/skl切换终末地角色` changes.
"""

from __future__ import annotations

from nonebot_plugin_skland.model import Character, CharacterDefault, SkUser
from nonebot_plugin_user.models import Bind
from sqlalchemy import select

PLATFORM = "QQClient"


async def owner_id_of(session, qq: str | int) -> int | None:
    """The member id behind a QQ number, if that QQ ever talked to the bot."""
    return (await session.scalars(select(Bind.bind_id).where(Bind.platform == PLATFORM, Bind.platform_id == str(qq)))).first()


def default_roles(app_code: str = "endfield"):
    """SELECT (owner_id, SkUser, Character) of every member's default role in one game."""
    return (
        select(CharacterDefault.owner_id, SkUser, Character)
        .join(Character, Character.id == CharacterDefault.character_id)
        .join(SkUser, SkUser.id == Character.account_id)
        .where(CharacterDefault.app_code == app_code, Character.app_code == app_code, SkUser.owner_id == CharacterDefault.owner_id)
    )


async def default_role(session, owner_id: int, app_code: str = "endfield") -> tuple[SkUser, Character] | None:
    """(account, role) a member's commands use for one game, or None when there is none."""
    row = (await session.execute(default_roles(app_code).where(CharacterDefault.owner_id == owner_id))).first()
    return (row[1], row[2]) if row else None
