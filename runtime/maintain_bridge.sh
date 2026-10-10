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
autonomy="$ROOT/current/autonomy_engine.py"
# Old verified releases remain compatible if the helper isn't installed yet.
network_due="RUN"
if [[ -f "$autonomy" ]]; then
  network_due="$(python "$autonomy" policy "$ROOT" 2>>"$LOG")" || network_due="RUN"
fi

if [[ -x "$ROOT/start_bridge.sh" ]]; then
  "$ROOT/start_bridge.sh" 8>&- >>"$LOG" 2>&1 || start_rc=$?
else
  start_rc=127
  echo "Local startup script is unavailable" >>"$LOG"
fi

if [[ "$network_due" == "SKIP" ]]; then
  echo "Power policy: verified release check deferred; local health still checked" >>"$LOG"
elif [[ -x "$ROOT/update_termux.sh" ]]; then
  # Execute a private snapshot so an in-progress upgrade cannot change its
  # own script. Clean up even if the network check fails.
  updater_snapshot="$(mktemp "$ROOT/state/.updater-run.XXXXXX")"
  cp "$ROOT/update_termux.sh" "$updater_snapshot"
  chmod 700 "$updater_snapshot"
  "$updater_snapshot" 8>&- >>"$LOG" 2>&1 || update_rc=$?
  rm -f "$updater_snapshot"
  if [[ "$update_rc" -eq 0 && -f "$ROOT/current/autonomy_engine.py" ]]; then
    python "$ROOT/current/autonomy_engine.py" record-update "$ROOT" >>"$LOG" 2>&1 || true
  fi
else
  update_rc=127
  echo "Verified update script unavailable" >>"$LOG"
fi

# Inspect all managed PIDs and authenticated localhost services, not just version strings.
health_script="$ROOT/current/maintenance_health.py"
if [[ ! -f "$health_script" ]]; then
  health_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/maintenance_health.py"
fi
health_rc=0
python "$health_script" "$ROOT" "$start_rc" "$update_rc" || health_rc=$?
# Three independently observed backend-specific failures trigger a guarded
# last-known-good rollback. The guard requires authenticated supervisor health,
# a previous local verified generation and the update lock.
rollback_script="$ROOT/current/rollback_guard.py"
rollback_rc=0
if [[ -f "$rollback_script" ]]; then
  rollback_out="$(python "$rollback_script" 2>>"$LOG")" || rollback_rc=$?
  [[ -z "$rollback_out" ]] || printf '%s\n' "$rollback_out" >>"$LOG"
  if [[ "$rollback_out" == *'"ROLLED_BACK"'* ]]; then
    # After successful rollback re-evaluate the actual recovered service.
    health_rc=0
    start_rc=0
    update_rc=0
    python "$ROOT/current/maintenance_health.py" "$ROOT" 0 0 || health_rc=$?
  fi
fi

# Notifications are best-effort, rate limited, local-only, and must not
# turn a healthy bridge into a failure if Android denies notification access.
if [[ -f "$ROOT/current/autonomy_engine.py" ]]; then
  python "$ROOT/current/autonomy_engine.py" alerts "$ROOT" >>"$LOG" 2>&1 || true
fi
# Preserve only signed, allowlisted configuration + a consistent SQLite
# queue snapshot. Skip backups when unhealthy or storage is low.
if [[ -f "$ROOT/current/ecosystem_cli.py" ]]; then
  python "$ROOT/current/ecosystem_cli.py" auto-backup "$ROOT" >>"$LOG" 2>&1 || true
  # Refresh private offline HTML dashboard; no web server is introduced.
  python "$ROOT/current/ecosystem_cli.py" html "$ROOT" >>"$LOG" 2>&1 || true
fi

printf '%s completed: start=%s update=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$start_rc" "$update_rc" >>"$LOG"
rotate_log
if [[ "$update_rc" -eq 75 ]]; then
  exit 0
fi
[[ "$start_rc" == 0 && "$update_rc" == 0 && "$health_rc" == 0 ]]
