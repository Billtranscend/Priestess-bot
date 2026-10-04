"""Restart nonebot.service only when no command is being handled.

    .venv/bin/python deploy/restart_when_idle.py [max_wait_seconds]

Reads the service journal since the current start and pairs every
"Event will be handled by Matcher(...)" with that matcher's "running complete" / "failed" line.
A total-count comparison is not enough: unrelated extra "complete" lines can hide one handler
that is still running, and restarting then cuts off its reply. Also waits while the endgame
clear collection is in progress. Restarts after two consecutive idle checks, or gives up.
"""

import re
import subprocess
import sys
import time
from collections import Counter

SERVICE = "nonebot.service"
# Alconna commands (all of nonebot-plugin-skland) are announced as "AlconnaMatcherMeta(...)" but finish as "AlconnaMatcher(...)".
STARTED = re.compile(r"Event will be handled by (\w*Matcher)(?:Meta)?(\(.*?\))\s*$")
# "running is cancelled": a run preprocessor refused the command (e.g. the binding guard).
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
        elif "War Echoes clears collected" in line or "War Echoes collection failed" in line:
            collecting = False
    reasons = [f"{name} x{count}" for name, count in running.items() if count > 0]
    if collecting:
        reasons.append("endgame clear collection in progress")
    return reasons


def main() -> int:
    deadline = time.time() + (float(sys.argv[1]) if len(sys.argv) > 1 else 180)
    idle_checks = 0
    while time.time() < deadline:
        reasons = busy(journal())
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
