#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

BRIDGE_DIR="$HOME/termux-mcp-bridge"
CLIENT="$BRIDGE_DIR/bin/tunnel-client-runtime"
SHOW_LOGS=0
if [[ "${1:-}" == "--logs" ]]; then
  SHOW_LOGS=1
fi

if [[ ! -x "$CLIENT" ]]; then
  echo "NOT INSTALLED: tunnel-client-runtime is missing"
  exit 1
fi

if [[ -f "$BRIDGE_DIR/bridge.pid" ]] && kill -0 "$(cat "$BRIDGE_DIR/bridge.pid")" 2>/dev/null; then
  echo "TUNNEL RUNNING PID $(cat "$BRIDGE_DIR/bridge.pid")"
else
  echo "TUNNEL STOPPED"
fi

if [[ -f "$BRIDGE_DIR/server.pid" ]] && kill -0 "$(cat "$BRIDGE_DIR/server.pid")" 2>/dev/null; then
  echo "MCP RUNNING PID $(cat "$BRIDGE_DIR/server.pid")"
else
  echo "MCP STOPPED"
fi

if [[ -f "$BRIDGE_DIR/proxy.pid" ]] && kill -0 "$(cat "$BRIDGE_DIR/proxy.pid")" 2>/dev/null; then
  echo "DNS PROXY RUNNING PID $(cat "$BRIDGE_DIR/proxy.pid")"
else
  echo "DNS PROXY STOPPED"
fi

python - <<'PY'
import socket
try:
    with socket.create_connection(('127.0.0.1', 8877), timeout=2):
        pass
    print('DNS PROXY HEALTH reachable')
except Exception as e:
    print('DNS PROXY HEALTH unavailable:', type(e).__name__)
PY

python - <<'PY'
import json, urllib.request
try:
    print('MCP HEALTH', json.load(urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)))
except Exception as e:
    print('MCP HEALTH unavailable:', type(e).__name__)
PY

if [[ "$SHOW_LOGS" -eq 1 ]]; then
  echo "== redacted tunnel log =="
  tail -n 40 "$BRIDGE_DIR/logs/tunnel.log" 2>/dev/null \
    | sed -E 's/tunnel_[A-Za-z0-9_-]+/tunnel_REDACTED/g; s/(api[_ -]?key[=: ]+)[^ ]+/\1REDACTED/Ig'
fi
