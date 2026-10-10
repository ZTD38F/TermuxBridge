"""Guarded last-known-good runtime rollback.

Only triggers after repeated *backend-specific* failure. Previous generation
must be the exact locally staged, previously verified release. Never signals an
unidentified process. A failed release is quarantined from automatic upgrades.
"""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request


def load(path):
    try:
        data=json.loads(Path(path).read_text())
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):
        return {}


def eligible(root, snapshot):
    if snapshot.get("consecutive_failures",0) < 3:
        return False
    processes=snapshot.get("local_processes",{})
    if not (processes.get("supervisor") and processes.get("supervisor_http")):
        return False
    if processes.get("mcp_http",True):
        return False
    if snapshot.get("reason") not in ("LOCAL_SERVICES_UNHEALTHY","STARTUP_OR_UPDATE_FAILED"):
        return False
    journal=load(root/"state/update.json")
    route=load(root/"state/route.json")
    generation=journal.get("current_generation")
    previous=journal.get("previous_generation")
    return (journal.get("phase")=="COMMITTED" and bool(previous)
            and previous!=generation and generation==route.get("generation")
            and (root/"current").resolve()==(root/"releases"/generation).resolve()
            and (root/"previous").resolve()==(root/"releases"/previous).resolve())


def save_json(path,object_):
    path=Path(path)
    temporary=path.with_name("."+path.name+".rollback-tmp")
    temporary.write_text(json.dumps(object_,sort_keys=True,separators=(",",":"))+"\n")
    temporary.chmod(0o600)
    os.replace(temporary,path)


def _probe(root,port,pid):
    token=(root/"secrets/backend_token").read_text().strip()
    request=urllib.request.Request(
        f"http://127.0.0.1:{port}/healthz",
        headers={"X-Bridge-Backend-Token":token})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=2) as rsp:
        obj=json.load(rsp)
    return obj.get("ok") is True and obj.get("pid")==pid


def _pid_verified(pid,script,port):
    p=Path("/proc")/str(pid)
    try:
        if p.stat().st_uid!=os.getuid():
            return False
        a=[os.fsdecode(x) for x in (p/"cmdline").read_bytes().split(b"\0") if x]
        return (str(script) in a and "--http" in a and
                a[a.index("--http")+1]==str(port))
    except (OSError,ValueError,IndexError):
        return False


def _choose_port(active):
    if active==18771:
        return 18772
    if active==18772:
        return 18771
    raise ValueError("unexpected active port")


def attempt(root=None):
    root=Path(root or Path.home()/"termux-mcp-bridge").resolve()
    lock=root/".update.lock"
    with lock.open("a+") as fp:
        try:
            fcntl.flock(fp,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {"result":"BUSY"}
        snapshot=load(root/"state/maintenance.json")
        if not eligible(root,snapshot):
            return {"result":"NOT_ELIGIBLE"}
        old_route=load(root/"state/route.json")
        current=old_route["generation"]
        prev=load(root/"state/update.json")["previous_generation"]
        prior=root/"releases"/prev
        active_dir=root/"releases"/current
        script=prior/"bridge_server.py"
        if (not script.is_file() or script.is_symlink() or
            not (root/"previous").is_symlink()):
            return {"result":"NO_VERIFIED_PREVIOUS"}
        port=_choose_port(old_route["port"])
        # Refuse unknown listeners; never kill another application to free a port.
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1",port))==0:
                return {"result":"PORT_OCCUPIED"}

        log=root/"logs"/("rollback-"+prev[:12]+".log")
        log.parent.mkdir(parents=True,exist_ok=True)
        env=os.environ.copy()
        env.update(TERMUX_BRIDGE_ROOT=str(Path.home()),
                   TERMUX_BRIDGE_JOBS=str(root/"jobs"),
                   TERMUX_BRIDGE_BACKEND_TOKEN_FILE=str(root/"secrets/backend_token"))
        proc=None
        switched=False
        try:
            with log.open("ab") as output:
                log.chmod(0o600)
                proc=subprocess.Popen([sys.executable,str(script),"--http",str(port)],
                                      env=env,stdin=subprocess.DEVNULL,
                                      stdout=output,stderr=subprocess.STDOUT,
                                      start_new_session=True,close_fds=True)
            for _ in range(35):
                if proc.poll() is not None:
                    break
                try:
                    if _probe(root,port,proc.pid):
                        break
                except (OSError,ValueError):
                    pass
                time.sleep(.2)
            else:
                return {"result":"CANDIDATE_UNHEALTHY"}
            if proc.poll() is not None or not _pid_verified(proc.pid,script,port):
                return {"result":"CANDIDATE_UNVERIFIED"}
            if not _probe(root,port,proc.pid):
                return {"result":"CANDIDATE_UNHEALTHY"}
            # Compare the previous backend's registered tools against its own
            # installed schema; no longer-running candidate is modified.
            old_pid=int((root/"server.pid").read_text())
            if load(root/"state/route.json")!=old_route:
                return {"result":"ROUTE_CHANGED"}
            save_json(root/"state/route.json",{"generation":prev,"port":port})
            switched=True
            if not _probe(root,port,proc.pid):
                raise RuntimeError("post-switch probe failed")
            # Atomic activation of the previously checksum-verified generation.
            tmp=root/".current.rollback-tmp"
            tmp.symlink_to(prior)
            os.replace(tmp,root/"current")
            previous_link=root/".previous.rollback-tmp"
            previous_link.symlink_to(active_dir)
            os.replace(previous_link,root/"previous")
            (root/"source_commit.rollback-tmp").write_text(prev+"\n")
            os.replace(root/"source_commit.rollback-tmp",root/"source_commit")
            (root/"server.pid.rollback-tmp").write_text(str(proc.pid)+"\n")
            os.replace(root/"server.pid.rollback-tmp",root/"server.pid")
            journal=load(root/"state/update.json")
            journal.update(phase="COMMITTED",current_generation=prev,
                           previous_generation=current,previous_port=old_route["port"],
                           candidate_generation=prev,candidate_port=port,
                           candidate_pid=proc.pid,previous_pid=old_pid,
                           rollback_reason="AUTOMATIC_BACKEND_FAILURE",
                           updated_at=int(time.time()))
            save_json(root/"state/update.json",journal)
            quarantine=load(root/"state/quarantine.json")
            quarantine.update({"generation":current,"reason":"AUTOMATIC_BACKEND_FAILURE",
                               "timestamp":int(time.time())})
            save_json(root/"state/quarantine.json",quarantine)
            if _pid_verified(old_pid,active_dir/"bridge_server.py",old_route["port"]):
                # Drain in-flight requests rather than killing an active MCP call.
                try:
                    token=(root/"secrets/router_token").read_text().strip()
                    req=urllib.request.Request("http://127.0.0.1:8765/__bridge/status",
                                               headers={"X-Bridge-Token":token})
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=3) as rsp:
                        in_flight=json.load(rsp).get("inflight",{}).get(current,0)
                    if in_flight==0:
                        os.kill(old_pid,signal.SIGTERM)
                except (OSError,ValueError):
                    pass
            return {"result":"ROLLED_BACK","active_generation":prev,
                    "quarantined_generation":current}
        except (OSError,RuntimeError,ValueError):
            if switched:
                # Original route remains intact if switching fails.
                save_json(root/"state/route.json",old_route)
            return {"result":"FAILED_SAFELY"}
        finally:
            if proc is not None and not switched and proc.poll() is None:
                if _pid_verified(proc.pid,script,port):
                    proc.terminate()


def main():
    result=attempt()
    print(json.dumps(result,separators=(",",":")))
    return 0 if result["result"] in {"NOT_ELIGIBLE","BUSY","ROLLED_BACK"} else 1


if __name__=="__main__":
    raise SystemExit(main())
