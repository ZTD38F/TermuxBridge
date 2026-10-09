#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"

stop_pidfile() {
  local file="$1" role="$2"
  if [[ -s "$file" ]]; then
    local pid
    pid="$(<"$file")"
    if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] && kill -0 "$pid" 2>/dev/null; then
      # The pidfile can outlive its process. Refuse to signal a recycled PID,
      # even if it happens to be owned by the Termux Unix user.
      if [[ -f "$ROOT/recover_bridge_port.py" ]] &&
         python "$ROOT/recover_bridge_port.py" --root "$ROOT" --stop-pid "$pid" --role "$role"; then
        for _ in {1..20}; do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.1
        done
      fi
    fi
    rm -f "$file"
  fi
}

# Manual/full shutdown only. Seamless runtime updates do not call this script.
# Stop the supervisor first; otherwise it would restart the tunnel immediately.
stop_pidfile "$ROOT/watchdog.pid" watchdog
stop_pidfile "$ROOT/bridge.pid" tunnel
stop_pidfile "$ROOT/supervisor.pid" supervisor
stop_pidfile "$ROOT/server.pid" backend
stop_pidfile "$ROOT/proxy.pid" proxy
echo "Stopped"
