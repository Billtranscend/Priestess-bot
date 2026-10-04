"""Load this project's models, then invoke nonebot-plugin-orm's official CLI.

    .venv/bin/python deploy/orm_cli.py upgrade
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)  # .env and the data directories are resolved from the project root
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402, F401
from nonebot_plugin_orm.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
