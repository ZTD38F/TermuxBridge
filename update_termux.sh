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
SELF_HOSTED_UPDATE=0

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
  for file in start_bridge.sh status_bridge.sh stop_bridge.sh; do
    cp -a "$source_root/runtime/$file" "$ROOT/$file"
  done
  if [[ ! -f "$ROOT/supervisor.py" ]]; then
    cp -a "$source_root/runtime/supervisor.py" "$ROOT/supervisor.py"
  fi
  cp -a "$source_root/update_termux.sh" "$ROOT/update_termux.sh"
  mkdir -p "$HOME/bin"
  cp -a "$source_root/termuxbridgectl" "$HOME/bin/termuxbridgectl"
  chmod 700 "$ROOT/start_bridge.sh" "$ROOT/status_bridge.sh" "$ROOT/stop_bridge.sh" "$ROOT/update_termux.sh" "$HOME/bin/termuxbridgectl"
  chmod 600 "$ROOT/supervisor.py"
}

update_supervisor() {
  local candidate="$1"
  [[ -f "$ROOT/supervisor.py" ]] || { cp -a "$candidate" "$ROOT/supervisor.py"; return 0; }
  [[ "$(sha256sum "$ROOT/supervisor.py" | awk '{print $1}')" != "$(sha256sum "$candidate" | awk '{print $1}')" ]] || return 0

  local busy
  busy="$(router_call /__bridge/status | python -c 'import json,sys; print(sum(int(v) for v in json.load(sys.stdin).get("inflight",{}).values()))')"
  if [[ "$busy" != 0 ]]; then log "supervisor update deferred inflight=$busy"; return 0; fi

  local old_pid backup new_pid
  old_pid="$(cat "$ROOT/supervisor.pid" 2>/dev/null || true)"
  backup="$ROOT/supervisor.py.previous"
  cp -a "$ROOT/supervisor.py" "$backup"
  if pid_alive "$old_pid"; then kill "$old_pid" 2>/dev/null || true; fi
  cp -a "$candidate" "$ROOT/supervisor.py"
  chmod 600 "$ROOT/supervisor.py"
  nohup env TERMUXBRIDGE_ROOT="$ROOT" python "$ROOT/supervisor.py" >"$ROOT/logs/supervisor.log" 2>&1 &
  new_pid=$!
  printf '%s\n' "$new_pid" >"$ROOT/supervisor.pid"
  sleep 1
  if ! pid_alive "$new_pid" || ! router_call /__bridge/healthz >/dev/null 2>&1; then
    if pid_alive "$new_pid"; then kill "$new_pid" 2>/dev/null || true; fi
    cp -a "$backup" "$ROOT/supervisor.py"
    nohup env TERMUXBRIDGE_ROOT="$ROOT" python "$ROOT/supervisor.py" >"$ROOT/logs/supervisor.log" 2>&1 &
    printf '%s\n' "$!" >"$ROOT/supervisor.pid"
    log "ERROR supervisor update rolled back"
    return 1
  fi
  log "supervisor update committed pid=$new_pid"
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
TARGET_TAG=""
if [[ "$CHANNEL" == "stable" ]]; then
  RELEASE_JSON="$(curl -fsSL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null || true)"
  if [[ -n "$RELEASE_JSON" ]]; then
    read -r TARGET_TAG RELEASE_DRAFT RELEASE_PRERELEASE < <(python -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tag_name",""), str(bool(d.get("draft"))).lower(), str(bool(d.get("prerelease"))).lower())' <<<"$RELEASE_JSON")
    if [[ -n "$TARGET_TAG" && "$RELEASE_DRAFT" == false && "$RELEASE_PRERELEASE" == false ]]; then
      TARGET="$(curl -fsSL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60 "https://api.github.com/repos/$REPO/commits/$TARGET_TAG" |
        python -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
    else
      TARGET_TAG=""
    fi
  fi
fi
if [[ -z "${TARGET:-}" ]]; then
  TARGET="$(curl -fsSL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60 "https://api.github.com/repos/$REPO/commits/$CHANNEL" |
    python -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
fi
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || { log "ERROR invalid target SHA"; exit 1; }
if [[ "$CURRENT" == "$TARGET" && "${1:-}" != "--force" ]]; then log "ok already current $CURRENT"; exit 0; fi

TMP="$(mktemp -d "$HOME/.cache/termuxbridge-update.XXXXXX")"
mkdir -p "$TMP/source"
journal CHECKING
log "runtime update $CURRENT -> $TARGET"

if [[ -n "$TARGET_TAG" ]]; then
  ASSET="TermuxBridge-$TARGET_TAG.tar.gz"
  BASE="https://github.com/$REPO/releases/download/$TARGET_TAG"
  curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 180 "$BASE/$ASSET" -o "$TMP/$ASSET"
  curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60 "$BASE/SHA256SUMS.txt" -o "$TMP/source-SHA256SUMS.txt"
  grep -E "[[:space:]]${ASSET//./\\.}$" "$TMP/source-SHA256SUMS.txt" >"$TMP/source.sha256" ||
    { journal FAILED_PRE_SWITCH "release checksum missing"; exit 1; }
  (cd "$TMP" && sha256sum -c source.sha256) >>"$LOG" 2>&1 ||
    { journal FAILED_PRE_SWITCH "release checksum mismatch"; exit 1; }
  tar -xzf "$TMP/$ASSET" -C "$TMP/source"
else
  curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 180 "https://github.com/$REPO/archive/$TARGET.tar.gz" -o "$TMP/source.tar.gz"
  tar -xzf "$TMP/source.tar.gz" -C "$TMP/source" --strip-components=1
fi
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
# If this updater was invoked through the currently active MCP backend (for
# example via termuxbridgectl update-now called from ChatGPT), that request
# itself keeps one in-flight request on the old generation until this process
# exits. Detect that ancestry so drain/retirement cannot deadlock on itself.
if [[ "$PREVIOUS_PID" =~ ^[0-9]+$ ]] && python - "$PREVIOUS_PID" <<'PY'
import os,sys
target=int(sys.argv[1]); pid=os.getppid(); seen=set()
while pid > 1 and pid not in seen:
    if pid == target:
        raise SystemExit(0)
    seen.add(pid)
    try:
        with open(f"/proc/{pid}/stat","r",encoding="utf-8") as f:
            pid=int(f.read().split()[3])
    except Exception:
        break
raise SystemExit(1)
PY
then
  SELF_HOSTED_UPDATE=1
  log "self-hosted MCP update detected; allowing own in-flight request during drain"
fi
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

DRAIN_FLOOR="$SELF_HOSTED_UPDATE"
for _ in {1..120}; do
  OLD_INFLIGHT="$(router_call /__bridge/status | python -c 'import json,sys; d=json.load(sys.stdin); print(d.get("inflight",{}).get(sys.argv[1],0))' "$PREVIOUS_GENERATION")"
  [[ "$OLD_INFLIGHT" -le "$DRAIN_FLOOR" ]] && break
  sleep 0.5
done
if [[ "${OLD_INFLIGHT:-1}" -gt "$DRAIN_FLOOR" ]]; then
  write_route "$PREVIOUS_GENERATION" "$PREVIOUS_PORT"
  SWITCHED=0
  kill "$CANDIDATE_PID" 2>/dev/null || true
  if pid_alive "$PREVIOUS_PID"; then printf '%s\n' "$PREVIOUS_PID" >"$ROOT/server.pid"; fi
  journal FAILED_ROLLED_BACK "drain timeout" "route and previous backend pointer restored"
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

# Old backend retirement happens after commit. A self-hosted MCP update must
# return its response through the previous backend, so retire that backend only
# after its in-flight count reaches zero. Ordinary timer/local-shell updates
# still retire synchronously.
if pid_alive "$PREVIOUS_PID" && [[ "$PREVIOUS_PID" != "$CANDIDATE_PID" ]]; then
  if [[ "$SELF_HOSTED_UPDATE" == 1 ]]; then
    nohup python - "$PREVIOUS_GENERATION" "$PREVIOUS_PID" "$ROUTER_TOKEN_FILE" "$LOG" <<'PY' >/dev/null 2>&1 &
import json,os,pathlib,signal,sys,time,urllib.request
generation,pid_s,token_path,log_path=sys.argv[1:]
pid=int(pid_s)
token=pathlib.Path(token_path).read_text().strip()
for _ in range(120):
    try:
        req=urllib.request.Request("http://127.0.0.1:8765/__bridge/status",headers={"X-Bridge-Token":token})
        with urllib.request.urlopen(req,timeout=3) as r:
            inflight=int(json.load(r).get("inflight",{}).get(generation,0))
        if inflight == 0:
            try: os.kill(pid,signal.SIGTERM)
            except ProcessLookupError: pass
            with open(log_path,"a",encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())} deferred old backend retired pid={pid}\n")
            raise SystemExit(0)
    except Exception:
        pass
    time.sleep(0.5)
with open(log_path,"a",encoding="utf-8") as f:
    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())} WARNING deferred old backend retirement timed out pid={pid}\n")
PY
    log "deferred old backend retirement scheduled pid=$PREVIOUS_PID"
  else
    kill "$PREVIOUS_PID" 2>/dev/null || true
  fi
fi
update_supervisor "$TMP/source/runtime/supervisor.py"

# Transport updates are a separate blue/green transaction. Both transport
# generations target the same authenticated supervisor, so application state is
# not split across two runtimes.
PINNED_TUNNEL="$(tr -d '[:space:]' <"$TMP/source/TUNNEL_CLIENT_VERSION")"
[[ "$PINNED_TUNNEL" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { log "ERROR invalid tunnel pin"; exit 1; }
CURRENT_TUNNEL="$("$ROOT/bin/tunnel-client-runtime" --version 2>/dev/null | awk '{print "v"$1; exit}' || true)"
if [[ "$PINNED_TUNNEL" != "$CURRENT_TUNNEL" ]]; then
  log "TRANSPORT_UPDATE $CURRENT_TUNNEL -> $PINNED_TUNNEL"
  ARCH="$(uname -m)"
  [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]] || { log "ERROR unsupported transport arch $ARCH"; exit 1; }
  ASSET="tunnel-client-runtime-$PINNED_TUNNEL-linux-arm64.zip"
  BASE="https://github.com/openai/tunnel-client/releases/download/$PINNED_TUNNEL"
  curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 180 "$BASE/$ASSET" -o "$TMP/$ASSET"
  curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 60 "$BASE/SHA256SUMS.txt" -o "$TMP/transport-SHA256SUMS.txt"
  grep -E "[[:space:]]${ASSET//./\\.}$" "$TMP/transport-SHA256SUMS.txt" >"$TMP/transport.sha256"
  (cd "$TMP" && sha256sum -c transport.sha256) >>"$LOG" 2>&1
  mkdir -p "$TMP/transport"
  unzip -q "$TMP/$ASSET" -d "$TMP/transport"
  NEW_TRANSPORT="$TMP/transport/tunnel-client-runtime"
  [[ -x "$NEW_TRANSPORT" ]] || chmod 700 "$NEW_TRANSPORT"
  "$NEW_TRANSPORT" --version >>"$LOG" 2>&1

  VERSIONED="$ROOT/bin/tunnel-client-runtime-$PINNED_TUNNEL"
  install -m 700 "$NEW_TRANSPORT" "$VERSIONED"
  OLD_PID="$(cat "$ROOT/bridge.pid" 2>/dev/null || true)"
  TUNNEL_ID="$(cat "$TUNNEL_FILE")"
  export CONTROL_PLANE_TUNNEL_ID="$TUNNEL_ID"
  export HTTPS_PROXY="http://127.0.0.1:8877"
  export https_proxy="$HTTPS_PROXY"
  export CA_BUNDLE="$PREFIX/etc/tls/cert.pem"
  export SSL_CERT_FILE="$CA_BUNDLE"
  TRANSPORT_LOG="$ROOT/logs/tunnel-$PINNED_TUNNEL.log"

  nohup "$VERSIONED" run \
    --control-plane.api-key "file:$KEYFILE" \
    --mcp.server-url http://127.0.0.1:8765/mcp \
    --mcp.extra-headers "X-Bridge-Token: file:$ROUTER_TOKEN_FILE" \
    >"$TRANSPORT_LOG" 2>&1 &
  NEW_TUNNEL_PID=$!
  sleep 5
  if ! pid_alive "$NEW_TUNNEL_PID"; then
    log "ERROR transport candidate exited before handoff"
    rm -f "$VERSIONED"
    exit 1
  fi

  # The candidate is already polling the same tunnel against the same router.
  # Retire the previous transport only after candidate survival is proven.
  if pid_alive "$OLD_PID"; then kill "$OLD_PID" 2>/dev/null || true; fi
  sleep 3
  if ! pid_alive "$NEW_TUNNEL_PID"; then
    log "ERROR transport candidate failed after handoff; restoring previous transport"
    nohup "$ROOT/bin/tunnel-client-runtime" run \
      --control-plane.api-key "file:$KEYFILE" \
      --mcp.server-url http://127.0.0.1:8765/mcp \
      --mcp.extra-headers "X-Bridge-Token: file:$ROUTER_TOKEN_FILE" \
      >"$ROOT/logs/tunnel.log" 2>&1 &
    echo $! >"$ROOT/bridge.pid"
    rm -f "$VERSIONED"
    exit 1
  fi

  # Preserve the old executable as an immediate rollback artifact before
  # replacing the stable path with a versioned symlink.
  if [[ -n "$CURRENT_TUNNEL" && ! -e "$ROOT/bin/tunnel-client-runtime-$CURRENT_TUNNEL" ]]; then
    cp -a "$ROOT/bin/tunnel-client-runtime" "$ROOT/bin/tunnel-client-runtime-$CURRENT_TUNNEL" 2>/dev/null || true
  fi
  rm -f "$ROOT/bin/tunnel-client-runtime"
  ln -s "$(basename "$VERSIONED")" "$ROOT/bin/tunnel-client-runtime"
  printf '%s\n' "$NEW_TUNNEL_PID" >"$ROOT/bridge.pid"
  log "TRANSPORT_UPDATE committed $PINNED_TUNNEL pid=$NEW_TUNNEL_PID"
fi

find "$RELEASES" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
  sort -nr | awk 'NR>3 {sub(/^[^ ]+ /,""); print}' |
  while IFS= read -r old; do [[ "$old" == "$(readlink -f "$ROOT/current" 2>/dev/null)" ]] || rm -rf "$old"; done

log "ok seamless runtime activation $TARGET; tunnel pid=$(cat "$ROOT/bridge.pid" 2>/dev/null || echo unknown)"
