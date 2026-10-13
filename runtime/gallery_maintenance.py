#!/data/data/com.termux/files/usr/bin/python3
"""Legacy TermuxService entrypoint after gallery moved into PhotoIntelligence.

Compatibility ONLY. The actual gallery pipeline is owned by the addon. This
script never launches another OCR worker, modifies photos, touches the SQLite
database, restarts the bridge, or manipulates Android charging thresholds.
Author: @dvkrum
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def _process_matches(pid: int, expected: Path, proc_root: Path) -> bool:
    if pid < 2:
        return False
    proc = proc_root / str(pid)
    try:
        if proc.stat().st_uid != os.getuid():
            return False
        argv = [x.decode("utf-8", errors="replace")
                for x in (proc / "cmdline").read_bytes().split(b"\0") if x]
        return len(argv) >= 2 and Path(argv[1]).resolve() == expected.resolve()
    except (OSError, ValueError):
        return False


def inspect(home: Path, *, proc_root: Path = Path("/proc"), now: float | None = None) -> dict:
    """Evaluate actual service/worker ownership; never modify device state."""
    now = time.time() if now is None else now
    root = home / ".termux-mcp-bridge" / "addons" / "photo-intelligence"
    release = root / "current"
    state = root / "state"
    if not release.is_dir():
        return {"ok": False, "reason": "PHOTO_INTELLIGENCE_NOT_INSTALLED"}
    try:
        service = json.loads((state / "service_status.json").read_text())
        pipeline = json.loads((state / "pipeline_status.json").read_text())
        service_pid = int(service["pid"])
        worker_pid = int(service["children"]["pipeline"]["pid"])
        actual_pid = int(pipeline["pid"])
        lock_pid = int((state / "pipeline_scheduler.pid").read_text().strip())
        freshness = (0 <= now - float(service["timestamp"]) <= 120
                     and 0 <= now - float(pipeline["timestamp"]) <= 120)
        correct = (
            service.get("state") == "RUNNING"
            and pipeline.get("state") in
                {"RUNNING", "IDLE", "COMPLETE", "DEFERRED_SOURCE", "POWER_PAUSED"}
            and service["children"]["pipeline"].get("running") is True
            and freshness
            and worker_pid == actual_pid == lock_pid
            and _process_matches(service_pid, release / "service.py", proc_root)
            and _process_matches(worker_pid,
                                 release / "runtime" / "pipeline_scheduler.py",
                                 proc_root)
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        correct = False
    if not correct:
        return {"ok": False, "reason": "PHOTO_INTELLIGENCE_PIPELINE_UNVERIFIED"}
    # The old caller no longer needs to start work: PhotoIntelligence already
    # owns continuous scans, OCR, visual search and metadata maintenance.
    return {"ok": True, "mode": "delegated_to_photo_intelligence",
            "state": pipeline["state"]}


def main() -> int:
    result = inspect(Path.home())
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
