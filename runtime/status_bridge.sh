#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

BRIDGE_DIR="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
CLIENT="$BRIDGE_DIR/bin/tunnel-client-runtime"
SHOW_LOGS=0
[[ "${1:-}" == "--logs" ]] && SHOW_LOGS=1

pid_line() {
  local label="$1" file="$2"
  if [[ -s "$file" ]] && kill -0 "$(<"$file")" 2>/dev/null; then
    echo "$label RUNNING PID $(<"$file")"
  else
    echo "$label STOPPED"
  fi
}

[[ -x "$CLIENT" ]] || { echo "NOT INSTALLED: tunnel-client-runtime is missing"; exit 1; }
pid_line "TUNNEL WATCHDOG" "$BRIDGE_DIR/watchdog.pid"
pid_line "TUNNEL" "$BRIDGE_DIR/bridge.pid"
pid_line "SUPERVISOR" "$BRIDGE_DIR/supervisor.pid"
pid_line "MCP" "$BRIDGE_DIR/server.pid"
pid_line "DNS PROXY" "$BRIDGE_DIR/proxy.pid"

if [[ -s "$BRIDGE_DIR/state/route.json" ]]; then
  python - "$BRIDGE_DIR/state/route.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("ACTIVE GENERATION", d.get("generation","unknown"))
print("ACTIVE BACKEND PORT", d.get("port","unknown"))
PY
fi

if [[ -s "$BRIDGE_DIR/state/update.json" ]]; then
  python - "$BRIDGE_DIR/state/update.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
for k in ("transaction_id","update_kind","phase","current_generation","candidate_generation","previous_generation","failure_reason","rollback_reason","updated_at"):
    if d.get(k) not in (None,""):
        print("UPDATE", k, d[k])
PY
fi

if [[ -s "$BRIDGE_DIR/state/tunnel_status.json" ]]; then
  python - "$BRIDGE_DIR/state/tunnel_status.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    for key in ("state","reason","exit_code","attempts","retry_seconds","timestamp"):
        if key in d:
            print("TUNNEL",key,d[key])
except (OSError,ValueError):
    print("TUNNEL status unavailable")
PY
fi

if ((SHOW_LOGS)); then
  echo "== update log =="
  tail -n 80 "$BRIDGE_DIR/logs/update.log" 2>/dev/null || true
fi
