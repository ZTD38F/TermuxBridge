#!/usr/bin/env python3
"""Supervise the authenticated tunnel without restarting a healthy local MCP stack.

Only a verified tunnel-client executable may be adopted. This program does
not generate or rotate any credentials and does not expose log details in status.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

try:
    from .recover_bridge_port import managed_role_identity
except ImportError:  # standalone Termux deployment places both modules together
    from recover_bridge_port import managed_role_identity

STOP = False


def on_signal(_signum, _frame):
    global STOP
    STOP = True


def classify_error(message: str) -> str:
    """Classify only; NEVER include raw logs or secrets in status JSON."""
    text = message.lower()
    if re.search(r"address already in use|bind:|listen tcp .*:.*:.*", text):
        return "PORT_CONFLICT"
    if re.search(r"\b(?:401|403)\b|unauthori[sz]ed|invalid (?:api )?(?:key|token)|authentication failed|permission denied|forbidden", text):
        return "AUTHORIZATION"
    if re.search(r"certificate|x509|tls handshake|unknown authority", text):
        return "TLS"
    if re.search(r"proxyconnect|proxy connection|proxy error|connect tunnel", text):
        return "PROXY"
    if re.search(r"no such host|dns lookup|name resolution", text):
        return "DNS"
    if re.search(r"timeout|timed out|deadline exceeded|connection reset|connection refused|network is unreachable", text):
        return "NETWORK"
    return "UNKNOWN"


def diagnostic_lines(log_text: str) -> str:
    """Only retain severity-tagged diagnostics; skip informational TLS inventories."""
    return "\n".join(
        line for line in log_text.splitlines()
        if re.search(r"\b(?:ERROR|WARN|FATAL|PANIC)\b", line, flags=re.I)
    )


def log_tail(path: Path, size: int = 32768) -> str:
    try:
        with path.open("rb") as file:
            file.seek(0, 2)
            file.seek(max(0, file.tell() - size))
            return file.read().decode("utf-8", "replace")
    except OSError:
        return ""


def retry_delay(failures: int, reason: str) -> int:
    # No tight crash loops; rejected credentials require intervention.
    return 300 if reason == "AUTHORIZATION" else min(300, 2 ** min(failures, 8))


def update_status(root: Path, **values) -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "tunnel_status.json"
    tmp = path.with_suffix(".tmp")
    data = {"timestamp": int(time.time()), **values}
    tmp.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def valid_adopted_pid(root: Path) -> int | None:
    path = root / "bridge.pid"
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if managed_role_identity(pid, root, "tunnel") else None


def sleep_interruptibly(seconds: float) -> None:
    until = time.monotonic() + seconds
    while not STOP and time.monotonic() < until:
        time.sleep(min(0.5, until - time.monotonic()))


def run(root: Path) -> int:
    secret_dir = root / "secrets"
    client = root / "bin/tunnel-client-runtime"
    key = secret_dir / "control_plane_api_key"
    tunnel_id_file = secret_dir / "tunnel_id"
    router_token = secret_dir / "router_token"
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    tunnel_log = logs / "tunnel.log"
    pid_file = root / "bridge.pid"

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    failures = 0
    update_status(root, state="STARTING", reason="NONE", attempts=0)

    while not STOP:
        adopted = valid_adopted_pid(root)
        if adopted is not None:
            update_status(root, state="CLIENT_RUNNING", reason="NONE", pid=adopted, adopted=True)
            while not STOP and managed_role_identity(adopted, root, "tunnel"):
                sleep_interruptibly(1)
            if STOP:
                break

        try:
            if not client.is_file() or not os.access(client, os.X_OK):
                raise RuntimeError("missing client")
            if not all(p.is_file() and p.stat().st_size for p in (key, tunnel_id_file, router_token)):
                raise RuntimeError("missing credential")
            tunnel_id = tunnel_id_file.read_text(encoding="utf-8").strip()
            if not tunnel_id.startswith("tunnel_"):
                raise RuntimeError("invalid tunnel identifier")
        except (OSError, RuntimeError):
            update_status(root, state="NEEDS_ATTENTION", reason="LOCAL_CONFIGURATION", attempts=failures)
            sleep_interruptibly(300)
            continue

        env = os.environ.copy()
        env["CONTROL_PLANE_TUNNEL_ID"] = tunnel_id
        env["HTTPS_PROXY"] = "http://127.0.0.1:8877"
        env["https_proxy"] = env["HTTPS_PROXY"]
        ca = os.environ.get("PREFIX", "/data/data/com.termux/files/usr") + "/etc/tls/cert.pem"
        env["CA_BUNDLE"] = ca
        env["SSL_CERT_FILE"] = ca

        argv = [
            str(client), "run",
            # Avoid conflict with applications using the default 8080 health port.
            "--health.listen-addr", "127.0.0.1:0",
            "--control-plane.api-key", f"file:{key}",
            "--mcp.server-url", "http://127.0.0.1:8765/mcp",
            "--mcp.extra-headers", f"X-Bridge-Token: file:{router_token}",
        ]
        launched_at = time.monotonic()
        process = None
        try:
            with tunnel_log.open("w", encoding="utf-8") as output:
                tunnel_log.chmod(0o600)
                process = subprocess.Popen(argv, env=env, cwd=root, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                pid_file.write_text(f"{process.pid}\n", encoding="ascii")
                pid_file.chmod(0o600)
                update_status(root, state="CLIENT_RUNNING", reason="NONE", pid=process.pid,
                              adopted=False, attempts=failures)
                while not STOP and process.poll() is None:
                    sleep_interruptibly(0.5)
                if STOP and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        # Never SIGKILL: leave process for explicit investigation.
                        update_status(root, state="NEEDS_ATTENTION", reason="SHUTDOWN_TIMEOUT")
                        return 3
                return_code = process.wait()
        except OSError:
            return_code = -1

        if pid_file.is_file():
            try:
                if process is not None and int(pid_file.read_text().strip()) == process.pid:
                    pid_file.unlink()
            except (OSError, ValueError, UnboundLocalError):
                pass
        if STOP:
            break

        # An extended healthy session resets the exponential backoff.
        if time.monotonic() - launched_at >= 120:
            failures = 0
        failures += 1
        # Only diagnostic severity lines are classified: INFO TLS trust
        # inventories may contain the word "certificate" without any error.
        diagnostic = diagnostic_lines(log_tail(tunnel_log))
        reason = classify_error(diagnostic)
        delay = retry_delay(failures, reason)
        update_status(root, state="RETRYING" if reason != "AUTHORIZATION" else "NEEDS_ATTENTION",
                      reason=reason, exit_code=return_code,
                      attempts=failures, retry_seconds=delay)
        sleep_interruptibly(delay)

    update_status(root, state="STOPPED", reason="NONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: tunnel_watchdog.py ROOT")
    raise SystemExit(run(Path(sys.argv[1]).expanduser().resolve()))
