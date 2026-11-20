"""体检删除前的备份：backups/skland-health/<UTC 时间>.jsonl，只追加，一行是一次删除（一个账号或一个角色）。
每行三个键 skland_user / skland_characters / skland_gacha_record，各是一组删掉的整行（含登录凭证，所以目录 700、文件 600）。

每次只追加这一次删的：整份重写的话，一次删几百个账号要累计写几十 GB，还一直占着数据库写锁。
这个文件就是最终备份；旧的备份是一个 .json（同样三个键），records() 两种都读。
写回数据库用 deploy/restore_health_backup.py。

只用标准库：恢复脚本不启动机器人，按文件路径加载这里。
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

TABLES = ("skland_user", "skland_characters", "skland_gacha_record")
_lock = threading.Lock()  # 追加在线程里做：两行不能交错，失败时截回的长度也不能被别的追加挪动


def empty() -> dict[str, list[dict]]:
    """一次删除要备份的行，三张表各一组。"""
    return {table: [] for table in TABLES}


def append(path: Path, removed: dict) -> None:
    """把一次删除写成一行并落盘。写不完整时把文件截回写之前的长度，异常照常抛出：调用方因此不提交这次删除。"""
    line = (json.dumps(removed, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _lock:
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        size = None
        try:
            os.fchmod(fd, 0o600)
            size = os.fstat(fd).st_size
            if size == 0:
                _sync_folder(path.parent)  # 新建的文件：目录项也要落盘
            elif os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line  # 以前截回也失败、留下了半行：先把它结束，这一行才能单独读出
            _write_all(fd, line)
            os.fsync(fd)
        except BaseException:
            if size is not None:
                os.ftruncate(fd, size)
            raise
        finally:
            os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """os.write 可能只写了一部分（不抛异常）：循环到写完。"""
    view, done = memoryview(data), 0
    while done < len(data):
        written = os.write(fd, view[done:])
        if written <= 0:
            raise OSError("nothing written")
        done += written


def _sync_folder(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def records(path: Path) -> Iterator[dict | None]:
    """逐个读出备份里的删除，读不出的行给 None。

    .jsonl 按字节 b"\\n" 分行（不用 splitlines：昵称里可能有 U+2028 这类也算换行的字符），只认以换行结尾的行：
    写到一半被打断的最后一行不算，那次删除没有提交。以前的 .json 整个文件是一个。
    """
    if path.suffix == ".json":
        yield json.loads(path.read_bytes())
        return
    with path.open("rb") as file:
        for raw in file:
            if not raw.endswith(b"\n"):
                return
            try:
                record = json.loads(raw)
            except ValueError:
                record = None
            yield record if isinstance(record, dict) else None


def tally(path: Path) -> Counter:
    """备份里每张表一共有多少行；有读不出的行时抛出 ValueError。"""
    counts: Counter = Counter()
    for record in records(path):
        if record is None:
            raise ValueError("a line of the backup is unreadable")
        counts.update({table: len(record.get(table) or ()) for table in TABLES})
    return counts
