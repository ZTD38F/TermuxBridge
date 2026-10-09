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

# Only report bounded machine state. Never export tokens, raw logs or argv.
python - "$STATE" "$ROOT" "$start_rc" "$update_rc" <<'PY'
import datetime
import json
import os
import pathlib
import sys

state=pathlib.Path(sys.argv[1])
root=pathlib.Path(sys.argv[2])
start_rc=int(sys.argv[3])
update_rc=int(sys.argv[4])

def load(name):
    try:
        return json.loads((root/"state"/name).read_text())
    except (OSError, ValueError):
        return {}

def source():
    try:
        return (root/"source_commit").read_text().strip()
    except OSError:
        return ""

route=load("route.json")
journal=load("update.json")
tunnel=load("tunnel_status.json")
generation=source()
consistent=(
    bool(generation)
    and generation==route.get("generation")
    and generation==journal.get("current_generation")
    and journal.get("phase")=="COMMITTED"
)
if update_rc==75:
    result="BUSY"
elif start_rc!=0 or update_rc!=0:
    result="FAILED"
elif not consistent:
    result="DEGRADED"
else:
    result="OK"
record={
    "last_checked_at":datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "result":result,
    "startup_exit_code":start_rc,
    "updater_exit_code":update_rc,
    "installed_generation":generation,
    "active_generation":route.get("generation"),
    "update_phase":journal.get("phase"),
    "tunnel_state":tunnel.get("state"),
    "verified_active_generation":consistent,
}
tmp=state.with_suffix(".tmp")
tmp.write_text(json.dumps(record,sort_keys=True,separators=(",",":"))+"\n")
tmp.chmod(0o600)
os.replace(tmp,state)
print("Maintenance:",result,"generation:",generation[:12],"tunnel:",tunnel.get("state","UNKNOWN"))
PY

printf '%s completed: start=%s update=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$start_rc" "$update_rc" >>"$LOG"
rotate_log
if [[ "$update_rc" -eq 75 ]]; then
  exit 0
fi
[[ "$start_rc" == 0 && "$update_rc" == 0 ]]
