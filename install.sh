#!/data/data/com.termux/files/usr/bin/bash
# TermuxBridge one-click repair/reinstall for an already provisioned phone.
# Source of truth: ZTD38F/TermuxBridge stable channel.
set -Eeuo pipefail
umask 077

ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
URL="https://raw.githubusercontent.com/ZTD38F/TermuxBridge/stable/update_termux.sh"
TMP=""
BACKUP=""
cleanup() {
  local rc=$?
  [[ -z "$TMP" || ! -d "$TMP" ]] || rm -rf -- "$TMP"
  if [[ "$rc" -ne 0 ]]; then
    printf '\nTermuxBridge installation did not finish (exit %s).\n' "$rc" >&2
    printf 'Safe diagnostics: termuxbridgectl status\n' >&2
    printf 'Do NOT delete secrets/, .start*.lock, or .update.lock.\n' >&2
  fi
}
trap cleanup EXIT

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
for cmd in bash curl python tar sha256sum flock mktemp; do
  command -v "$cmd" >/dev/null 2>&1 || fail "missing dependency: $cmd"
done
[[ -d "$ROOT" ]] || fail "Existing deployment not found: $ROOT. This installer repairs an already provisioned bridge."
[[ ! -L "$ROOT" ]] || fail "Deployment path is a symlink; refusing to change an ambiguous installation."
[[ -s "$ROOT/secrets/control_plane_api_key" ]] || fail "Control-plane credential missing. Reauthorize via supported provider flow; credentials cannot be generated locally."
[[ -s "$ROOT/secrets/tunnel_id" ]] || fail "Tunnel ID missing. Reauthorize via supported provider flow."
[[ -x "$ROOT/bin/tunnel-client-runtime" ]] || fail "Tunnel client missing. Existing provisioned installation required."

printf '\n== TermuxBridge: verified in-place reinstall ==\n'
printf 'Code: official GitHub stable release; secrets and user data remain in place.\n'
mkdir -p "$ROOT/backups" "$ROOT/logs"
chmod 700 "$ROOT/backups"
BACKUP="$(mktemp -d "$ROOT/backups/oneclick-XXXXXXXX")"
chmod 700 "$BACKUP"
# Backup managed scripts only. Never duplicate or print credentials.
for f in update_termux.sh start_bridge.sh stop_bridge.sh status_bridge.sh supervisor.py tunnel_watchdog.py; do
  if [[ -f "$ROOT/$f" && ! -L "$ROOT/$f" ]]; then cp -p "$ROOT/$f" "$BACKUP/$f"; fi
done
if [[ -f "$PREFIX/bin/gpt" && ! -L "$PREFIX/bin/gpt" ]]; then
  cp -p "$PREFIX/bin/gpt" "$BACKUP/gpt.previous"
fi
printf 'Previous management scripts backed up inside the private installation.\n'

TMP="$(mktemp -d "${TMPDIR:-$PREFIX/tmp}/termuxbridge-install.XXXXXXXX")"
curl -fLSs --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 90 "$URL" -o "$TMP/update_termux.sh"
[[ -s "$TMP/update_termux.sh" ]] || fail "Downloaded updater is empty"
bash -n "$TMP/update_termux.sh" || fail "Downloaded updater failed bash syntax validation"
printf 'Running official transactional updater (interrupted STAGED updates are recovered)...\n'
TERMUXBRIDGE_ROOT="$ROOT" TERMUXBRIDGE_CHANNEL=stable bash "$TMP/update_termux.sh" --force ||
  fail "Transactional update failed; managed bridge preserved where recovery was possible"

[[ -x "$PREFIX/bin/gpt" ]] || fail "Managed GPT launcher was not installed"
printf 'Starting managed bridge and tunnel watchdog...\n'
TERMUXBRIDGE_ROOT="$ROOT" "$PREFIX/bin/gpt" ||
  fail "Managed launcher failed; local runtime may still be available"

printf 'Checking committed generation and authenticated supervisor...\n'
TERMUXBRIDGE_ROOT="$ROOT" python - "$ROOT" <<'PY'
import json, pathlib, sys, urllib.request
root=pathlib.Path(sys.argv[1])
commit=(root/"source_commit").read_text().strip()
journal=json.loads((root/"state/update.json").read_text())
route=json.loads((root/"state/route.json").read_text())
assert len(commit)==40 and all(c in "0123456789abcdef" for c in commit), "Invalid source commit"
assert journal.get("phase")=="COMMITTED", f"Update not committed: {journal.get('phase')}"
assert route.get("generation")==commit, "Router still points to another generation"
token=(root/"secrets/router_token").read_text().strip()
req=urllib.request.Request("http://127.0.0.1:8765/__bridge/healthz", headers={"X-Bridge-Token":token})
with urllib.request.urlopen(req,timeout=5) as response:
    assert json.load(response).get("ok") is True, "Supervisor returned unhealthy"
print("Verified: committed release, routing, authenticated supervisor.")
PY

printf '\n== Sanitized status ==\n'
TERMUXBRIDGE_ROOT="$ROOT" "$ROOT/status_bridge.sh"
printf '\nLocal reinstall completed. Remote ChatGPT connectivity requires a live MCP tool call.\n'
