#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
umask 077

REPO="ZTD38F/TermuxBridge"
CHANNEL="${TERMUXBRIDGE_CHANNEL:-stable}"
ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
STATE="$ROOT/source_commit"
LOCK="$ROOT/.update.lock"
LOG="$ROOT/logs/update.log"
BACKUPS="$ROOT/.bridge-backup"
TMP=""
BACKUP=""
CURRENT=""
TARGET=""

MANAGED=(
  bridge_server.py
  google_bridge_tools.py
  local_https_proxy.py
  start_bridge.sh
  status_bridge.sh
  stop_bridge.sh
  check_bridge_syntax.py
  check_phone_bridge.py
  validate_phone_integration.py
)

mkdir -p "$ROOT/logs" "$BACKUPS"
exec 9>"$LOCK"
flock -n 9 || exit 0

rotate_log() {
  [[ -f "$LOG" ]] || return 0
  local size
  size="$(stat -c %s "$LOG" 2>/dev/null || echo 0)"
  ((size <= 5242880)) && return 0
  for i in 3 2 1; do
    if ((i == 1)); then
      [[ ! -f "$LOG" ]] || mv -f "$LOG" "$LOG.1"
    else
      [[ ! -f "$LOG.$((i-1))" ]] || mv -f "$LOG.$((i-1))" "$LOG.$i"
    fi
  done
}

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"
}

cleanup() {
  local rc=$?
  [[ -z "$TMP" || ! -d "$TMP" ]] || rm -rf "$TMP"
  exit "$rc"
}
trap cleanup EXIT INT TERM
rotate_log

CURRENT="$(cat "$STATE" 2>/dev/null || true)"
TARGET="$(curl -fsSL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60 \
  "https://api.github.com/repos/$REPO/commits/$CHANNEL" |
  python -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || { log "ERROR invalid target SHA"; exit 1; }

if [[ "$CURRENT" == "$TARGET" && "${1:-}" != "--force" ]]; then
  log "ok already current $CURRENT"
  exit 0
fi

log "update $CURRENT -> $TARGET"
TMP="$(mktemp -d "$HOME/.cache/termuxbridge-update.XXXXXX")"
mkdir -p "$TMP/source"

curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 180 \
  "https://github.com/$REPO/archive/$TARGET.tar.gz" -o "$TMP/source.tar.gz"
tar -xzf "$TMP/source.tar.gz" -C "$TMP/source" --strip-components=1

for file in "${MANAGED[@]}"; do
  [[ -f "$TMP/source/runtime/$file" ]] || {
    log "ERROR release is missing runtime/$file"
    exit 1
  }
done
[[ -f "$TMP/source/update_termux.sh" ]] || { log "ERROR release is missing update_termux.sh"; exit 1; }
[[ -f "$TMP/source/termuxbridgectl" ]] || { log "ERROR release is missing termuxbridgectl"; exit 1; }

python -m py_compile \
  "$TMP/source/runtime/bridge_server.py" \
  "$TMP/source/runtime/google_bridge_tools.py" \
  "$TMP/source/runtime/local_https_proxy.py" \
  "$TMP/source/runtime/check_bridge_syntax.py" \
  "$TMP/source/runtime/check_phone_bridge.py" \
  "$TMP/source/runtime/validate_phone_integration.py"
for file in start_bridge.sh status_bridge.sh stop_bridge.sh; do
  bash -n "$TMP/source/runtime/$file"
done
bash -n "$TMP/source/update_termux.sh"
bash -n "$TMP/source/termuxbridgectl"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUPS/auto-update-$stamp"
mkdir -p "$BACKUP"
for file in "${MANAGED[@]}"; do
  [[ ! -e "$ROOT/$file" ]] || cp -a "$ROOT/$file" "$BACKUP/$file"
done
[[ ! -e "$ROOT/update_termux.sh" ]] || cp -a "$ROOT/update_termux.sh" "$BACKUP/update_termux.sh"
[[ ! -e "$HOME/bin/termuxbridgectl" ]] || cp -a "$HOME/bin/termuxbridgectl" "$BACKUP/termuxbridgectl"
[[ ! -e "$STATE" ]] || cp -a "$STATE" "$BACKUP/source_commit"

rollback() {
  log "rollback to $CURRENT"
  "$ROOT/stop_bridge.sh" >/dev/null 2>&1 || true
  for file in "${MANAGED[@]}"; do
    [[ ! -e "$BACKUP/$file" ]] || cp -a "$BACKUP/$file" "$ROOT/$file"
  done
  [[ ! -e "$BACKUP/update_termux.sh" ]] || cp -a "$BACKUP/update_termux.sh" "$ROOT/update_termux.sh"
  mkdir -p "$HOME/bin"
  [[ ! -e "$BACKUP/termuxbridgectl" ]] || cp -a "$BACKUP/termuxbridgectl" "$HOME/bin/termuxbridgectl"
  if [[ -e "$BACKUP/source_commit" ]]; then
    cp -a "$BACKUP/source_commit" "$STATE"
  else
    rm -f "$STATE"
  fi
  chmod +x "$ROOT/start_bridge.sh" "$ROOT/status_bridge.sh" "$ROOT/stop_bridge.sh" "$ROOT/update_termux.sh" 2>/dev/null || true
  chmod +x "$HOME/bin/termuxbridgectl" 2>/dev/null || true
  "$ROOT/start_bridge.sh" >>"$LOG" 2>&1 || true
}

"$ROOT/stop_bridge.sh" >>"$LOG" 2>&1 || true
if ! {
  for file in "${MANAGED[@]}"; do
    cp -a "$TMP/source/runtime/$file" "$ROOT/$file"
  done
  cp -a "$TMP/source/update_termux.sh" "$ROOT/update_termux.sh"
  mkdir -p "$HOME/bin"
  cp -a "$TMP/source/termuxbridgectl" "$HOME/bin/termuxbridgectl"
  chmod +x "$ROOT/start_bridge.sh" "$ROOT/status_bridge.sh" "$ROOT/stop_bridge.sh" "$ROOT/update_termux.sh" "$HOME/bin/termuxbridgectl"
  printf '%s\n' "$TARGET" >"$STATE"
  "$ROOT/start_bridge.sh" >>"$LOG" 2>&1

  python - <<'PY'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=5) as r:
    data=json.load(r)
if data.get("ok") is not True:
    raise SystemExit("MCP health check failed")
PY

  "$ROOT/status_bridge.sh" >>"$LOG" 2>&1
}; then
  rollback
  log "ERROR update failed and rollback was attempted"
  exit 1
fi

find "$BACKUPS" -maxdepth 1 -type d -name 'auto-update-*' -printf '%T@ %p\n' |
  sort -nr | awk 'NR>3 {sub(/^[^ ]+ /,""); print}' |
  while IFS= read -r old; do rm -rf "$old"; done

log "ok activated $TARGET"
