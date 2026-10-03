#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

BRIDGE_DIR="$HOME/termux-mcp-bridge"
PIDFILE="$BRIDGE_DIR/bridge.pid"
SERVER_PIDFILE="$BRIDGE_DIR/server.pid"
PROXY_PIDFILE="$BRIDGE_DIR/proxy.pid"
KEYFILE="$BRIDGE_DIR/secrets/control_plane_api_key"
TUNNEL_FILE="$BRIDGE_DIR/secrets/tunnel_id"
CLIENT="$BRIDGE_DIR/bin/tunnel-client-runtime"
LOCKFILE="$BRIDGE_DIR/.start.lock"

mkdir -p "$BRIDGE_DIR/logs" "$BRIDGE_DIR/jobs"
chmod 700 "$BRIDGE_DIR/secrets"
chmod 600 "$KEYFILE" "$TUNNEL_FILE"
[[ -x "$CLIENT" ]] || { echo "ERROR: tunnel-client-runtime is missing. Re-run install_termux.sh"; exit 1; }
[[ -s "$KEYFILE" ]] || { echo "ERROR: API key is missing. Re-run install_termux.sh"; exit 1; }
[[ -s "$TUNNEL_FILE" ]] || { echo "ERROR: tunnel ID is missing. Re-run install_termux.sh"; exit 1; }

exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "Bridge start is already in progress"
  exit 0
fi

pid_alive() {
  local file="$1"
  [[ -s "$file" ]] && kill -0 "$(<"$file")" 2>/dev/null
}

mcp_healthy() {
  python - <<'PY' >/dev/null 2>&1
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=2) as response:
    payload = json.load(response)
if payload.get("ok") is not True:
    raise SystemExit(1)
PY
}

proxy_healthy() {
  python - <<'PY' >/dev/null 2>&1
import socket

with socket.create_connection(("127.0.0.1", 8877), timeout=2):
    pass
PY
}

if pid_alive "$PIDFILE" && pid_alive "$SERVER_PIDFILE" && pid_alive "$PROXY_PIDFILE" && proxy_healthy && mcp_healthy; then
  echo "Already healthy: tunnel PID $(<"$PIDFILE"), MCP PID $(<"$SERVER_PIDFILE"), proxy PID $(<"$PROXY_PIDFILE")"
  exit 0
fi

# Recover partial/stale state as one unit to avoid duplicate listeners.
"$BRIDGE_DIR/stop_bridge.sh" >/dev/null 2>&1 || true
rm -f "$PIDFILE" "$SERVER_PIDFILE" "$PROXY_PIDFILE"

cleanup_partial() {
  "$BRIDGE_DIR/stop_bridge.sh" >/dev/null 2>&1 || true
}
trap cleanup_partial ERR INT TERM

export TERMUX_BRIDGE_ROOT="$HOME"
export TERMUX_BRIDGE_JOBS="$BRIDGE_DIR/jobs"
TUNNEL_ID="$(<"$TUNNEL_FILE")"
[[ "$TUNNEL_ID" == tunnel_* ]] || { echo "ERROR: invalid tunnel ID"; exit 1; }
export CONTROL_PLANE_TUNNEL_ID="$TUNNEL_ID"

nohup python "$BRIDGE_DIR/local_https_proxy.py" >"$BRIDGE_DIR/logs/proxy.log" 2>&1 &
echo $! >"$PROXY_PIDFILE"
chmod 600 "$PROXY_PIDFILE"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if pid_alive "$PROXY_PIDFILE" && proxy_healthy; then
    break
  fi
  sleep 0.2
done
pid_alive "$PROXY_PIDFILE" && proxy_healthy || { echo "ERROR: local HTTPS proxy failed; inspect its redacted log locally"; exit 1; }

nohup python "$BRIDGE_DIR/bridge_server.py" --http 8765 >"$BRIDGE_DIR/logs/server.log" 2>&1 &
echo $! >"$SERVER_PIDFILE"
chmod 600 "$SERVER_PIDFILE"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if pid_alive "$SERVER_PIDFILE" && mcp_healthy; then
    break
  fi
  sleep 0.5
done
pid_alive "$SERVER_PIDFILE" && mcp_healthy || { echo "ERROR: local MCP server failed; inspect its redacted log locally"; exit 1; }

export HTTPS_PROXY="http://127.0.0.1:8877"
export https_proxy="$HTTPS_PROXY"
export CA_BUNDLE="$PREFIX/etc/tls/cert.pem"
export SSL_CERT_FILE="$CA_BUNDLE"
nohup "$CLIENT" run --control-plane.api-key "file:$KEYFILE" --mcp.server-url http://127.0.0.1:8765/mcp >"$BRIDGE_DIR/logs/tunnel.log" 2>&1 &
echo $! >"$PIDFILE"
chmod 600 "$PIDFILE"
sleep 2
pid_alive "$PIDFILE" || { echo "ERROR: tunnel client failed; run status_bridge.sh --logs for redacted diagnostics"; exit 1; }

trap - ERR INT TERM
echo "Started: tunnel PID $(<"$PIDFILE"), MCP PID $(<"$SERVER_PIDFILE"), proxy PID $(<"$PROXY_PIDFILE")"
