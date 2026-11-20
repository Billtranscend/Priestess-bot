"""只在没有指令正在处理时重启 nonebot.service。

    .venv/bin/python deploy/restart_when_idle.py [max_wait_seconds]

读取服务本次启动以来的日志，把每条 "Event will be handled by Matcher(...)" 和该 matcher 的
"running complete" / "failed" 行配对。只比较总数不够：无关的多余 "complete" 行可能掩盖一个
仍在运行的处理器，这时重启会截断它的回复。终局通关数据收集、卫戍榜统计（每天 10:30 约 7 分钟，
以及启动后的补统计）和森空岛体检（每周一 04:00，以及 /森空岛体检）开始了还没结束时也会等待；
夜间森空岛签到（北京时间 00:05-00:50）期间同样等待，除非当晚两份签到结果都已写入：定时任务不留
matcher 日志，00:20 和 00:25 的重启曾两次打断签到。连续两次检查空闲后重启，否则超时放弃。
"""

import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICE = "nonebot.service"
BEIJING = timezone(timedelta(hours=8))
SIGN_WINDOW = ((0, 5), (0, 50))  # 北京时间：两个签到任务分别在 00:15 和 00:20 开始，各需约 20 分钟
SIGN_RESULTS = [Path(__file__).resolve().parents[1] / "cache" / "nonebot_plugin_skland" / name
                for name in ("sign_result.json", "endfield_sign_result.json")]
# Alconna 指令（nonebot-plugin-skland 的指令全都是）开始处理时日志记为 "AlconnaMatcherMeta(...)"，
# 结束时却记为 "AlconnaMatcher(...)"。
STARTED = re.compile(r"Event will be handled by (\w*Matcher)(?:Meta)?(\(.*?\))\s*$")
# "running is cancelled"：运行预处理器拒绝了这条指令（例如绑定检查）。
# 进度行由通关数据收集和只收集配置的那种共用，两者的结束行不同。
COLLECTION_ENDS = ("War Echoes clears collected", "War Echoes collection failed", "Operator builds collected", "Operator builds collection failed")
FINISHED = re.compile(r"(?:Running )?(\w*Matcher\(.*?\)) (?:running complete|running is cancelled|failed\.?)\s*$")
# 卫戍榜统计：排定（启动后先等一分钟）或开始时记一行，结束时记上榜人数或失败（plugins/arknights_garrison 的 collect）。
BOARD_COUNT_STARTS = re.compile(r"Arknights stronghold board: count (?:scheduled|started)")
BOARD_COUNT_ENDS = re.compile(r"Arknights stronghold board: (?:\d+ on the board of \d+ members|count failed)")
# 森空岛体检（plugins/skland_health 的 run）：开始记 started，结束记结果（"{'apply': …"）或 failed。
HEALTH_STARTS = re.compile(r"Skland health check: started")
HEALTH_ENDS = re.compile(r"Skland health check: (?:\{'apply'|failed)")


def journal() -> list[str]:
    since = subprocess.run(
        ["systemctl", "show", SERVICE, "-p", "ActiveEnterTimestamp", "--value"], capture_output=True, text=True, check=True
    ).stdout.strip()
    out = subprocess.run(
        ["sudo", "-n", "journalctl", "-u", SERVICE, "--since", since, "--no-pager", "-o", "cat"], capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


def busy(lines: list[str]) -> list[str]:
    running: Counter[str] = Counter()
    collecting = counting = checking = False
    for line in lines:
        if match := STARTED.search(line):
            running[match.group(1) + match.group(2)] += 1
        elif match := FINISHED.search(line):
            if running[match.group(1)] > 0:
                running[match.group(1)] -= 1
        elif "War Echoes collection progress" in line:
            collecting = True
        elif any(end in line for end in COLLECTION_ENDS):
            collecting = False
        elif BOARD_COUNT_STARTS.search(line):
            counting = True
        elif BOARD_COUNT_ENDS.search(line):
            counting = False
        elif HEALTH_STARTS.search(line):
            checking = True
        elif HEALTH_ENDS.search(line):
            checking = False
    reasons = [f"{name} x{count}" for name, count in running.items() if count > 0]
    if collecting:
        reasons.append("endgame clear collection in progress")
    if counting:
        reasons.append("Arknights stronghold board count in progress")
    if checking:
        reasons.append("Skland health check in progress")
    return reasons


def in_sign_window(now: datetime | None = None) -> bool:
    """处于夜间签到时段内，且签到尚未完成（两份结果文件都在 00:15 之后写入才算完成）。"""
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    (h0, m0), (h1, m1) = SIGN_WINDOW
    if not (now.replace(hour=h0, minute=m0, second=0, microsecond=0) <= now < now.replace(hour=h1, minute=m1, second=0, microsecond=0)):
        return False
    started = now.replace(hour=0, minute=15, second=0, microsecond=0).timestamp()
    return not all(p.exists() and p.stat().st_mtime >= started for p in SIGN_RESULTS)


def main() -> int:
    deadline = time.time() + (float(sys.argv[1]) if len(sys.argv) > 1 else 180)
    idle_checks = 0
    while time.time() < deadline:
        reasons = busy(journal()) + (["nightly Skland sign (00:05-00:50 Beijing) not finished"] if in_sign_window() else [])
        if reasons:
            idle_checks = 0
            print("busy:", "; ".join(r[-70:] for r in reasons), flush=True)
            time.sleep(3)
            continue
        idle_checks += 1
        if idle_checks >= 2:
            subprocess.run(["sudo", "-n", "systemctl", "restart", SERVICE], check=True)
            print("restarted at", time.strftime("%H:%M:%S", time.gmtime()), "UTC", flush=True)
            return 0
        time.sleep(1.5)
    print("gave up: still busy", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
