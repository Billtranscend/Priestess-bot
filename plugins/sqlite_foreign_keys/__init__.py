"""Turn on foreign-key enforcement for the bot's SQLite database.

SQLite ignores foreign keys unless `PRAGMA foreign_keys=ON` is issued on every connection.
nonebot-plugin-skland 0.7.2 deletes an unbound account with a plain DELETE and leaves its
roles, default-role rows and gacha records to `ON DELETE CASCADE`. Without the pragma those rows
stay behind, and since SQLite reuses the highest row id, a role bound later could inherit a
stranger's gacha history.

Loaded before every plugin that touches the database. Migrations must run with enforcement off
(they rebuild tables): deploy/orm_cli.py sets SQLITE_FOREIGN_KEYS=off before importing the bot.
"""

from __future__ import annotations

import os
import sqlite3

from nonebot import get_driver, logger, require
from nonebot.plugin import PluginMetadata
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

require("nonebot_plugin_orm")
from nonebot_plugin_orm import get_session

__plugin_meta__ = PluginMetadata(
    name="SQLite foreign keys",
    description="为机器人的 SQLite 数据库开启外键约束，解绑时角色与抽卡记录随账号一并删除。",
    usage="无指令，自动生效。",
    type="application",
)

ENFORCE = os.environ.get("SQLITE_FOREIGN_KEYS", "on").lower() != "off"


@event.listens_for(Engine, "connect")
def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    raw = getattr(dbapi_connection, "driver_connection", dbapi_connection)  # aiosqlite wraps the sqlite3 connection
    raw = getattr(raw, "_conn", raw)
    if not ENFORCE or not isinstance(raw, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


@get_driver().on_startup
async def _verify_foreign_keys() -> None:
    async with get_session() as session:
        if session.bind.dialect.name != "sqlite":
            return
        enabled = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
    if ENFORCE and not enabled:
        raise RuntimeError("SQLite foreign keys could not be enabled; unbinding would leave orphaned records")
    logger.info(f"SQLite foreign keys: {'on' if enabled else 'off (migration mode)'}")
