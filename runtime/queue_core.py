"""Durable SQLite task queue with a single leased worker per phone install.

Queued payloads are named tasks and validated local script paths, never raw argv
or API credentials. Interrupted non-idempotent scripts require explicit retry.
"""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid

try:
    from .execution_core import execute
except ImportError:
    from execution_core import execute

DB_TIMEOUT = 5.0
MAX_QUEUED = 1000
MAX_TASK_TIME = 600


def root_dir():
    return Path(os.environ.get("TERMUXBRIDGE_ROOT", Path.home() / "termux-mcp-bridge")).resolve()


def queue_db(root=None):
    root = Path(root) if root is not None else root_dir()
    folder = root / "state"
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / "queue.sqlite3"
    db = sqlite3.connect(path, timeout=DB_TIMEOUT)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS queue (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            script TEXT,
            priority INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            exit_code INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            result_code TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS q_pending ON queue(state,priority DESC,created_at)")
        db.commit()
        for file in [path, Path(str(path)+"-wal"), Path(str(path)+"-shm")]:
            if file.exists():
                file.chmod(0o600)
        return db
    except BaseException:
        db.close()
        raise


def safe_script(value, root=None):
    root = Path(root or root_dir()).resolve()
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("script path required")
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = root / p
    try:
        path = p.resolve(strict=True)
    except OSError as exc:
        raise ValueError("script does not exist") from exc
    if path.suffix != ".sh" or not path.is_file() or not path.is_relative_to(root):
        raise ValueError("script must be existing .sh inside TermuxBridge root")
    return str(path)


def submit(task, script=None, priority=5, *, root=None):
    if task not in {"health", "script"}:
        raise ValueError("task must be health or script")
    if not isinstance(priority,int) or not 0 <= priority <= 9:
        raise ValueError("priority must be 0..9")
    script = safe_script(script, root) if task == "script" else None
    if task == "health" and script is not None:
        raise ValueError("health task accepts no script")
    now = int(time.time())
    jid = uuid.uuid4().hex
    with contextlib.closing(queue_db(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute("SELECT COUNT(*) FROM queue WHERE state IN ('QUEUED','RUNNING')").fetchone()[0]
        if count >= MAX_QUEUED:
            raise RuntimeError("queue capacity reached")
        db.execute("INSERT INTO queue (id,task,script,priority,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (jid,task,script,priority,"QUEUED",now,now))
        db.commit()
    return {"id":jid,"state":"QUEUED","task":task,"priority":priority}


FIELDS = ("id","task","script","priority","state","created_at","updated_at",
          "started_at","finished_at","exit_code","attempts","cancel_requested","result_code")


def status(jid, *, root=None):
    if not isinstance(jid,str) or len(jid)!=32 or any(c not in "0123456789abcdef" for c in jid):
        raise ValueError("invalid task ID")
    with contextlib.closing(queue_db(root)) as db:
        row = db.execute("SELECT "+",".join(FIELDS)+" FROM queue WHERE id=?",(jid,)).fetchone()
        if not row:
            raise ValueError("unknown task")
        return dict(zip(FIELDS,row))


def listing(limit=25, *, root=None):
    if not isinstance(limit,int) or not 1<=limit<=100:
        raise ValueError("limit must be 1..100")
    with contextlib.closing(queue_db(root)) as db:
        rows=db.execute("SELECT "+",".join(FIELDS)+" FROM queue ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
        summary=dict(db.execute("SELECT state,COUNT(*) FROM queue GROUP BY state").fetchall())
        return {"tasks":[dict(zip(FIELDS,r)) for r in rows],"counts":summary}


def cancel(jid, *, root=None):
    status(jid,root=root)
    now=int(time.time())
    with contextlib.closing(queue_db(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE queue SET state='CANCELLED',updated_at=?,finished_at=? WHERE id=? AND state='QUEUED'",
                   (now,now,jid))
        db.execute("UPDATE queue SET cancel_requested=1,updated_at=? WHERE id=? AND state='RUNNING'",(now,jid))
        db.commit()
    return status(jid,root=root)


def retry(jid, *, root=None):
    item=status(jid,root=root)
    if item["state"] not in ("NEEDS_REVIEW","FAILED","CANCELLED"):
        raise ValueError("retry only for NEEDS_REVIEW, FAILED, CANCELLED")
    with contextlib.closing(queue_db(root)) as db:
        db.execute("UPDATE queue SET state='QUEUED',cancel_requested=0,exit_code=NULL,result_code=NULL,updated_at=? WHERE id=? AND state=?",
                   (int(time.time()),jid,item["state"]))
        db.commit()
    return status(jid,root=root)


def recover(root=None):
    # Only the worker owning the exclusive filesystem lease calls this.
    with contextlib.closing(queue_db(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE queue SET state='QUEUED',updated_at=?,result_code='RECOVERED_IDEMPOTENT' WHERE state='RUNNING' AND task='health'",
                   (int(time.time()),))
        db.execute("UPDATE queue SET state='NEEDS_REVIEW',updated_at=?,result_code='INTERRUPTED_REVIEW_REQUIRED' WHERE state='RUNNING' AND task<>'health'",
                   (int(time.time()),))
        db.commit()


def claim(root=None):
    with contextlib.closing(queue_db(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT id,task,script FROM queue WHERE state='QUEUED' ORDER BY priority DESC,created_at,id LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE queue SET state='RUNNING',attempts=attempts+1,started_at=?,updated_at=? WHERE id=?",
                       (int(time.time()),int(time.time()),row[0]))
        db.commit()
        return row


def finish(jid, result, *, root=None):
    with contextlib.closing(queue_db(root)) as db:
        now=int(time.time())
        state="CANCELLED" if result.get("cancelled") else "FAILED" if result.get("exit_code") != 0 or result.get("timed_out") else "DONE"
        error="CANCELLED" if state=="CANCELLED" else "TIMEOUT" if result.get("timed_out") else "NONZERO_EXIT" if state=="FAILED" else "NONE"
        db.execute("UPDATE queue SET state=?,exit_code=?,result_code=?,finished_at=?,updated_at=? WHERE id=? AND state='RUNNING'",
                   (state,result.get("exit_code"),error,now,now,jid))
        db.commit()


def _task_argv(task, script, root):
    if task=="health":
        return [sys.executable,str(root/"current/maintenance_health.py"),str(root),"0","0"]
    return ["/data/data/com.termux/files/usr/bin/bash" if Path("/data/data/com.termux/files/usr/bin/bash").exists() else "/bin/bash",
            safe_script(script,root)]


def process_next(root=None):
    root=Path(root or root_dir())
    claimed=claim(root)
    if claimed is None:
        return False
    jid,task,script=claimed
    stopped=threading.Event()
    halt=threading.Event()

    def poll_cancel():
        while not halt.wait(.3):
            try:
                with contextlib.closing(queue_db(root)) as db:
                    item=db.execute("SELECT cancel_requested FROM queue WHERE id=?",(jid,)).fetchone()
                    if item and item[0]:
                        stopped.set()
                        return
            except sqlite3.Error:
                continue

    monitor=threading.Thread(target=poll_cancel,daemon=True)
    monitor.start()
    try:
        outcome=execute(_task_argv(task,script,root),str(root),timeout=MAX_TASK_TIME,
                        cpu_seconds=MAX_TASK_TIME,memory_mb=2048,max_output_chars=8192,cancel=stopped)
    except Exception:
        outcome={"exit_code":-1,"output":"","timed_out":False,"cancelled":stopped.is_set()}
    finally:
        halt.set()
        monitor.join(timeout=1)
    finish(jid,outcome,root=root)
    return True


def worker(root=None, *, stop=None):
    root=Path(root or root_dir())
    lock=root/"state/.queue-worker.lock"
    lock.parent.mkdir(parents=True,exist_ok=True)
    stop=stop or threading.Event()
    with lock.open("a+") as fp:
        while not stop.is_set():
            try:
                fcntl.flock(fp,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                stop.wait(1)
        if stop.is_set():
            return
        recover(root)
        while not stop.is_set():
            if not process_next(root):
                stop.wait(1)


def start_worker(root=None):
    thread=threading.Thread(target=worker,args=(root,),name="termuxbridge-sqlite-worker",daemon=True)
    thread.start()
    return thread
