#!/usr/bin/env python3
"""Transactional external addon hot reload; never restarts the tunnel.
Author: @dvkrum.  Management-only, ordinary Termux UID.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(os.environ.get("TERMUXBRIDGE_ROOT", str(Path.home() / "termux-mcp-bridge")))
STATE = ROOT / "state"
ROUTE = STATE / "route.json"
JOURNAL = STATE / "addon_reload.json"
BACKEND_KEY = ROOT / "secrets" / "backend_token"
ROUTER_KEY = ROOT / "secrets" / "router_token"
ROUTER_PORT = 8765


def atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".addon-reload-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fp:
            json.dump(data, fp, separators=(",", ":"))
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def journal(phase: str, **kwargs) -> None:
    previous = {}
    try:
        previous = json.loads(JOURNAL.read_text())
    except (OSError, ValueError):
        pass
    # Never store credentials or arbitrary exceptions.
    atomic(JOURNAL, {**previous, "phase": phase, **kwargs, "updated_at": int(time.time())})


def identity(pid: int, port: int, release: Path, *, expected_ticks: int | None = None) -> int:
    proc = Path("/proc") / str(pid)
    if proc.stat().st_uid != os.getuid():
        raise RuntimeError("backend UID mismatch")
    cmd = [x.decode(errors="replace") for x in (proc / "cmdline").read_bytes().split(b"\0") if x]
    if len(cmd) != 4 or Path(cmd[1]).resolve() != (release / "bridge_server.py").resolve() or cmd[2:] != ["--http", str(port)]:
        raise RuntimeError("backend process identity mismatch")
    stats = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
    if stats[0] == "Z":
        raise RuntimeError("backend already exited")
    ticks = int(stats[19])
    if expected_ticks is not None and ticks != expected_ticks:
        raise RuntimeError("backend PID was recycled")
    return ticks


def current_route() -> dict:
    data = json.loads(ROUTE.read_text())
    if not isinstance(data.get("port"), int) or data["port"] not in (18771, 18772):
        raise RuntimeError("unsupported active backend port")
    generation = (ROOT / "source_commit").read_text().strip()
    if data.get("generation") != generation or not generation or len(generation) != 40:
        raise RuntimeError("core generation/route mismatch")
    if (ROOT / "current").resolve() != (ROOT / "releases" / generation).resolve():
        raise RuntimeError("active core release pointer mismatch")
    return data


def request(port: int, key: Path, header: str, path: str, payload: dict | None = None):
    secret = key.read_text().strip()
    if len(secret) < 32:
        raise RuntimeError("local authentication not initialized")
    headers = {header: secret}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    else:
        body = None
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=7) as res:
        return json.load(res)


def rpc(port: int, key: Path, header: str, method: str, params=None):
    msg = {"jsonrpc": "2.0", "id": 240, "method": method}
    if params is not None:
        msg["params"] = params
    response = request(port, key, header, "/mcp", msg)
    if "error" in response:
        raise RuntimeError("MCP protocol failed")
    result = response["result"]
    if result.get("isError"):
        raise RuntimeError("MCP tool failed")
    return result


def addon_status(port, key, header):
    response = rpc(port, key, header, "tools/call", {"name": "addon_status", "arguments": {}})
    info = json.loads(next(c["text"] for c in response["content"] if c["type"] == "text"))
    return info["addons"]


def verify_contract(before: list[dict], after: list[dict], old_addons: dict, new_addons: dict) -> list[str]:
    prev = {t["name"]: t for t in before}
    nxt = {t["name"]: t for t in after}
    old_ns = tuple(x["namespace"] for x in old_addons.values() if x.get("namespace"))
    new_ns = tuple(x["namespace"] for x in new_addons.values() if x.get("namespace"))
    def is_external(name):
        return name.startswith(old_ns + new_ns)
    for name, descriptor in prev.items():
        if not is_external(name) and nxt.get(name) != descriptor:
            raise RuntimeError("core MCP tool contract changed")
    for name in nxt:
        if name not in prev and not is_external(name):
            raise RuntimeError("unexpected new core MCP tool")
    for name, detail in old_addons.items():
        if name not in new_addons or new_addons[name].get("namespace") != detail.get("namespace"):
            raise RuntimeError("installed addon namespace removed/changed")
    if any(x.get("status") != "READY" for x in new_addons.values()):
        raise RuntimeError("candidate addon is not ready")
    return [name for name, detail in new_addons.items()
            if (name not in old_addons or detail.get("version") != old_addons[name].get("version"))]


def _port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _is_descendant(child: int, ancestor: int) -> bool:
    seen = set()
    while child > 1 and child not in seen:
        if child == ancestor:
            return True
        seen.add(child)
        try:
            child = int((Path("/proc") / str(child) / "stat").read_text().rsplit(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return False


def drain_count(generation, port):
    status = request(ROUTER_PORT, ROUTER_KEY, "X-Bridge-Token", "/__bridge/status")
    key = f"{generation}:{port}"
    exact = status.get("inflight_by_backend")
    if not isinstance(exact, dict):
        raise RuntimeError("supervisor needs per-backend inflight accounting")
    return int(exact.get(key, 0))


def retire(pid: int, port: int, release: Path, ticks: int, generation: str, timeout=140):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            identity(pid, port, release, expected_ticks=ticks)
            if drain_count(generation, port) == 0:
                # Prevent races with still-arriving requests routed before switch.
                time.sleep(2)
                if drain_count(generation, port) != 0:
                    continue
                os.kill(pid, signal.SIGTERM)
                journal("COMMITTED", old_backend_retired=True)
                return True
        except (OSError, RuntimeError):
            return False
        time.sleep(.5)
    journal("COMMITTED", old_backend_retired=False, retirement_timeout=True)
    return False


def recover_interrupted() -> None:
    """Fail closed and restore the old route after an interrupted switch."""
    try:
        data = json.loads(JOURNAL.read_text())
    except (OSError, ValueError):
        return
    phase = data.get("phase")
    if phase not in {"SWITCHED", "CANDIDATE_HEALTHY", "CANDIDATE_STARTING"}:
        return
    original = json.loads(ROUTE.read_text())
    old_port = data.get("old_port")
    candidate_port = data.get("new_port")
    old_pid = data.get("old_pid")
    generation = data.get("generation")
    if not (isinstance(old_port, int) and old_port in (18771, 18772)
            and isinstance(candidate_port, int) and candidate_port in (18771, 18772)
            and old_port != candidate_port and isinstance(old_pid, int)
            and isinstance(generation, str) and len(generation) == 40
            and original.get("generation") == generation):
        raise RuntimeError("interrupted addon reload journal not recoverable")
    release = (ROOT / "releases" / generation).resolve()
    identity(old_pid, old_port, release)
    if original["port"] == candidate_port:
        atomic(ROUTE, {**original, "port": old_port})
    elif original["port"] != old_port:
        raise RuntimeError("interrupted reload route mismatch")
    candidate_pid, ticks = data.get("new_pid"), data.get("new_start_ticks")
    if isinstance(candidate_pid, int) and isinstance(ticks, int):
        try:
            identity(candidate_pid, candidate_port, release, expected_ticks=ticks)
        except (OSError, RuntimeError):
            pass
        else:
            os.kill(candidate_pid, signal.SIGTERM)
    journal("FAILED_ROLLED_BACK", interrupted=True)


def run() -> int:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / ".update.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ADDON_RELOAD_LOCKED")
            return 75
        recover_interrupted()
        route = current_route()
        generation, old_port = route["generation"], route["port"]
        old_pid = int((ROOT / "server.pid").read_text())
        release = (ROOT / "current").resolve()
        old_ticks = identity(old_pid, old_port, release)
        # Fail closed if the long-lived supervisor is too old for safe same-generation drain.
        drain_count(generation, old_port)
        older = addon_status(ROUTER_PORT, ROUTER_KEY, "X-Bridge-Token")
        tools_before = rpc(ROUTER_PORT, ROUTER_KEY, "X-Bridge-Token", "tools/list")["tools"]
        # Compare against immutable on-disk release manifests.
        if not any(
            (ROOT.parent / ".termux-mcp-bridge" / "addons" / name / "current" / "addon.json").is_file()
            and json.loads((ROOT.parent / ".termux-mcp-bridge" / "addons" / name / "current" / "addon.json").read_text()).get("version") != detail.get("version")
            for name, detail in older.items()
        ):
            print("ADDON_RELOAD_ALREADY_CURRENT")
            return 0
        new_port = 18771 if old_port == 18772 else 18772
        if not _port_available(new_port):
            raise RuntimeError("inactive port occupied; existing backend retained")
        journal("CANDIDATE_STARTING", generation=generation, old_port=old_port, new_port=new_port,
                old_pid=old_pid, new_pid=None, new_start_ticks=None,
                changed_addons=[], old_backend_retired=False, retirement_timeout=False)
        logfile = ROOT / "logs" / "addon-reload.log"
        logfile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(logfile, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        log = os.fdopen(fd, "ab", buffering=0)
        env = dict(os.environ, TERMUX_BRIDGE_ROOT=str(Path.home()),
                   TERMUX_BRIDGE_JOBS=str(ROOT / "jobs"),
                   TERMUX_BRIDGE_BACKEND_TOKEN_FILE=str(BACKEND_KEY))
        try:
            process = subprocess.Popen([sys.executable, str(release / "bridge_server.py"), "--http", str(new_port)],
                                       env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
        finally:
            log.close()
        switched = False
        try:
            end = time.monotonic() + 20
            while time.monotonic() < end:
                if process.poll() is not None:
                    raise RuntimeError("candidate exited early")
                try:
                    health = request(new_port, BACKEND_KEY, "X-Bridge-Backend-Token", "/healthz")
                    if health.get("ok") and health.get("pid") == process.pid:
                        break
                except (OSError, ValueError):
                    time.sleep(.3)
            else:
                raise RuntimeError("candidate health timeout")
            new_ticks = identity(process.pid, new_port, release)
            candidate_addons = addon_status(new_port, BACKEND_KEY, "X-Bridge-Backend-Token")
            tools_after = rpc(new_port, BACKEND_KEY, "X-Bridge-Backend-Token", "tools/list")["tools"]
            changed = verify_contract(tools_before, tools_after, older, candidate_addons)
            if not changed:
                print("ADDON_RELOAD_NOT_REQUIRED")
                return 0
            journal("CANDIDATE_HEALTHY", changed_addons=changed, new_pid=process.pid,
                    new_start_ticks=new_ticks)
            atomic(ROUTE, {**route, "port": new_port})
            switched = True
            journal("SWITCHED")
            routed = addon_status(ROUTER_PORT, ROUTER_KEY, "X-Bridge-Token")
            if any(routed[name]["version"] != candidate_addons[name]["version"] for name in changed):
                raise RuntimeError("routed addon version mismatch")
            time.sleep(2)
            identity(process.pid, new_port, release, expected_ticks=new_ticks)
            request(new_port, BACKEND_KEY, "X-Bridge-Backend-Token", "/healthz")
            # Persist the active PID before retiring the old backend.
            tmp = ROOT / ".addon-reload-server.pid"
            tmp.write_text(str(process.pid) + "\n")
            tmp.chmod(0o600)
            os.replace(tmp, ROOT / "server.pid")
            journal("COMMITTED", old_backend_retired=False)
            # This command can itself be executed through the old MCP backend.
            # Do not retire that process before its response has returned.
            if _is_descendant(os.getppid(), old_pid):
                child = subprocess.Popen([sys.executable, __file__, "--retire",
                     str(old_pid), str(old_port), str(old_ticks), generation],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     env=os.environ.copy())
                print("ADDON_RELOAD_VERIFIED", ",".join(changed), "deferred_drain_pid", child.pid)
            else:
                drained = retire(old_pid, old_port, release, old_ticks, generation)
                print("ADDON_RELOAD_VERIFIED", ",".join(changed),
                      "old_retired", drained)
            return 0
        except Exception:
            if switched:
                # Never lose the known healthy original backend on failure.
                atomic(ROUTE, route)
                tmp = ROOT / ".addon-reload-revert.pid"
                tmp.write_text(str(old_pid) + "\n")
                tmp.chmod(0o600)
                os.replace(tmp, ROOT / "server.pid")
            journal("FAILED_ROLLED_BACK" if switched else "FAILED_PRE_SWITCH")
            if process.poll() is None:
                process.terminate()
            raise
        finally:
            if not switched and process.poll() is None:
                process.terminate()


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--status":
        try:
            data = json.loads(JOURNAL.read_text())
            print(json.dumps({k:data.get(k) for k in ("phase", "generation", "changed_addons", "old_backend_retired",
                        "retirement_timeout", "updated_at")}))
        except (OSError, ValueError):
            print("ADDON_RELOAD_NOT_STARTED")
        return 0
    if len(sys.argv) == 6 and sys.argv[1] == "--retire":
        generation = sys.argv[5]
        if len(generation) != 40 or not all(c in "0123456789abcdef" for c in generation):
            return 2
        return 0 if retire(int(sys.argv[2]), int(sys.argv[3]),
                          ROOT / "releases" / generation, int(sys.argv[4]), generation) else 1
    if len(sys.argv) != 1:
        return 2
    try:
        return run()
    except Exception as exc:
        # Sanitized failure code only; do not expose paths or credentials.
        print("ADDON_RELOAD_FAILED", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
