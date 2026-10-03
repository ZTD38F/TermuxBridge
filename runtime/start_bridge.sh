#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
umask 077

BRIDGE_DIR="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
PIDFILE="$BRIDGE_DIR/bridge.pid"
SERVER_PIDFILE="$BRIDGE_DIR/server.pid"
SUPERVISOR_PIDFILE="$BRIDGE_DIR/supervisor.pid"
PROXY_PIDFILE="$BRIDGE_DIR/proxy.pid"
KEYFILE="$BRIDGE_DIR/secrets/control_plane_api_key"
TUNNEL_FILE="$BRIDGE_DIR/secrets/tunnel_id"
ROUTER_TOKEN_FILE="$BRIDGE_DIR/secrets/router_token"
BACKEND_TOKEN_FILE="$BRIDGE_DIR/secrets/backend_token"
CLIENT="$BRIDGE_DIR/bin/tunnel-client-runtime"
LOCKFILE="$BRIDGE_DIR/.start.lock"
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
[[ -f "$BRIDGE_DIR/supervisor.py" ]] || { echo "ERROR: supervisor.py is missing; run termuxbridgectl update"; exit 1; }

exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "Bridge start is already in progress"
  exit 0
fi

pid_alive() {
  local file="$1"
  [[ -s "$file" ]] && kill -0 "$(<"$file")" 2>/dev/null
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

if pid_alive "$PIDFILE" && pid_alive "$SERVER_PIDFILE" && pid_alive "$SUPERVISOR_PIDFILE" && pid_alive "$PROXY_PIDFILE" &&
   proxy_healthy && backend_health "$CURRENT_PORT" && router_health; then
  echo "Already healthy: tunnel PID $(<"$PIDFILE"), supervisor PID $(<"$SUPERVISOR_PIDFILE"), MCP PID $(<"$SERVER_PIDFILE"), proxy PID $(<"$PROXY_PIDFILE")"
  exit 0
fi

# Startup/reboot recovery may restart the complete local stack. Normal runtime
# updates never call this path; they switch backends behind the live supervisor.
"$BRIDGE_DIR/stop_bridge.sh" >/dev/null 2>&1 || true
rm -f "$PIDFILE" "$SERVER_PIDFILE" "$SUPERVISOR_PIDFILE" "$PROXY_PIDFILE"

cleanup_partial() {
  "$BRIDGE_DIR/stop_bridge.sh" >/dev/null 2>&1 || true
}
trap cleanup_partial ERR INT TERM

export TERMUX_BRIDGE_ROOT="$HOME"
export TERMUX_BRIDGE_JOBS="$BRIDGE_DIR/jobs"
export TERMUX_BRIDGE_BACKEND_TOKEN_FILE="$BACKEND_TOKEN_FILE"

nohup python "$BRIDGE_DIR/local_https_proxy.py" >"$BRIDGE_DIR/logs/proxy.log" 2>&1 &
echo $! >"$PROXY_PIDFILE"
chmod 600 "$PROXY_PIDFILE"
for _ in {1..20}; do
  pid_alive "$PROXY_PIDFILE" && proxy_healthy && break
  sleep 0.2
done
pid_alive "$PROXY_PIDFILE" && proxy_healthy || { echo "ERROR: local HTTPS proxy failed"; exit 1; }

nohup python "$CURRENT_DIR/bridge_server.py" --http "$CURRENT_PORT" >"$BRIDGE_DIR/logs/server-$CURRENT_GENERATION.log" 2>&1 &
echo $! >"$SERVER_PIDFILE"
chmod 600 "$SERVER_PIDFILE"
for _ in {1..20}; do
  pid_alive "$SERVER_PIDFILE" && backend_health "$CURRENT_PORT" && break
  sleep 0.25
done
pid_alive "$SERVER_PIDFILE" && backend_health "$CURRENT_PORT" || { echo "ERROR: MCP backend failed"; exit 1; }

nohup env TERMUXBRIDGE_ROOT="$BRIDGE_DIR" python "$BRIDGE_DIR/supervisor.py" >"$BRIDGE_DIR/logs/supervisor.log" 2>&1 &
echo $! >"$SUPERVISOR_PIDFILE"
chmod 600 "$SUPERVISOR_PIDFILE"
for _ in {1..20}; do
  pid_alive "$SUPERVISOR_PIDFILE" && router_health && break
  sleep 0.25
done
pid_alive "$SUPERVISOR_PIDFILE" && router_health || { echo "ERROR: bridge supervisor failed"; exit 1; }

TUNNEL_ID="$(<"$TUNNEL_FILE")"
[[ "$TUNNEL_ID" == tunnel_* ]] || { echo "ERROR: invalid tunnel ID"; exit 1; }
export CONTROL_PLANE_TUNNEL_ID="$TUNNEL_ID"
export HTTPS_PROXY="http://127.0.0.1:8877"
export https_proxy="$HTTPS_PROXY"
export CA_BUNDLE="$PREFIX/etc/tls/cert.pem"
export SSL_CERT_FILE="$CA_BUNDLE"
nohup "$CLIENT" run   --control-plane.api-key "file:$KEYFILE"   --mcp.server-url http://127.0.0.1:8765/mcp   --mcp.extra-headers "X-Bridge-Token: file:$ROUTER_TOKEN_FILE"   >"$BRIDGE_DIR/logs/tunnel.log" 2>&1 &
echo $! >"$PIDFILE"
chmod 600 "$PIDFILE"
sleep 2
pid_alive "$PIDFILE" || { echo "ERROR: tunnel client failed; run status_bridge.sh --logs"; exit 1; }

trap - ERR INT TERM
echo "Started: tunnel PID $(<"$PIDFILE"), supervisor PID $(<"$SUPERVISOR_PIDFILE"), MCP PID $(<"$SERVER_PIDFILE"), proxy PID $(<"$PROXY_PIDFILE")"
