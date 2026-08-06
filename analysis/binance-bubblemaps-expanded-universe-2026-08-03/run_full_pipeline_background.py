#!/usr/bin/env python3
"""Run the rate-limited Bubblemaps completion pipeline as a persistent job."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
PID_FILE = ROOT / "pipeline-background.pid"
STATUS_FILE = ROOT / "pipeline-background-status.json"
LOG_FILE = ROOT / "pipeline-background.log"
PYTHON = PROJECT_ROOT / ".venv/bin/python"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_status(**values) -> None:
    current = {}
    if STATUS_FILE.is_file():
        try:
            current = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            current = {}
    current.update(values)
    temporary = STATUS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(STATUS_FILE)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def command(*parts: str) -> list[str]:
    return [str(PYTHON), *parts]


def worker() -> int:
    steps = [
        {
            "name": "postgresql transfer prefill",
            "optional": True,
            "args": command(
                str(ROOT / "import_pg_transfers.py"),
                "--symbols",
                "BLESS,FF,ZBT",
            ),
        },
        {
            "name": "bubblemaps missing-member capture",
            "optional": False,
            "args": command(
                "analysis/binance-bubblemaps-out-of-sample-2026-07-30/capture_bubblemaps.py",
                "--config",
                str(ROOT / "expanded_universe_config.json"),
                "--snapshot-root",
                str(ROOT / "bubblemaps-snapshot"),
                "--timeout",
                "20",
                "--max-attempts",
                "100",
                "--retry-delay",
                "3",
                "--min-interval",
                "1",
                "--concurrency",
                "4",
            ),
        },
        {
            "name": "address inventory",
            "optional": False,
            "args": command(
                str(ROOT / "build_address_inventory.py"),
                "--snapshot",
                str(ROOT / "bubblemaps-snapshot"),
                "--config",
                str(ROOT / "expanded_universe_config.json"),
                "--output-dir",
                str(ROOT / "arkham-review"),
            ),
        },
        {
            "name": "arkham label queue",
            "optional": False,
            "args": command(str(ROOT / "build_arkham_label_queue.py")),
        },
        {
            "name": "cex flow calculation",
            "optional": False,
            "args": command(str(ROOT / "compute_cex_net_flows.py")),
        },
        {
            "name": "data status",
            "optional": False,
            "args": command(str(ROOT / "build_data_status.py")),
        },
    ]
    write_status(
        pid=os.getpid(),
        state="running",
        started_at=utc_now(),
        completed_at=None,
        current_step=0,
        error=None,
    )
    for index, step in enumerate(steps, 1):
        args = step["args"]
        write_status(current_step=index, current_step_name=step["name"], command=args)
        result = subprocess.run(args, cwd=PROJECT_ROOT, check=False)
        if result.returncode:
            if step["optional"]:
                message = (
                    f"optional step {index} ({step['name']}) exited "
                    f"{result.returncode}; continuing with API fallback"
                )
                print(message, flush=True)
                write_status(optional_step_error=message)
                continue
            write_status(
                state="failed",
                completed_at=utc_now(),
                error=(
                    f"step {index} ({step['name']}) exited {result.returncode}"
                ),
            )
            return result.returncode
    write_status(state="complete", completed_at=utc_now(), current_step=len(steps))
    return 0


def detach() -> int:
    if PID_FILE.is_file():
        try:
            old_pid = int(PID_FILE.read_text().strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid and alive(old_pid):
            print(f"pipeline already running: pid {old_pid}")
            return 0

    first = os.fork()
    if first:
        os.waitpid(first, 0)
        for _ in range(100):
            if STATUS_FILE.is_file():
                try:
                    status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    status = {}
                if status.get("state") in {"starting", "running"}:
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError("background pipeline did not publish status")
        print(f"pipeline started: pid {status['pid']}; log {LOG_FILE}")
        return 0

    os.setsid()
    second = os.fork()
    if second:
        os._exit(0)

    log_handle = LOG_FILE.open("a", encoding="utf-8", buffering=1)
    devnull = open(os.devnull, "r", encoding="utf-8")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(log_handle.fileno(), sys.stdout.fileno())
    os.dup2(log_handle.fileno(), sys.stderr.fileno())
    PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    write_status(pid=os.getpid(), state="starting", launched_at=utc_now())
    code = worker()
    PID_FILE.unlink(missing_ok=True)
    os._exit(code)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    raise SystemExit(worker() if args.worker else detach())


if __name__ == "__main__":
    main()
