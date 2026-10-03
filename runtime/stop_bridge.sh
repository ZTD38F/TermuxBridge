#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"

stop_pidfile() {
  local file="$1"
  if [[ -s "$file" ]]; then
    local pid
    pid="$(<"$file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
      done
    fi
    rm -f "$file"
  fi
}

# Manual/full shutdown only. Seamless runtime updates do not call this script.
stop_pidfile "$ROOT/bridge.pid"
stop_pidfile "$ROOT/supervisor.pid"
stop_pidfile "$ROOT/server.pid"
stop_pidfile "$ROOT/proxy.pid"
echo "Stopped"
