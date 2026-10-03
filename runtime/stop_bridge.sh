#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
PIDFILE="$HOME/termux-mcp-bridge/bridge.pid"
SERVER_PIDFILE="$HOME/termux-mcp-bridge/server.pid"
PROXY_PIDFILE="$HOME/termux-mcp-bridge/proxy.pid"
if [[ -f "$PIDFILE" ]]; then PID="$(<"$PIDFILE")"; if kill -0 "$PID" 2>/dev/null; then kill "$PID"; fi; rm -f "$PIDFILE"; fi
if [[ -f "$SERVER_PIDFILE" ]]; then PID="$(<"$SERVER_PIDFILE")"; if kill -0 "$PID" 2>/dev/null; then kill "$PID"; fi; rm -f "$SERVER_PIDFILE"; fi
if [[ -f "$PROXY_PIDFILE" ]]; then PID="$(<"$PROXY_PIDFILE")"; if kill -0 "$PID" 2>/dev/null; then kill "$PID"; fi; rm -f "$PROXY_PIDFILE"; fi
echo "Stopped"
