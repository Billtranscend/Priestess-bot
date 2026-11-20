"""把每周体检删掉的森空岛账号、角色和抽卡记录，从体检备份写回数据库。

    .venv/bin/python deploy/restore_health_backup.py <备份文件> <数据库文件> [--apply]

备份文件是 backups/skland-health/ 下的 .jsonl（一行一次删除，见 plugins/skland_health/backup.py），或以前的 .json。
不带 --apply 只核对、报数，什么都不写；带 --apply 时核对和写回在同一个事务里，有任何冲突就一行都不写。
只打印行数和要写回的账号属于哪些成员（bind id，不是 QQ 号），不打印行的内容（账号行里有登录凭证）。

备份先写、删除后提交，所以提交失败的那几次也在备份里。这样的账号要是后来因为别的原因没了（例如成员自己
/skl解绑），这里会把它连同凭证写回：--apply 之前先看列出的成员该不该回来。

先停掉机器人，写回后再启动：默认角色不在备份里，启动时 skland_health 会给写回来的成员重新挑一个。

三张表的 id 都不是 AUTOINCREMENT，SQLite 会把删掉的最大 id 再发给新行，所以不能按 id 直接 INSERT OR IGNORE。
每一行先按自然键核对（账号：owner_id + skland_user_id；角色：account_id + app_code + channel_master_id + role_id；
抽卡记录：character_id + gacha_ts + pos）：
  已在库里  同一个 id、同样的自然键：那次删除没有提交（备份可能比真正删掉的多），跳过
  写回      id 空着，自然键也不在库里：按原来的 id 写回
  冲突      id 被别的行占了；自然键在库里换了 id（例如成员已经重新绑定）；或上一级（角色的账号、记录的角色）
            冲突或不在库里。上一级不在这份备份里时（单独删掉的角色），库里有这个 id 就算数
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("skland_health_backup", ROOT / "plugins" / "skland_health" / "backup.py")
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)  # 不经过插件包：那里会启动 nonebot

NATURAL = {
    "skland_user": ("owner_id", "skland_user_id"),
    "skland_characters": ("account_id", "app_code", "channel_master_id", "role_id"),
    "skland_gacha_record": ("character_id", "gacha_ts", "pos"),
}
PARENT = {"skland_characters": ("account_id", "skland_user"), "skland_gacha_record": ("character_id", "skland_characters")}
BACK, THERE, CLASH = "写回", "已在库里", "冲突"


def read(path: Path) -> tuple[dict[str, list[dict]], int]:
    """备份里三张表的行（同一个 id 只留一次），以及读不出的行数（写备份失败的那几次，删除没有提交）。"""
    rows: dict[str, dict] = {table: {} for table in backup.TABLES}
    unreadable = 0
    for record in backup.records(path):
        if record is None:
            unreadable += 1
            continue
        for table in backup.TABLES:
            for row in record.get(table) or ():
                rows[table].setdefault(row["id"], row)
    return {table: list(found.values()) for table, found in rows.items()}, unreadable


def _has(db: sqlite3.Connection, table: str, row_id: int) -> bool:
    return db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is not None


def verdict(db: sqlite3.Connection, table: str, row: dict, settled: dict[str, dict[int, str]]) -> str:
    """这一行是写回、已在库里还是冲突；settled 是上一级表已经下过的结论。"""
    if table in PARENT:
        column, parent = PARENT[table]
        known = settled[parent].get(row[column])
        if known == CLASH or (known is None and not _has(db, parent, row[column])):
            return CLASH
    keys = NATURAL[table]
    natural = tuple(row[key] for key in keys)
    found = db.execute(f"SELECT {', '.join(keys)} FROM {table} WHERE id = ?", (row["id"],)).fetchone()
    if found is not None:
        return THERE if tuple(found) == natural else CLASH
    taken = db.execute(f"SELECT 1 FROM {table} WHERE " + " AND ".join(f"{key} IS ?" for key in keys), natural).fetchone()
    return CLASH if taken else BACK


def plan(db: sqlite3.Connection, rows: dict[str, list[dict]]) -> tuple[dict[str, list[dict]], Counter]:
    """每张表要写回的行，以及每种结论的行数（"表名 结论" -> 行数）。按账号、角色、记录的顺序核对。"""
    settled: dict[str, dict[int, str]] = {table: {} for table in backup.TABLES}
    todo: dict[str, list[dict]] = {table: [] for table in backup.TABLES}
    counts: Counter = Counter()
    for table in backup.TABLES:
        for row in rows[table]:
            said = settled[table][row["id"]] = verdict(db, table, row, settled)
            counts[f"{table} {said}"] += 1
            if said == BACK:
                todo[table].append(row)
    return todo, counts


def missing_columns(db: sqlite3.Connection, todo: dict[str, list[dict]]) -> list[str]:
    """备份里有、数据库里已经没有的列（数据库结构变了）。"""
    missing = set()
    for table, rows in todo.items():
        columns = {line[1] for line in db.execute(f"PRAGMA table_info({table})")}
        missing |= {f"{table}.{name}" for row in rows for name in row if name not in columns}
    return sorted(missing)


def write(db: sqlite3.Connection, todo: dict[str, list[dict]]) -> None:
    for table in backup.TABLES:
        for row in todo[table]:
            names = list(row)
            db.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", [row[name] for name in names])


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    args = [arg for arg in argv if arg != "--apply"]
    if len(args) != 2 or not Path(args[0]).is_file() or not Path(args[1]).is_file():
        print(__doc__)
        return 2
    rows, unreadable = read(Path(args[0]))
    db = sqlite3.connect(Path(args[1]).resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=30)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")  # 核对到写回之间，库不能被别人改
        try:
            todo, counts = plan(db, rows)
            for key in sorted(counts):
                print(f"{key}：{counts[key]}")
            if owners := sorted({row["owner_id"] for row in todo["skland_user"]}):
                print(f"写回的账号属于这些成员（bind id）：{', '.join(map(str, owners))}")
            if unreadable:
                print(f"读不出的行（写备份失败的那几次，删除没有提交）：{unreadable}，已跳过")
            if any(key.endswith(CLASH) for key in counts):
                print("有冲突：一行都没写。冲突的行要按实际情况手动处理")
                return 1
            if missing := missing_columns(db, todo):
                print(f"数据库里没有这些列：{', '.join(missing)}（结构变了），一行都没写")
                return 1
            if not apply:
                print("只核对，没有写。确认无误后加 --apply 写回")
                return 0
            write(db, todo)
            db.execute("COMMIT")
            print(f"已写回 {sum(len(rows) for rows in todo.values())} 行")
            return 0
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
