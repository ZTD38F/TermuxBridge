#!/usr/bin/env python3
"""Sanitized, verifiable local health and privacy maintenance for TermuxBridge.

Only localhost is contacted, using the existing per-installation auth files.
A running tunnel client does *not* prove the remote ChatGPT tunnel is connected.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.request

JOB_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9]+\.json$")
ROLES = {
    "tunnel": ("bridge.pid", "tunnel"),
    "watchdog": ("watchdog.pid", "watchdog"),
    "supervisor": ("supervisor.pid", "supervisor"),
    "mcp": ("server.pid", "backend"),
    "proxy": ("proxy.pid", "proxy"),
}


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def read_generation(root: Path) -> str:
    try:
        return (root / "source_commit").read_text().strip()
    except OSError:
        return ""


def pid_file(root: Path, filename: str) -> int:
    try:
        return int((root / filename).read_text().strip())
    except (OSError, ValueError):
        return -1


def owned_processes(root: Path) -> dict[str, bool]:
    # Shared verifier used by bridge recovery; no generic pid-only trust.
    try:
        sys.path.insert(0, str(root))
        from recover_bridge_port import managed_role_identity
    except ImportError:
        return {name: False for name in ROLES}
    return {
        name: managed_role_identity(pid_file(root, filename), root, role) is not None
        for name, (filename, role) in ROLES.items()
    }


def authenticated_local_health(root: Path, port: int, header: str, key_file: str, expected_pid: int) -> bool:
    if not 1024 <= port <= 65535 or expected_pid <= 1:
        return False
    try:
        token = (root / "secrets" / key_file).read_text().strip()
        if len(token) < 32:
            return False
        path = "/__bridge/healthz" if header == "X-Bridge-Token" else "/healthz"
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", headers={header: token})
        # Do not route private localhost probes through proxy environment.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            body = json.load(response)
        return isinstance(body, dict) and body.get("ok") is True and body.get("pid") == expected_pid
    except (OSError, ValueError, KeyError, TimeoutError):
        return False


def check_services(root: Path, route: dict) -> dict[str, bool]:
    result = owned_processes(root)
    result["supervisor_http"] = result["supervisor"] and authenticated_local_health(
        root, 8765, "X-Bridge-Token", "router_token", pid_file(root, "supervisor.pid"))
    try:
        port = int(route.get("port", 0) or 0)
    except (ValueError, TypeError):
        port = 0
    result["mcp_http"] = result["mcp"] and authenticated_local_health(
        root, port, "X-Bridge-Backend-Token",
        "backend_token", pid_file(root, "server.pid"))
    return result


def scrub_legacy_job_args(root: Path) -> int:
    """Drop obsolete plaintext argv, retaining job PID, log, cwd and other metadata.

    No backup containing sensitive historical arguments is generated.
    Does not alter logs or running processes.
    """
    folder = root / "jobs"
    if not folder.is_dir() or folder.is_symlink():
        return 0
    changed = 0
    for file in folder.iterdir():
        if not JOB_ID.fullmatch(file.name) or file.is_symlink():
            continue
        try:
            if file.stat().st_size > 65536:
                continue
            data = load(file)
            if "argv" not in data:
                continue
            data.pop("argv")
            temp = file.with_name("." + file.name + ".privacy.tmp")
            with temp.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(data, separators=(",", ":")) + "\n")
            temp.chmod(0o600)
            os.replace(temp, file)
            changed += 1
        except (OSError, ValueError):
            # Keep any file we couldn't safely process unchanged.
            continue
    return changed


def evaluate(root: Path, start_rc: int, update_rc: int,
             *, services: dict[str, bool] | None = None,
             now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    route = load(root / "state" / "route.json")
    update = load(root / "state" / "update.json")
    tunnel = load(root / "state" / "tunnel_status.json")
    previous = load(root / "state" / "maintenance.json")
    generation = read_generation(root)
    consistent = bool(generation) and generation == route.get("generation") and (
        generation == update.get("current_generation")) and update.get("phase") == "COMMITTED"
    checks = services if services is not None else check_services(root, route)
    healthy = bool(checks) and all(checks.values())

    if update_rc == 75 or start_rc == 75:
        result, reason = "BUSY", "CONCURRENT_OPERATION"
    elif start_rc != 0 or update_rc != 0:
        result, reason = "FAILED", "STARTUP_OR_UPDATE_FAILED"
    elif not consistent:
        result, reason = "DEGRADED", "GENERATION_MISMATCH"
    elif not healthy:
        result, reason = "DEGRADED", "LOCAL_SERVICES_UNHEALTHY"
    else:
        result, reason = "OK", "NONE"

    attempts = previous.get("consecutive_failures", 0)
    if not isinstance(attempts, int) or attempts < 0:
        attempts = 0
    if result == "OK":
        attempts = 0
        last_success = now.isoformat(timespec="seconds")
    elif result != "BUSY":
        attempts += 1
        last_success = previous.get("last_success_at")
    else:
        last_success = previous.get("last_success_at")

    try:
        free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    except OSError:
        free_mb = None
    return {
        "last_checked_at": now.isoformat(timespec="seconds"),
        "last_success_at": last_success,
        "result": result,
        "reason": reason,
        "consecutive_failures": attempts,
        "startup_exit_code": start_rc,
        "updater_exit_code": update_rc,
        "installed_generation": generation,
        "active_generation": route.get("generation"),
        "update_phase": update.get("phase"),
        "tunnel_state": tunnel.get("state"),
        "verified_active_generation": bool(consistent),
        "local_processes": checks,
        "free_storage_mb": free_mb,
        "tunnel_local_only": True,
    }


def write_status(root: Path, record: dict) -> None:
    state = root / "state" / "maintenance.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    temp = state.with_suffix(".tmp")
    temp.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, state)


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("Usage: maintenance_health.py ROOT START_RC UPDATE_RC")
    root = Path(sys.argv[1]).expanduser().resolve()
    start_rc, update_rc = map(int, sys.argv[2:])
    removed = scrub_legacy_job_args(root)
    record = evaluate(root, start_rc, update_rc)
    record["legacy_job_metadata_scrubbed"] = removed
    write_status(root, record)
    print("Maintenance:", record["result"], "reason:", record["reason"],
          "generation:", record["installed_generation"][:12],
          "local tunnel:", record["local_processes"].get("tunnel"))
    return 0 if record["result"] in {"OK", "BUSY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
