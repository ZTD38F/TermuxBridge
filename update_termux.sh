#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
umask 077

REPO="ZTD38F/TermuxBridge"
CHANNEL="${TERMUXBRIDGE_CHANNEL:-stable}"
ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
STATE_DIR="$ROOT/state"
RELEASES="$ROOT/releases"
STATE="$ROOT/source_commit"
ROUTE="$STATE_DIR/route.json"
JOURNAL="$STATE_DIR/update.json"
LOCK="$ROOT/.update.lock"
LOG="$ROOT/logs/update.log"
ROUTER_TOKEN_FILE="$ROOT/secrets/router_token"
BACKEND_TOKEN_FILE="$ROOT/secrets/backend_token"
KEYFILE="$ROOT/secrets/control_plane_api_key"
TUNNEL_FILE="$ROOT/secrets/tunnel_id"
TMP=""
TARGET=""
CURRENT=""
CANDIDATE_PID=""
PREVIOUS_PID=""
PREVIOUS_GENERATION=""
PREVIOUS_PORT=""
CANDIDATE_PORT=""
SWITCHED=0

RUNTIME_FILES=(
  bridge_server.py
  google_bridge_tools.py
  local_https_proxy.py
  check_bridge_syntax.py
  check_phone_bridge.py
  validate_phone_integration.py
)
MANAGEMENT_FILES=(start_bridge.sh status_bridge.sh stop_bridge.sh supervisor.py)

mkdir -p "$ROOT/logs" "$ROOT/secrets" "$STATE_DIR" "$RELEASES"
chmod 700 "$ROOT/secrets" "$STATE_DIR" "$RELEASES"
exec 9>"$LOCK"
flock -n 9 || exit 0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

cleanup() {
  local rc=$?
  [[ -z "$TMP" || ! -d "$TMP" ]] || rm -rf "$TMP"
  exit "$rc"
}
trap cleanup EXIT INT TERM

ensure_secret() {
  local path="$1"
  if [[ ! -s "$path" ]]; then
    python - "$path" <<'PY'
from pathlib import Path
import secrets,sys
p=Path(sys.argv[1]); p.write_text(secrets.token_hex(32)+"\n", encoding="utf-8"); p.chmod(0o600)
PY
  fi
  chmod 600 "$path"
}

write_route() {
  local generation="$1" port="$2"
  python - "$ROUTE" "$generation" "$port" <<'PY'
import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); tmp=p.with_suffix(".tmp")
tmp.write_text(json.dumps({"generation":sys.argv[2],"port":int(sys.argv[3])},separators=(",",":"))+"\n",encoding="utf-8")
os.replace(tmp,p); p.chmod(0o600)
PY
}

journal() {
  local phase="$1" failure="${2:-}" rollback="${3:-}"
  python - "$JOURNAL" "$phase" "$CURRENT" "$TARGET" "$PREVIOUS_GENERATION" "$PREVIOUS_PORT" "$PREVIOUS_PID" "$CANDIDATE_PORT" "$CANDIDATE_PID" "$failure" "$rollback" <<'PY'
import json,os,pathlib,sys,time,uuid
p=pathlib.Path(sys.argv[1])
old={}
if p.exists():
    try: old=json.loads(p.read_text())
    except Exception: old={}
data={
 "transaction_id": old.get("transaction_id") or uuid.uuid4().hex,
 "update_kind":"RUNTIME_UPDATE",
 "phase":sys.argv[2],
 "current_generation":sys.argv[3],
 "candidate_generation":sys.argv[4],
 "previous_generation":sys.argv[5],
 "previous_port":int(sys.argv[6] or 0),
 "previous_pid":int(sys.argv[7] or 0),
 "candidate_port":int(sys.argv[8] or 0),
 "candidate_pid":int(sys.argv[9] or 0),
 "failure_reason":sys.argv[10],
 "rollback_reason":sys.argv[11],
 "updated_at":int(time.time()),
}
tmp=p.with_suffix(".tmp"); tmp.write_text(json.dumps(data,separators=(",",":"))+"\n"); os.replace(tmp,p); p.chmod(0o600)
PY
}

pid_alive() { [[ -n "${1:-}" && "$1" =~ ^[0-9]+$ ]] && kill -0 "$1" 2>/dev/null; }

backend_health() {
  local port="$1"
  python - "$port" "$BACKEND_TOKEN_FILE" <<'PY' >/dev/null 2>&1
import json,pathlib,sys,urllib.request
token=pathlib.Path(sys.argv[2]).read_text().strip()
req=urllib.request.Request(f"http://127.0.0.1:{int(sys.argv[1])}/healthz",headers={"X-Bridge-Backend-Token":token})
with urllib.request.urlopen(req,timeout=3) as r: d=json.load(r)
if d.get("ok") is not True: raise SystemExit(1)
PY
}

router_call() {
  local path="$1"
  python - "$path" "$ROUTER_TOKEN_FILE" <<'PY'
import pathlib,sys,urllib.request
token=pathlib.Path(sys.argv[2]).read_text().strip()
req=urllib.request.Request("http://127.0.0.1:8765"+sys.argv[1],headers={"X-Bridge-Token":token})
with urllib.request.urlopen(req,timeout=5) as r: print(r.read().decode())
PY
}

mcp_contract() {
  local url="$1" header="$2" token_file="$3"
  python - "$url" "$header" "$token_file" <<'PY'
import json,pathlib,sys,urllib.request
url,header,path=sys.argv[1:]
token=pathlib.Path(path).read_text().strip()
def rpc(method, params=None):
    body={"jsonrpc":"2.0","id":1,"method":method}
    if params is not None: body["params"]=params
    data=json.dumps(body,separators=(",",":")).encode()
    req=urllib.request.Request(url,data=data,headers={"Content-Type":"application/json",header:token})
    with urllib.request.urlopen(req,timeout=8) as r: return json.load(r)
init=rpc("initialize",{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"TermuxBridgeUpdater","version":"1"}})
tools=rpc("tools/list",{})
if "result" not in init or "result" not in tools or not isinstance(tools["result"].get("tools"),list):
    raise SystemExit("MCP contract failed")
print(json.dumps(tools["result"]["tools"],sort_keys=True,separators=(",",":")))
PY
}

compare_tool_contracts() {
  local current_file="$1" candidate_file="$2"
  python - "$current_file" "$candidate_file" <<'PY'
import json,sys
old={t["name"]:t.get("inputSchema") for t in json.load(open(sys.argv[1]))}
new={t["name"]:t.get("inputSchema") for t in json.load(open(sys.argv[2]))}
missing=sorted(set(old)-set(new))
changed=sorted(k for k in old.keys()&new.keys() if old[k]!=new[k])
if missing or changed:
    raise SystemExit("breaking tool contract: missing=%r changed=%r"%(missing,changed))
PY
}

recover_unfinished() {
  [[ -s "$JOURNAL" ]] || return 0
  read -r phase prev_gen prev_port prev_pid cand_pid < <(python - "$JOURNAL" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print(d.get("phase",""), d.get("previous_generation",""), d.get("previous_port",0), d.get("previous_pid",0), d.get("candidate_pid",0))
PY
)
  case "$phase" in
    ""|COMMITTED|FAILED_ROLLED_BACK) return 0 ;;
  esac
  log "recovering interrupted update phase=$phase"
  if [[ -n "$prev_gen" && "$prev_port" != 0 ]]; then write_route "$prev_gen" "$prev_port"; fi
  if pid_alive "$cand_pid"; then kill "$cand_pid" 2>/dev/null || true; fi
  if pid_alive "$prev_pid"; then printf '%s\n' "$prev_pid" >"$ROOT/server.pid"; fi
  python - "$JOURNAL" <<'PY'
import json,os,pathlib,sys,time
p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text()); d["phase"]="FAILED_ROLLED_BACK"; d["rollback_reason"]="interrupted update recovered"; d["updated_at"]=int(time.time())
tmp=p.with_suffix(".tmp"); tmp.write_text(json.dumps(d,separators=(",",":"))+"\n"); os.replace(tmp,p)
PY
}

install_management_from_stage() {
  local source_root="$1"
  for file in "${MANAGEMENT_FILES[@]}"; do
    cp -a "$source_root/runtime/$file" "$ROOT/$file"
  done
  cp -a "$source_root/update_termux.sh" "$ROOT/update_termux.sh"
  mkdir -p "$HOME/bin"
  cp -a "$source_root/termuxbridgectl" "$HOME/bin/termuxbridgectl"
  chmod 700 "$ROOT/start_bridge.sh" "$ROOT/status_bridge.sh" "$ROOT/stop_bridge.sh" "$ROOT/update_termux.sh" "$HOME/bin/termuxbridgectl"
  chmod 600 "$ROOT/supervisor.py"
}

ensure_seamless_topology() {
  ensure_secret "$ROUTER_TOKEN_FILE"
  ensure_secret "$BACKEND_TOKEN_FILE"
  if [[ -s "$ROOT/supervisor.pid" ]] && pid_alive "$(<"$ROOT/supervisor.pid")" && [[ -s "$ROUTE" ]]; then return 0; fi
  log "one-time migration to long-lived supervisor topology"
  install_management_from_stage "$TMP/source"
  local old_generation
  old_generation="$(cat "$STATE" 2>/dev/null || echo legacy)"
  write_route "$old_generation" 18771
  "$ROOT/stop_bridge.sh" >>"$LOG" 2>&1 || true
  "$ROOT/start_bridge.sh" >>"$LOG" 2>&1
}

recover_unfinished

CURRENT="$(cat "$STATE" 2>/dev/null || true)"
TARGET="$(curl -fsSL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60 "https://api.github.com/repos/$REPO/commits/$CHANNEL" |
  python -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || { log "ERROR invalid target SHA"; exit 1; }
if [[ "$CURRENT" == "$TARGET" && "${1:-}" != "--force" ]]; then log "ok already current $CURRENT"; exit 0; fi

TMP="$(mktemp -d "$HOME/.cache/termuxbridge-update.XXXXXX")"
mkdir -p "$TMP/source"
journal CHECKING
log "runtime update $CURRENT -> $TARGET"

curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 180 "https://github.com/$REPO/archive/$TARGET.tar.gz" -o "$TMP/source.tar.gz"
tar -xzf "$TMP/source.tar.gz" -C "$TMP/source" --strip-components=1
journal DOWNLOADED

for file in "${RUNTIME_FILES[@]}" "${MANAGEMENT_FILES[@]}"; do
  [[ -f "$TMP/source/runtime/$file" ]] || { journal FAILED_PRE_SWITCH "missing runtime/$file"; exit 1; }
done
[[ -f "$TMP/source/update_termux.sh" && -f "$TMP/source/termuxbridgectl" ]] || { journal FAILED_PRE_SWITCH "missing management scripts"; exit 1; }
[[ -f "$TMP/source/TUNNEL_CLIENT_VERSION" ]] || { journal FAILED_PRE_SWITCH "missing tunnel pin"; exit 1; }

python -m py_compile "$TMP/source/runtime/"*.py
for file in start_bridge.sh status_bridge.sh stop_bridge.sh; do bash -n "$TMP/source/runtime/$file"; done
bash -n "$TMP/source/update_termux.sh" "$TMP/source/termuxbridgectl"
journal VERIFIED_ARTIFACT

CANDIDATE="$RELEASES/$TARGET"
rm -rf "$CANDIDATE.tmp"
mkdir -p "$CANDIDATE.tmp"
for file in "${RUNTIME_FILES[@]}"; do cp -a "$TMP/source/runtime/$file" "$CANDIDATE.tmp/$file"; done
mv "$CANDIDATE.tmp" "$CANDIDATE"
journal STAGED

ensure_seamless_topology

read -r PREVIOUS_GENERATION PREVIOUS_PORT < <(python - "$ROUTE" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); print(d["generation"], int(d["port"]))
PY
)
PREVIOUS_PID="$(cat "$ROOT/server.pid" 2>/dev/null || true)"
if [[ "$PREVIOUS_PORT" == 18771 ]]; then CANDIDATE_PORT=18772; else CANDIDATE_PORT=18771; fi
journal CANDIDATE_STARTING

nohup env TERMUX_BRIDGE_ROOT="$HOME" TERMUX_BRIDGE_JOBS="$ROOT/jobs" TERMUX_BRIDGE_BACKEND_TOKEN_FILE="$BACKEND_TOKEN_FILE"   python "$CANDIDATE/bridge_server.py" --http "$CANDIDATE_PORT" >"$ROOT/logs/server-$TARGET.log" 2>&1 &
CANDIDATE_PID=$!
for _ in {1..40}; do pid_alive "$CANDIDATE_PID" && backend_health "$CANDIDATE_PORT" && break; sleep 0.25; done
if ! pid_alive "$CANDIDATE_PID" || ! backend_health "$CANDIDATE_PORT"; then
  journal FAILED_PRE_SWITCH "candidate health failed"; kill "$CANDIDATE_PID" 2>/dev/null || true; exit 1
fi

mcp_contract "http://127.0.0.1:$CANDIDATE_PORT/mcp" "X-Bridge-Backend-Token" "$BACKEND_TOKEN_FILE" >"$TMP/candidate-tools.json"
mcp_contract "http://127.0.0.1:8765/mcp" "X-Bridge-Token" "$ROUTER_TOKEN_FILE" >"$TMP/current-tools.json"
compare_tool_contracts "$TMP/current-tools.json" "$TMP/candidate-tools.json"
journal CANDIDATE_HEALTHY

write_route "$TARGET" "$CANDIDATE_PORT"
SWITCHED=1
journal SWITCHED

# Prove the stable router serves the candidate before touching current pointers.
mcp_contract "http://127.0.0.1:8765/mcp" "X-Bridge-Token" "$ROUTER_TOKEN_FILE" >"$TMP/switched-tools.json"
compare_tool_contracts "$TMP/current-tools.json" "$TMP/switched-tools.json"
journal DRAINING_OLD

for _ in {1..120}; do
  OLD_INFLIGHT="$(router_call /__bridge/status | python -c 'import json,sys; d=json.load(sys.stdin); print(d.get("inflight",{}).get(sys.argv[1],0))' "$PREVIOUS_GENERATION")"
  [[ "$OLD_INFLIGHT" == 0 ]] && break
  sleep 0.5
done
if [[ "${OLD_INFLIGHT:-1}" != 0 ]]; then
  write_route "$PREVIOUS_GENERATION" "$PREVIOUS_PORT"
  SWITCHED=0
  kill "$CANDIDATE_PID" 2>/dev/null || true
  journal FAILED_ROLLED_BACK "drain timeout" "route restored before candidate retirement"
  exit 1
fi

journal OBSERVING
sleep 3
backend_health "$CANDIDATE_PORT"
mcp_contract "http://127.0.0.1:8765/mcp" "X-Bridge-Token" "$ROUTER_TOKEN_FILE" >/dev/null
journal VERIFIED

if [[ -L "$ROOT/current" ]]; then
  ln -sfn "$(readlink -f "$ROOT/current")" "$ROOT/previous"
fi
ln -sfn "$CANDIDATE" "$ROOT/current"
printf '%s\n' "$TARGET" >"$STATE"
printf '%s\n' "$CANDIDATE_PID" >"$ROOT/server.pid"
install_management_from_stage "$TMP/source"
journal COMMITTED

# Old backend retirement happens after commit. A crash here leaves a harmless
# extra old backend; the next update/start can clean it without routing to it.
if pid_alive "$PREVIOUS_PID" && [[ "$PREVIOUS_PID" != "$CANDIDATE_PID" ]]; then kill "$PREVIOUS_PID" 2>/dev/null || true; fi

# Transport updates are intentionally classified separately. Ordinary runtime
# updates never replace or restart the tunnel-client.
PINNED_TUNNEL="$(tr -d '[:space:]' <"$TMP/source/TUNNEL_CLIENT_VERSION")"
CURRENT_TUNNEL="$("$ROOT/bin/tunnel-client-runtime" --version 2>/dev/null | awk '{print "v"$1; exit}' || true)"
if [[ -n "$PINNED_TUNNEL" && "$PINNED_TUNNEL" != "$CURRENT_TUNNEL" ]]; then
  log "transport update available $CURRENT_TUNNEL -> $PINNED_TUNNEL; runtime committed without transport restart"
  python - "$JOURNAL" "$PINNED_TUNNEL" <<'PY'
import json,os,pathlib,sys,time
p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text()); d["transport_update_pending"]=sys.argv[2]; d["updated_at"]=int(time.time())
tmp=p.with_suffix(".tmp"); tmp.write_text(json.dumps(d,separators=(",",":"))+"\n"); os.replace(tmp,p)
PY
fi

find "$RELEASES" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
  sort -nr | awk 'NR>3 {sub(/^[^ ]+ /,""); print}' |
  while IFS= read -r old; do [[ "$old" == "$(readlink -f "$ROOT/current" 2>/dev/null)" ]] || rm -rf "$old"; done

log "ok seamless runtime activation $TARGET; tunnel pid=$(cat "$ROOT/bridge.pid" 2>/dev/null || echo unknown)"
