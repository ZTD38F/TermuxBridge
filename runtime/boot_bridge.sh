#!/data/data/com.termux/files/usr/bin/bash
# TermuxBridge managed boot hook. Safe for repeated boots and manual invocation.
set -Eeuo pipefail
umask 077
ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
LOG="$ROOT/logs/boot.log"
mkdir -p "$ROOT/logs"
touch "$LOG"
chmod 600 "$LOG"
if [[ "${TERMUXBRIDGE_BOOT_TEST:-0}" != "1" ]]; then
  # Allow Android connectivity and Termux storage mounts to initialize.
  sleep 20
fi
if [[ ! -x "$ROOT/start_bridge.sh" ]]; then
  echo "TermuxBridge bootstrap unavailable" >>"$LOG"
  exit 1
fi
# start_bridge.sh uses verified PID identities and health probes to avoid duplicates.
"$ROOT/start_bridge.sh" >>"$LOG" 2>&1 9>&- || {
  echo "TermuxBridge boot startup failed" >>"$LOG"
  exit 1
}
# JobScheduler persists, but some device builds lose pending jobs across app updates.
# Re-register idempotently; not dependent on connectivity being available at boot.
if command -v termux-job-scheduler >/dev/null && command -v termuxbridgectl >/dev/null; then
  termuxbridgectl auto-update-enable >>"$LOG" 2>&1 || true
fi
echo "TermuxBridge boot completed" >>"$LOG"
