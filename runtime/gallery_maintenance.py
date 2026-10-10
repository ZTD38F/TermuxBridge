#!/data/data/com.termux/files/usr/bin/python
"""Android JobScheduler entrypoint: bounded local gallery maintenance.

Author: Dāvids Krūmiņš. Designed for --charging true, --period-ms 14400000.
Never modifies user photos. Only writes private SQLite metadata and local status.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

def run() -> dict:
    import gallery_bridge_tools as g
    from gallery_ocr import battery_status
    battery = battery_status()
    temp = battery.get("temperature_c")
    if (not battery.get("available") or battery.get("level",0) < 15
        or not battery.get("charging") or (temp is not None and temp >= 43)):
        return {"ok":True,"result":"SKIPPED_POWER","battery":battery}
    root = Path.home() / ".termux-mcp-bridge"
    fs = os.statvfs(root)
    if fs.f_bavail*fs.f_frsize < 1024**3:
        return {"ok":True,"result":"SKIPPED_STORAGE"}
    state = g.gallery_status({})
    scan = {"ok": True,"result":"NOT_DUE"}
    if not state.get("last_scan") or time.time()-float(state["last_scan"]) >= 6*3600:
        scan = g.gallery_scan({})
    if not scan.get("ok"):
        return {"ok":False,"result":"SCAN_FAILED","scan":scan}
    try:
        ocr = g.gallery_ocr_index({"max_images":8,"max_seconds":48})
    except Exception as exc:
        return {"ok":False,"result":"OCR_ERROR","error":type(exc).__name__}
    return {"ok":bool(ocr.get("ok")), "result":"OK" if ocr.get("ok") else "OCR_UNAVAILABLE",
            "scan":scan,"ocr":ocr}

def main() -> int:
    private = Path.home() / ".termux-mcp-bridge"
    private.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (private / "gallery_maintenance.lock").open("a+") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            result = run()
        except Exception as exc:
            result = {"ok": False, "result": "ERROR", "error": type(exc).__name__}
        result["timestamp"] = time.time()
        status = private / "gallery_maintenance_status.json"
        tmp = private / ".gallery_maintenance_status.json.tmp"
        tmp.write_text(json.dumps(result,ensure_ascii=False) + "\n")
        tmp.chmod(0o600)
        os.replace(tmp,status)
    return 0 if result["ok"] else 1

if __name__ == "__main__":
    sys.exit(main())
