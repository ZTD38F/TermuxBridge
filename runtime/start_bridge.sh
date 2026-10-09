#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
umask 077

BRIDGE_DIR="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
PIDFILE="$BRIDGE_DIR/bridge.pid"
SERVER_PIDFILE="$BRIDGE_DIR/server.pid"
SUPERVISOR_PIDFILE="$BRIDGE_DIR/supervisor.pid"
PROXY_PIDFILE="$BRIDGE_DIR/proxy.pid"
WATCHDOG_PIDFILE="$BRIDGE_DIR/watchdog.pid"
KEYFILE="$BRIDGE_DIR/secrets/control_plane_api_key"
TUNNEL_FILE="$BRIDGE_DIR/secrets/tunnel_id"
ROUTER_TOKEN_FILE="$BRIDGE_DIR/secrets/router_token"
BACKEND_TOKEN_FILE="$BRIDGE_DIR/secrets/backend_token"
CLIENT="$BRIDGE_DIR/bin/tunnel-client-runtime"
LOCKFILE="$BRIDGE_DIR/.start.v2.lock"
# v2 lock file bypasses a legacy lock inherited by long-lived daemons.
STATE_DIR="$BRIDGE_DIR/state"
ROUTE_FILE="$STATE_DIR/route.json"
CURRENT_LINK="$BRIDGE_DIR/current"

mkdir -p "$BRIDGE_DIR/logs" "$BRIDGE_DIR/jobs" "$BRIDGE_DIR/secrets" "$STATE_DIR" "$BRIDGE_DIR/releases"
chmod 700 "$BRIDGE_DIR/secrets" "$STATE_DIR" "$BRIDGE_DIR/releases"

ensure_secret() {
  local path="$1"
  if [[ ! -s "$path" ]]; then
    python - "$path" <<'PY'
from pathlib import Path
import secrets, sys
p=Path(sys.argv[1])
p.write_text(secrets.token_hex(32)+"\n", encoding="utf-8")
p.chmod(0o600)
PY
  fi
  chmod 600 "$path"
}

ensure_secret "$ROUTER_TOKEN_FILE"
ensure_secret "$BACKEND_TOKEN_FILE"
chmod 600 "$KEYFILE" "$TUNNEL_FILE"
[[ -x "$CLIENT" ]] || { echo "ERROR: tunnel-client-runtime is missing. Re-run install_termux.sh"; exit 1; }
[[ -s "$KEYFILE" ]] || { echo "ERROR: API key is missing. Re-run install_termux.sh"; exit 1; }
[[ -s "$TUNNEL_FILE" ]] || { echo "ERROR: tunnel ID is missing. Re-run install_termux.sh"; exit 1; }

# Legacy updaters do not know supervisor.py yet. During the one-time migration
# only, recover that file from the same immutable commit already selected by the
# legacy updater. Future generations carry/manage it normally.
if [[ ! -f "$BRIDGE_DIR/supervisor.py" ]]; then
  SOURCE_COMMIT="$(cat "$BRIDGE_DIR/source_commit" 2>/dev/null || true)"
  [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "ERROR: supervisor missing and source commit is invalid"; exit 1; }
  SUPERVISOR_TMP="$BRIDGE_DIR/.supervisor-migration.tmp"
  curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60     "https://raw.githubusercontent.com/ZTD38F/TermuxBridge/$SOURCE_COMMIT/runtime/supervisor.py"     -o "$SUPERVISOR_TMP"
  python -m py_compile "$SUPERVISOR_TMP"
  mv "$SUPERVISOR_TMP" "$BRIDGE_DIR/supervisor.py"
  chmod 600 "$BRIDGE_DIR/supervisor.py"
fi

exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "Bridge start is already in progress; retry after the current startup finishes" >&2
  exit 75
fi

pid_alive() {
  local file="$1"
  if [[ -s "$file" ]]; then
    local pid
    pid="$(<"$file")"
    [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] && kill -0 "$pid" 2>/dev/null
  else
    return 1
  fi
}

proxy_healthy() {
  python - <<'PY' >/dev/null 2>&1
import socket
with socket.create_connection(("127.0.0.1", 8877), timeout=2):
    pass
PY
}

backend_health() {
  local port="$1"
  python - "$port" "$BACKEND_TOKEN_FILE" <<'PY' >/dev/null 2>&1
import json, pathlib, sys, urllib.request
port=int(sys.argv[1]); token=pathlib.Path(sys.argv[2]).read_text().strip()
req=urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={"X-Bridge-Backend-Token":token})
with urllib.request.urlopen(req, timeout=2) as response:
    payload=json.load(response)
if payload.get("ok") is not True: raise SystemExit(1)
PY
}

router_health() {
  python - "$ROUTER_TOKEN_FILE" <<'PY' >/dev/null 2>&1
import json, pathlib, sys, urllib.request
token=pathlib.Path(sys.argv[1]).read_text().strip()
req=urllib.request.Request("http://127.0.0.1:8765/__bridge/healthz", headers={"X-Bridge-Token":token})
with urllib.request.urlopen(req, timeout=2) as response:
    payload=json.load(response)
if payload.get("ok") is not True: raise SystemExit(1)
PY
}

write_route() {
  local generation="$1" port="$2"
  python - "$ROUTE_FILE" "$generation" "$port" <<'PY'
import json, os, pathlib, sys
path=pathlib.Path(sys.argv[1]); generation=sys.argv[2]; port=int(sys.argv[3])
tmp=path.with_suffix(".tmp")
tmp.write_text(json.dumps({"generation":generation,"port":port}, separators=(",",":"))+"\n", encoding="utf-8")
os.replace(tmp, path)
path.chmod(0o600)
PY
}

if [[ -L "$CURRENT_LINK" && -f "$CURRENT_LINK/bridge_server.py" ]]; then
  CURRENT_DIR="$(readlink -f "$CURRENT_LINK")"
else
  CURRENT_DIR="$BRIDGE_DIR"
fi

CURRENT_GENERATION="$(cat "$BRIDGE_DIR/source_commit" 2>/dev/null || echo legacy)"
CURRENT_PORT=18771
if [[ -s "$ROUTE_FILE" ]]; then
  read -r CURRENT_GENERATION CURRENT_PORT < <(python - "$ROUTE_FILE" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print(d.get("generation","legacy"), int(d.get("port",18771)))
PY
)
else
  write_route "$CURRENT_GENERATION" "$CURRENT_PORT"
fi

start_tunnel_watchdog() {
  [[ -f "$BRIDGE_DIR/tunnel_watchdog.py" ]] || {
    echo "ERROR: tunnel watchdog missing" >&2; return 1;
  }
  if pid_alive "$WATCHDOG_PIDFILE" &&
    python "$BRIDGE_DIR/recover_bridge_port.py" --root "$BRIDGE_DIR" --check-pid "$(<"$WATCHDOG_PIDFILE")" --role watchdog; then
    echo "Tunnel watchdog active (PID $(<"$WATCHDOG_PIDFILE")); remote connectivity not yet confirmed."
    return 0
  fi
  nohup python "$BRIDGE_DIR/tunnel_watchdog.py" "$BRIDGE_DIR"     >"$BRIDGE_DIR/logs/tunnel-watchdog.log" 2>&1 9>&- &
  printf '%s\n' "$!" >"$WATCHDOG_PIDFILE"
  chmod 600 "$WATCHDOG_PIDFILE"
  sleep 1
  if ! pid_alive "$WATCHDOG_PIDFILE"; then
    echo "ERROR: tunnel watchdog failed; inspect tunnel-watchdog.log" >&2
    return 1
  fi
  echo "Tunnel watchdog started (PID $(<"$WATCHDOG_PIDFILE")); reconnecting automatically."
}

# A tunnel failure MUST NOT restart a healthy authenticated MCP stack.
if pid_alive "$SERVER_PIDFILE" && pid_alive "$SUPERVISOR_PIDFILE" && pid_alive "$PROXY_PIDFILE" &&
   proxy_healthy && backend_health "$CURRENT_PORT" && router_health; then
  start_tunnel_watchdog
  exit $?
fi

# Startup/reboot recovery may restart the complete local stack. Normal runtime
# updates never call this path; they switch backends behind the live supervisor.
"$BRIDGE_DIR/stop_bridge.sh" >/dev/null 2>&1 || true
rm -f "$PIDFILE" "$SERVER_PIDFILE" "$SUPERVISOR_PIDFILE" "$PROXY_PIDFILE"

# Recover only our own identifiable stale listeners. Authentication failures
# on a legacy backend must not be mistaken for an empty TCP port; equally,
# an unknown process must never be killed just to make startup succeed.
# This applies to manual/reboot full startup, NEVER seamless runtime updates.
[[ -f "$BRIDGE_DIR/recover_bridge_port.py" ]] || {
  echo "ERROR: recovery helper missing; refusing unsafe restart" >&2
  exit 1
}
python "$BRIDGE_DIR/recover_bridge_port.py" --root "$BRIDGE_DIR" --port 8765 --kind mcp
python "$BRIDGE_DIR/recover_bridge_port.py" --root "$BRIDGE_DIR" --port 8877 --kind proxy

cleanup_partial() {
  "$BRIDGE_DIR/stop_bridge.sh" >/dev/null 2>&1 || true
}
trap cleanup_partial ERR INT TERM

export TERMUX_BRIDGE_ROOT="$HOME"
export TERMUX_BRIDGE_JOBS="$BRIDGE_DIR/jobs"
export TERMUX_BRIDGE_BACKEND_TOKEN_FILE="$BACKEND_TOKEN_FILE"

nohup python "$BRIDGE_DIR/local_https_proxy.py" >"$BRIDGE_DIR/logs/proxy.log" 2>&1 9>&- &
echo $! >"$PROXY_PIDFILE"
chmod 600 "$PROXY_PIDFILE"
for _ in {1..20}; do
  pid_alive "$PROXY_PIDFILE" && proxy_healthy && break
  sleep 0.2
done
pid_alive "$PROXY_PIDFILE" && proxy_healthy || { echo "ERROR: local HTTPS proxy failed"; exit 1; }

nohup python "$CURRENT_DIR/bridge_server.py" --http "$CURRENT_PORT" >"$BRIDGE_DIR/logs/server-$CURRENT_GENERATION.log" 2>&1 9>&- &
echo $! >"$SERVER_PIDFILE"
chmod 600 "$SERVER_PIDFILE"
for _ in {1..20}; do
  pid_alive "$SERVER_PIDFILE" && backend_health "$CURRENT_PORT" && break
  sleep 0.25
done
pid_alive "$SERVER_PIDFILE" && backend_health "$CURRENT_PORT" || { echo "ERROR: MCP backend failed"; exit 1; }

nohup env TERMUXBRIDGE_ROOT="$BRIDGE_DIR" python "$BRIDGE_DIR/supervisor.py" >"$BRIDGE_DIR/logs/supervisor.log" 2>&1 9>&- &
echo $! >"$SUPERVISOR_PIDFILE"
chmod 600 "$SUPERVISOR_PIDFILE"
for _ in {1..20}; do
  pid_alive "$SUPERVISOR_PIDFILE" && router_health && break
  sleep 0.25
done
pid_alive "$SUPERVISOR_PIDFILE" && router_health || { echo "ERROR: bridge supervisor failed"; exit 1; }

if ! start_tunnel_watchdog; then
  # Failure to launch the remote transport must not tear down a working MCP.
  trap - ERR INT TERM
  echo "WARNING: local Bridge is healthy but tunnel recovery did not start." >&2
  exit 4
fi
trap - ERR INT TERM
echo "Local Bridge ready: supervisor PID $(<"$SUPERVISOR_PIDFILE"), MCP PID $(<"$SERVER_PIDFILE"), proxy PID $(<"$PROXY_PIDFILE"). Tunnel managed separately."
