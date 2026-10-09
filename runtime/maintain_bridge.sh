#!/data/data/com.termux/files/usr/bin/bash
# Safe, opportunistic self-maintenance driven by Android JobScheduler.
set -Eeuo pipefail
umask 077

ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
STATE="$ROOT/state/maintenance.json"
LOG="$ROOT/logs/maintenance.log"
LOCK="$ROOT/.maintenance.lock"
mkdir -p "$ROOT/state" "$ROOT/logs"
chmod 700 "$ROOT/state"
touch "$LOG"
chmod 600 "$LOG"

exec 8>"$LOCK"
# Android can reschedule the job while a previous run is still active.
if ! flock -n 8; then
  echo "Maintenance already active; skipped overlapping invocation."
  exit 0
fi

rotate_log() {
  if [[ $(wc -c <"$LOG") -gt 2000000 ]]; then
    local tmp
    tmp="$(mktemp "$ROOT/logs/.maintenance.XXXXXX")"
    tail -c 1000000 "$LOG" >"$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$LOG"
  fi
}

rotate_log
printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "maintenance started" >>"$LOG"
start_rc=0
update_rc=0

if [[ -x "$ROOT/start_bridge.sh" ]]; then
  "$ROOT/start_bridge.sh" 8>&- >>"$LOG" 2>&1 || start_rc=$?
else
  start_rc=127
  echo "Local startup script is unavailable" >>"$LOG"
fi

if [[ -x "$ROOT/update_termux.sh" ]]; then
  # Do not force a same-version reinstall every hour.
  # The transactional updater checks the latest stable published release,
  # validates SHA-256 and MCP schemas, and does not restart a healthy tunnel.
  # Execute a private snapshot: an updater must never overwrite its own script.
  updater_snapshot="$(mktemp "$ROOT/state/.updater-run.XXXXXX")"
  cp "$ROOT/update_termux.sh" "$updater_snapshot"
  chmod 700 "$updater_snapshot"
  "$updater_snapshot" 8>&- >>"$LOG" 2>&1 || update_rc=$?
  rm -f "$updater_snapshot"
else
  update_rc=127
  echo "Verified update script is unavailable" >>"$LOG"
fi

# Inspect all managed PIDs and authenticated localhost services, not just version strings.
health_script="$ROOT/current/maintenance_health.py"
if [[ ! -f "$health_script" ]]; then
  health_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/maintenance_health.py"
fi
health_rc=0
python "$health_script" "$ROOT" "$start_rc" "$update_rc" || health_rc=$?

printf '%s completed: start=%s update=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$start_rc" "$update_rc" >>"$LOG"
rotate_log
if [[ "$update_rc" -eq 75 ]]; then
  exit 0
fi
[[ "$start_rc" == 0 && "$update_rc" == 0 && "$health_rc" == 0 ]]
