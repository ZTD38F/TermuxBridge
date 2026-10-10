#!/usr/bin/env python3
"""Phase 2: deterministic Android autonomy, no cloud inference or new secrets."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

HOUR=3600
UPDATE_INTERVALS={"CHARGING":3600,"BATTERY_NORMAL":3*HOUR,
                  "BATTERY_LOW":6*HOUR,"UNKNOWN":3*HOUR}
CRITICAL_CODES={"MCP_BACKEND_DOWN","SUPERVISOR_DOWN","TUNNEL_DOWN",
                "DNS_PROXY_DOWN","QUEUE_STALLED","UPDATE_STALLED","STORAGE_CRITICAL",
                "VERSION_MISMATCH"}
ALERT_COOLDOWN=6*HOUR
QUEUE_HEARTBEAT_GRACE=120
QUEUE_DURATION_LIMIT=720
UPDATE_STALE_AFTER=900


def load(path):
    try:
        item=json.loads(Path(path).read_text(encoding="utf-8"))
        return item if isinstance(item,dict) else {}
    except (OSError,ValueError):
        return {}


def save(path,obj):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name("."+path.name+".tmp")
    tmp.write_text(json.dumps(obj,sort_keys=True,separators=(",",":"))+"\n")
    tmp.chmod(0o600)
    os.replace(tmp,path)


def battery_state(raw):
    if not isinstance(raw,dict):
        return {"mode":"UNKNOWN","percent":None}
    try:
        percent=int(raw["percentage"])
    except (KeyError,ValueError,TypeError):
        return {"mode":"UNKNOWN","percent":None}
    if not 0<=percent<=100:
        return {"mode":"UNKNOWN","percent":None}
    connected=str(raw.get("plugged","")).upper()
    mode=("CHARGING" if connected in {"PLUGGED_AC","PLUGGED_USB","PLUGGED_WIRELESS","AC","USB","WIRELESS"}
          or str(raw.get("status","")).upper() in {"CHARGING","FULL"} else
          "BATTERY_LOW" if percent<30 else "BATTERY_NORMAL")
    return {"mode":mode,"percent":percent}


def read_battery():
    executable=shutil.which("termux-battery-status")
    if not executable:
        return {"mode":"UNKNOWN","percent":None}
    try:
        res=subprocess.run([executable],capture_output=True,text=True,timeout=5,check=False)
        if res.returncode:
            return {"mode":"UNKNOWN","percent":None}
        return battery_state(json.loads(res.stdout))
    except (OSError,subprocess.TimeoutExpired,ValueError):
        return {"mode":"UNKNOWN","percent":None}


def update_policy(root,now=None, *, battery=None, force=False):
    root=Path(root)
    now=int(time.time() if now is None else now)
    b=battery if battery is not None else read_battery()
    mode=b["mode"]
    old=load(root/"state/energy.json")
    last=int(old.get("last_successful_update_check",0) or 0)
    interval=UPDATE_INTERVALS.get(mode,UPDATE_INTERVALS["UNKNOWN"])
    # Never defer first update because battery telemetry is absent.
    due=force or last<=0 or now-last>=interval or last>now+600
    result={"battery_mode":mode,"battery_percent":b.get("percent"),
            "network_check_due":bool(due),"check_interval_seconds":interval,
            "next_due_epoch":max(now,last+interval) if not due else now,
            "last_successful_update_check":last}
    save(root/"state/energy.json",result)
    return result


def mark_update_success(root,when=None):
    path=Path(root)/"state/energy.json"
    data=load(path)
    data["last_successful_update_check"]=int(time.time() if when is None else when)
    data["last_update_result"]="SUCCESS"
    save(path,data)


def hung_operations(root,now=None):
    root=Path(root)
    now=int(time.time() if now is None else now)
    issues=[]
    dbfile=root/"state/queue.sqlite3"
    if dbfile.is_file() and not dbfile.is_symlink():
        try:
            # Never create a database or modify work state during inspection.
            db=sqlite3.connect(f"file:{dbfile}?mode=ro",uri=True,timeout=2)
            try:
                rows=db.execute("SELECT id,started_at,updated_at FROM queue WHERE state='RUNNING' LIMIT 50").fetchall()
            finally:
                db.close()
            for _id,started,heartbeat in rows:
                if started is None:
                    continue
                duration=max(0,now-int(started))
                age=max(0,now-int(heartbeat or started))
                if duration>QUEUE_DURATION_LIMIT or (duration>QUEUE_HEARTBEAT_GRACE and age>QUEUE_HEARTBEAT_GRACE):
                    issues.append({"kind":"QUEUE_STALLED","id":str(_id)[:32],
                                   "duration_seconds":duration,"heartbeat_age_seconds":age})
        except (sqlite3.Error,OSError,ValueError):
            issues.append({"kind":"QUEUE_STATUS_UNAVAILABLE"})
    update=load(root/"state/update.json")
    phase=update.get("phase")
    if phase in {"CANDIDATE_STARTING","CHECKING","DOWNLOADED","VERIFIED_ARTIFACT",
                 "CANDIDATE_HEALTHY","SWITCHED","DRAINING_OLD","OBSERVING","VERIFIED"}:
        elapsed=now-int(update.get("updated_at") or now)
        if elapsed>UPDATE_STALE_AFTER:
            # Detection only: the existing transactional updater owns recovery.
            issues.append({"kind":"UPDATE_STALLED","duration_seconds":elapsed})
    return issues


def classify(snapshot,*,hung=None):
    checks=snapshot.get("local_processes") or {}
    issues=hung or []
    faults=[]
    if not checks.get("supervisor",True) or not checks.get("supervisor_http",True):
        faults.append("SUPERVISOR_DOWN")
    if not checks.get("mcp",True) or not checks.get("mcp_http",True):
        faults.append("MCP_BACKEND_DOWN")
    if not checks.get("watchdog",True) or not checks.get("tunnel",True):
        faults.append("TUNNEL_DOWN")
    if not checks.get("proxy",True):
        faults.append("DNS_PROXY_DOWN")
    if not snapshot.get("verified_active_generation",True):
        faults.append("VERSION_MISMATCH")
    if snapshot.get("updater_exit_code",0) not in (0,75):
        faults.append("UPDATE_FAILED")
    free=snapshot.get("free_storage_mb")
    if isinstance(free,int) and free<300:
        faults.append("STORAGE_CRITICAL")
    for issue in issues:
        if issue["kind"] not in faults:
            faults.append(issue["kind"])
    state=snapshot.get("tunnel_state")
    if state in {"NEEDS_ATTENTION","RETRYING","STOPPED"} and "TUNNEL_DOWN" not in faults:
        faults.append("TUNNEL_CONNECTION_UNCERTAIN")
    if not faults:
        return {"fault_code":"NONE","severity":"OK","faults":[],"recommended_action":"NONE"}
    severe=any(x in CRITICAL_CODES for x in faults)
    return {"fault_code":faults[0],"severity":"CRITICAL" if severe else "WARNING",
            "faults":faults,"recommended_action":(
                "VERIFY_LOCAL_SERVICES" if severe else "INSPECT_STATUS"),
            "remote_connectivity_verified":False}


def enrich(root,snapshot,now=None):
    issues=hung_operations(root,now)
    diagnostic=classify(snapshot,hung=issues)
    snapshot.update(diagnostic)
    snapshot["hung_operations"]=issues
    if issues and snapshot.get("result")=="OK":
        snapshot["result"]="DEGRADED"
        snapshot["reason"]="HUNG_OPERATION_DETECTED"
    return snapshot


def notification_plan(snapshot,previous,now=None):
    now=int(time.time() if now is None else now)
    code=snapshot.get("fault_code","NONE")
    count=snapshot.get("consecutive_failures",0)
    if code not in CRITICAL_CODES or not isinstance(count,int) or count<3:
        return {"send":False,"reason":"NOT_CONFIRMED"}
    last=int(previous.get("last_sent_epoch",0) or 0)
    unchanged=code==previous.get("last_code")
    if unchanged and now-last<ALERT_COOLDOWN:
        return {"send":False,"reason":"COOLDOWN"}
    return {"send":True,"reason":"CRITICAL","code":code}


def send_notification(root,now=None, *, run=subprocess.run):
    root=Path(root)
    now=int(time.time() if now is None else now)
    status=load(root/"state/maintenance.json")
    file=root/"state/notification_state.json"
    old=load(file)
    decision=notification_plan(status,old,now)
    if not decision["send"]:
        return decision
    executable=shutil.which("termux-notification")
    if not executable:
        return {"send":False,"reason":"TERMUX_API_UNAVAILABLE"}
    code=decision["code"]
    # Enumerated code only: never publish logs, commands, tokens, filenames.
    try:
        proc=run([executable,"--id","38039","--title","TermuxBridge: требуется внимание",
                  "--content",f"Критический сбой: {code}. Открой maintenance-status."],
                 timeout=8,capture_output=True,text=True,check=False)
    except (OSError,subprocess.TimeoutExpired):
        return {"send":False,"reason":"NOTIFICATION_FAILED"}
    if proc.returncode!=0:
        return {"send":False,"reason":"NOTIFICATION_FAILED"}
    save(file,{"last_sent_epoch":now,"last_code":code})
    return {"send":True,"reason":"SENT","code":code}


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if len(argv)!=2 or argv[0] not in {"policy","record-update","alerts","diagnose"}:
        raise SystemExit("Usage: autonomy_engine.py policy|record-update|alerts|diagnose ROOT")
    action,root=argv
    if action=="policy":
        plan=update_policy(root,force=os.environ.get("TERMUXBRIDGE_MANUAL_MAINTENANCE")=="1")
        print("RUN" if plan["network_check_due"] else "SKIP")
    elif action=="record-update":
        mark_update_success(root)
        print("RECORDED")
    elif action=="alerts":
        result=send_notification(root)
        print(result["reason"])
    else:
        status=load(Path(root)/"state/maintenance.json")
        print(json.dumps(enrich(root,status),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
