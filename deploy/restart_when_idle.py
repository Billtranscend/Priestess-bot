"""Restart nonebot.service only when no command is being handled.

    .venv/bin/python deploy/restart_when_idle.py [max_wait_seconds]

Reads the service journal since the current start and pairs every
"Event will be handled by Matcher(...)" with that matcher's "running complete" / "failed" line.
A total-count comparison is not enough: unrelated extra "complete" lines can hide one handler
that is still running, and restarting then cuts off its reply. Also waits while the endgame
clear collection is in progress, and through the nightly Skland sign (00:05-00:50 Beijing) unless
both sign results were already written that night: scheduled jobs leave no matcher lines, and
restarts at 00:20 and 00:25 killed the sign twice (2026-11-03/04). Restarts after two consecutive
idle checks, or gives up.
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
SIGN_WINDOW = ((0, 5), (0, 50))  # Beijing time: the sign jobs start 00:15 and 00:20 and take about 20 minutes each
SIGN_RESULTS = [Path(__file__).resolve().parents[1] / "cache" / "nonebot_plugin_skland" / name
                for name in ("sign_result.json", "endfield_sign_result.json")]
# Alconna commands (all of nonebot-plugin-skland) are announced as "AlconnaMatcherMeta(...)" but finish as "AlconnaMatcher(...)".
STARTED = re.compile(r"Event will be handled by (\w*Matcher)(?:Meta)?(\(.*?\))\s*$")
# "running is cancelled": a run preprocessor refused the command (e.g. the binding guard).
# The progress line is shared by the clears collection and the builds-only one, which end differently.
COLLECTION_ENDS = ("War Echoes clears collected", "War Echoes collection failed", "Operator builds collected", "Operator builds collection failed")
FINISHED = re.compile(r"(?:Running )?(\w*Matcher\(.*?\)) (?:running complete|running is cancelled|failed\.?)\s*$")


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
    collecting = False
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
    reasons = [f"{name} x{count}" for name, count in running.items() if count > 0]
    if collecting:
        reasons.append("endgame clear collection in progress")
    return reasons


def in_sign_window(now: datetime | None = None) -> bool:
    """Inside the nightly sign window, and the sign not yet finished (both result files written after 00:15)."""
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
