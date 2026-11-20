"""先加载本项目的模型，再调用 nonebot-plugin-orm 自带的命令行。

    .venv/bin/python deploy/orm_cli.py upgrade
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)  # .env 和数据目录都以项目根目录为基准
sys.path.insert(0, str(ROOT))
# 迁移会重建表；外键约束开着时，删除旧表会级联删掉它的子表数据。
os.environ["SQLITE_FOREIGN_KEYS"] = "off"  # 这个开关由插件 plugins/sqlite_foreign_keys 读取

import bot  # noqa: E402, F401
from nonebot_plugin_orm.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
