#!/usr/bin/env python3
"""Opportunistic Sonoryx gallery worker independent of Android JobScheduler.

Author: Dāvids Krūmiņš.
The worker holds an advisory lock for its lifetime, invokes a *versioned*
gallery_maintenance entrypoint and never runs CPU-intensive OCR on low charge.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(os.environ.get("TERMUXBRIDGE_ROOT", str(Path.home() / "termux-mcp-bridge")))
STATE = ROOT / "state"
STATUS = STATE / "gallery_worker_status.json"
PID = STATE / "gallery_worker.pid"
LOCK = STATE / "gallery_worker.lock"
DISABLE = STATE / "gallery_worker.disabled"
STOP = False

def _stop(_signal, _frame):
    global STOP
    STOP = True

def single_iteration(root: Path = ROOT, *, timeout: int = 85) -> dict:
    path = root / "current" / "gallery_maintenance.py"
    if not path.is_file():
        return {"ok": False, "result": "MISSING_RELEASE_HELPER"}
    try:
        result = subprocess.run([sys.executable, str(path)], capture_output=True,
                                text=True, timeout=timeout, check=False)
        # gallery_maintenance stores detailed JSON in private user-owned storage;
        # do not echo OCR text or environment values into bridge logs.
        return {"ok": result.returncode == 0,
                "result": "COMPLETED" if result.returncode == 0 else "MAINTENANCE_FAILED",
                "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": "TIMED_OUT"}
    except OSError:
        return {"ok": False, "result": "SPAWN_FAILED"}

def write_status(value: dict, path: Path = STATUS):
    data = dict(value, timestamp=time.time())
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)

def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK.open("a+") as fp:
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if DISABLE.exists():
            write_status({"ok": True, "result": "DISABLED"})
            return 0
        PID.write_text(str(os.getpid()) + "\n", encoding="ascii")
        PID.chmod(0o600)
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        once = "--once" in sys.argv
        try:
            while not STOP:
                if DISABLE.exists():
                    write_status({"ok": True, "result": "DISABLED"})
                    break
                result = single_iteration()
                write_status(result)
                if once:
                    break
                # Ignore wakeups; use bounded polling without OS JobScheduler.
                # Android may suspend Termux when background activity is restricted.
                for _ in range(30):  # 5 minutes, interrupts promptly on SIGTERM
                    if STOP:
                        break
                    time.sleep(10)
        finally:
            if PID.exists() and PID.read_text().strip() == str(os.getpid()):
                PID.unlink()
    return 0

if __name__ == "__main__":
    sys.exit(main())
