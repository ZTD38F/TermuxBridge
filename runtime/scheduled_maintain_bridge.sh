#!/data/data/com.termux/files/usr/bin/bash
# JobScheduler-only entrypoint: records execution without the broken --pending API.
# Author: @dvkrum
set -Eeuo pipefail
umask 077
ROOT="${TERMUXBRIDGE_ROOT:-$HOME/termux-mcp-bridge}"
STATE="$ROOT/state"
mkdir -p "$STATE" "$ROOT/logs"
chmod 700 "$STATE"
[[ -x "$ROOT/maintain_bridge.sh" ]] || exit 127
request_id="$(python - "$STATE/auto_update.json" <<'PY_REQUEST'
import json,sys
try:
    with open(sys.argv[1],encoding="utf-8") as f:
        request=json.load(f)
    print(request.get("request_id","") if request.get("enabled") is True else "")
except (OSError,ValueError,TypeError):
    print("")
PY_REQUEST
)"
started_at="$(date +%s)"
rc=0
"$ROOT/maintain_bridge.sh" >> "$ROOT/logs/scheduled_maintenance.log" 2>&1 || rc=$?
python - "$STATE/scheduled_maintenance.json" "$request_id" "$started_at" "$rc" <<'PY_RESULT'
import json,os,sys,tempfile,time
path,request_id,started_at,rc=sys.argv[1:]
payload={"job_id":38038,"request_id":request_id,
         "started_at":int(started_at),"finished_at":time.time(),"exit_code":int(rc)}
fd,tmp=tempfile.mkstemp(prefix=".scheduled-maintenance.",dir=os.path.dirname(path))
try:
    with os.fdopen(fd,"w",encoding="utf-8") as f:
        json.dump(payload,f,sort_keys=True);f.write("\n");f.flush();os.fsync(f.fileno())
    os.chmod(tmp,0o600);os.replace(tmp,path)
finally:
    if os.path.exists(tmp):os.unlink(tmp)
PY_RESULT
exit "$rc"
