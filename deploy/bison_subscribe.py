"""Give newly joined groups the default nonebot-bison subscriptions.

    bison_subscribe.py --db <bison data.db> --requests <dir> --backups <dir>

Bison's admin API needs a one-time token that only a superuser can obtain in chat, so a group
cannot be subscribed through it automatically. The bot (plugins/group_onboarding) therefore drops
an empty file named after the group id into the requests directory, and this script, run by a
systemd path unit outside the bot's sandbox, adds the rows Bison itself would add:

  - only for targets Bison already follows (a new target would need Bison's scheduler to be told);
  - only subscriptions the group does not have yet; existing ones are never changed;
  - after a backup of the database, in one short transaction.

Bison reads a target's subscribers from the database on every dispatch, so no restart is needed.
The requests directory is writable by the bot and therefore untrusted: only file names made of
digits are used, nothing is read from the files, and every entry is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

POSTS = [1, 2, 3, 4, 6]  # 一般动态, 专栏文章, 视频, 纯文字, 直播推送
NO_LOTTERY = ["~互动抽奖"]  # "~" excludes posts with that tag
LIVE_START = [1]  # 开播提醒 only
# (platform, target id on that platform, categories, tags): 明日方舟 and 明日方舟终末地 on Bilibili,
# their posts and the start of their live streams
TEMPLATE = (
    ("bilibili", "161775300", POSTS, NO_LOTTERY),
    ("bilibili", "1265652806", POSTS, NO_LOTTERY),
    ("bilibili-live", "161775300", LIVE_START, []),
    ("bilibili-live", "1265652806", LIVE_START, []),
)
LABELS = {"bilibili": "动态", "bilibili-live": "直播"}
GROUP_ID = re.compile(r"[1-9][0-9]{4,11}")
MAX_PER_RUN = 20
KEEP_BACKUPS = 30


def take_requests(directory: Path) -> list[int]:
    """Group ids waiting in the directory; every entry is removed, valid or not."""
    groups: list[int] = []
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in sorted(os.listdir(fd)):
            if GROUP_ID.fullmatch(name) and len(groups) < MAX_PER_RUN:
                groups.append(int(name))
            try:
                os.unlink(name, dir_fd=fd)
            except IsADirectoryError:
                os.rmdir(name, dir_fd=fd)
            except OSError as e:
                print(f"could not remove request entry: {type(e).__name__}", file=sys.stderr)
    finally:
        os.close(fd)
    return groups


def backup(db_path: Path, backups: Path) -> None:
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = backups / f"data-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
    source, copy = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        source.close()
        copy.close()
    os.chmod(target, 0o600)
    for old in sorted(backups.glob("data-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()


def subscribe(db_path: Path, groups: list[int], template=TEMPLATE) -> dict[int, list[str]]:
    """{group id: names of the targets newly subscribed}."""
    added: dict[int, list[str]] = {}
    db = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        targets = []
        for platform, target, categories, tags in template:
            row = db.execute("SELECT id, target_name FROM nonebot_bison_target WHERE platform_name = ? AND target = ?", (platform, target)).fetchone()
            if row is None:
                print(f"template target {platform}/{target} is not followed by Bison; skipped", file=sys.stderr)
            else:
                targets.append((row[0], f"{row[1]} {LABELS.get(platform, platform)}", categories, tags))
        for group in groups:
            user_target = json.dumps({"platform_type": "QQ Group", "group_id": group})  # the exact text Bison stores
            row = db.execute("SELECT id FROM nonebot_bison_user WHERE user_target = ?", (user_target,)).fetchone()
            user_id = row[0] if row else db.execute("INSERT INTO nonebot_bison_user (user_target) VALUES (?)", (user_target,)).lastrowid
            added[group] = []
            for target_id, name, categories, tags in targets:
                if db.execute("SELECT 1 FROM nonebot_bison_subscribe WHERE target_id = ? AND user_id = ?", (target_id, user_id)).fetchone():
                    continue
                db.execute(
                    "INSERT INTO nonebot_bison_subscribe (target_id, user_id, categories, tags) VALUES (?, ?, ?, ?)",
                    (target_id, user_id, json.dumps(categories), json.dumps(tags)),  # the same text Bison writes
                )
                added[group].append(name)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return added


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("--db", "--requests", "--backups"):
        parser.add_argument(name, type=Path, required=True)
    args = parser.parse_args()
    groups = take_requests(args.requests)
    if not groups:
        return 0
    backup(args.db, args.backups)
    for group, names in subscribe(args.db, groups).items():
        print(f"group ...{str(group)[-3:]}: {'subscribed to ' + ', '.join(names) if names else 'already subscribed, nothing changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
