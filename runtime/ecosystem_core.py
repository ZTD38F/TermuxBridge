"""Local-only diagnostics, integrity-checked private backups and safe chaos tests.

Never packages the control-plane credentials, tunnel IDs, token files, phone
photos, shell history, application cookies, or raw logs. No network calls.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import zipfile

FORMAT=1
MAX_FILE=8*1024*1024
MAX_ARCHIVE=18*1024*1024
KEEP_BACKUPS=5
CONFIG_FILES=("state/energy.json","state/notification_state.json","state/adapter_policy.json")
QUEUE_FILE="state/queue.sqlite3"
FILE_ALLOWLIST=frozenset((*CONFIG_FILES,QUEUE_FILE))
RESTORE_ALLOWLIST=frozenset(CONFIG_FILES)


def safe_json(path):
    try:
        data=json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):
        return {}


def write_json(path,record):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name("."+path.name+".next")
    tmp.write_text(json.dumps(record,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp,path)


def _key(root):
    file=Path(root)/"secrets/router_token"
    if file.is_symlink():
        raise ValueError("backup key cannot be symlink")
    token=file.read_text(encoding="ascii").strip()
    if len(token)<32:
        raise ValueError("backup integrity key unavailable")
    return hashlib.sha256(b"TermuxBridge-local-backup-v1\x00"+token.encode()).digest()


def _source_data(root,relative):
    root=Path(root)
    file=root/relative
    if file.is_symlink() or not file.is_file():
        return None
    if file.stat().st_size>MAX_FILE:
        raise ValueError("backup source exceeds quota: "+relative)
    return file.read_bytes()


def _db_snapshot(root):
    file=Path(root)/QUEUE_FILE
    if not file.is_file() or file.is_symlink():
        return None
    with tempfile.TemporaryDirectory(prefix="bridge-queue-backup-") as folder:
        dst=Path(folder)/"queue.sqlite3"
        source=sqlite3.connect(f"file:{file}?mode=ro",uri=True,timeout=5)
        target=sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close();source.close()
        if dst.stat().st_size>MAX_FILE:
            raise ValueError("queue snapshot exceeds quota")
        return dst.read_bytes()


def _manifest_bytes(files):
    return json.dumps({"format":FORMAT,"files":files},sort_keys=True,separators=(",",":")).encode()


def backups_dir(root):
    folder=Path(root)/"backups"
    if folder.is_symlink():
        raise ValueError("backup folder cannot be symlink")
    folder.mkdir(parents=True,exist_ok=True)
    folder.chmod(0o700)
    return folder


def backup_create(root,*,clock=None):
    root=Path(root).resolve()
    key=_key(root)
    now=dt.datetime.now(dt.timezone.utc) if clock is None else clock
    folder=backups_dir(root)
    payload={}
    for name in CONFIG_FILES:
        data=_source_data(root,name)
        if data is not None:
            # State files are JSON only; do not accidentally archive arbitrary text.
            if not isinstance(json.loads(data),dict):
                raise ValueError("invalid backup JSON")
            payload[name]=data
    queue=_db_snapshot(root)
    if queue is not None:
        payload[QUEUE_FILE]=queue
    if not payload:
        raise ValueError("no supported configuration or queue state to back up")
    meta={name:{"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)}
          for name,data in sorted(payload.items())}
    signed=_manifest_bytes(meta)
    signature=hmac.new(key,signed,hashlib.sha256).hexdigest().encode()
    # Use nanosecond suffix to avoid collisions between concurrent manual runs.
    label=now.strftime("%Y%m%dT%H%M%SZ")+"-"+str(time.time_ns()%1_000_000_000).zfill(9)
    output=folder/("termuxbridge-"+label+".zip")
    temp=folder/("."+output.name+".next")
    try:
        with zipfile.ZipFile(temp,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            z.writestr("manifest.json",signed)
            z.writestr("manifest.hmac",signature)
            for name,data in payload.items():
                z.writestr("config/"+name,data)
        if temp.stat().st_size>MAX_ARCHIVE:
            raise ValueError("backup archive exceeds quota")
        temp.chmod(0o600)
        os.replace(temp,output)
    finally:
        temp.unlink(missing_ok=True)
    files=sorted(folder.glob("termuxbridge-*.zip"))
    for old in files[:-KEEP_BACKUPS]:
        if old.is_file() and not old.is_symlink():
            old.unlink()
    write_json(root/"state/backup_status.json",
               {"last_success_epoch":int(now.timestamp()),"last_archive":output.name,
                "file_count":len(meta),"result":"OK"})
    return {"name":output.name,"file_count":len(meta),"size_bytes":output.stat().st_size}


def backup_list(root):
    folder=backups_dir(root)
    out=[]
    for path in sorted(folder.glob("termuxbridge-*.zip"),reverse=True)[:KEEP_BACKUPS+4]:
        if path.is_file() and not path.is_symlink():
            out.append({"name":path.name,"bytes":path.stat().st_size})
    return out


def _selected_archive(root,name):
    if not isinstance(name,str) or not name.startswith("termuxbridge-") or not name.endswith(".zip") or "/" in name or "\\" in name:
        raise ValueError("invalid backup name")
    p=backups_dir(root)/name
    if not p.is_file() or p.is_symlink() or p.stat().st_size>MAX_ARCHIVE:
        raise ValueError("backup missing or too large")
    return p


def backup_verify(root,name):
    root=Path(root).resolve()
    file=_selected_archive(root,name)
    with zipfile.ZipFile(file,"r") as z:
        names=z.namelist()
        if len(names)!=len(set(names)):
            raise ValueError("duplicate archive members")
        if len(names)>len(FILE_ALLOWLIST)+2:
            raise ValueError("unexpected archive member count")
        if "manifest.json" not in names or "manifest.hmac" not in names:
            raise ValueError("backup signature missing")
        raw=z.read("manifest.json")
        sig=z.read("manifest.hmac").decode("ascii")
        if len(raw)>16384 or len(sig)!=64:
            raise ValueError("backup manifest exceeds limit")
        expected=hmac.new(_key(root),raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected):
            raise ValueError("backup integrity verification failed")
        manifest=json.loads(raw)
        if manifest.get("format")!=FORMAT or not isinstance(manifest.get("files"),dict):
            raise ValueError("unsupported backup format")
        files=manifest["files"]
        if not files or not set(files).issubset(FILE_ALLOWLIST):
            raise ValueError("backup contains disallowed source path")
        if set(names)!={"manifest.json","manifest.hmac",*("config/"+f for f in files)}:
            raise ValueError("unexpected archive files")
        for member in z.infolist():
            if member.file_size>MAX_FILE or member.filename.startswith("/") or ".." in Path(member.filename).parts:
                raise ValueError("unsafe backup entry")
        for name,info in files.items():
            data=z.read("config/"+name)
            if len(data)!=info.get("bytes") or hashlib.sha256(data).hexdigest()!=info.get("sha256"):
                raise ValueError("backup file checksum mismatch")
        return {"verified":True,"name":file.name,"files":sorted(files),
                "restorable":sorted(set(files)&RESTORE_ALLOWLIST)}


def backup_restore(root,name,*,confirm=False):
    root=Path(root).resolve()
    report=backup_verify(root,name)
    if not confirm:
        return {"dry_run":True,**report}
    file=_selected_archive(root,name)
    written=[]
    with zipfile.ZipFile(file,"r") as z:
        # Validate every candidate BEFORE writing any file; corrupted JSON
        # must never cause partial restoration of valid prior members.
        validated={}
        for rel in report["restorable"]:
            data=z.read("config/"+rel)
            if not isinstance(json.loads(data),dict):
                raise ValueError("restorable settings are not a JSON object")
            target=root/rel
            if target.is_symlink() or target.parent.is_symlink():
                raise ValueError("refusing symlink destination")
            validated[rel]=data
        for rel,data in validated.items():
            target=root/rel
            target.parent.mkdir(parents=True,exist_ok=True)
            tmp=target.with_name("."+target.name+".restore-next")
            tmp.write_bytes(data)
            tmp.chmod(0o600)
            os.replace(tmp,target)
            written.append(rel)
    # Queue is intentionally NOT auto-restored into the live worker.
    return {"restored":written,"queue_restored":False,"verified":True}


def snapshot(root,*,adapters=None):
    root=Path(root)
    maintenance=safe_json(root/"state/maintenance.json")
    energy=safe_json(root/"state/energy.json")
    update=safe_json(root/"state/update.json")
    route=safe_json(root/"state/route.json")
    notification=safe_json(root/"state/notification_state.json")
    queue_summary={}
    dbfile=root/QUEUE_FILE
    if dbfile.is_file() and not dbfile.is_symlink():
        try:
            con=sqlite3.connect(f"file:{dbfile}?mode=ro",uri=True,timeout=2)
            try:
                queue_summary=dict(con.execute("SELECT state,COUNT(*) FROM queue GROUP BY state").fetchall())
            finally:
                con.close()
        except sqlite3.Error:
            queue_summary={"UNKNOWN":0}
    backup=safe_json(root/"state/backup_status.json")
    try:
        current=(root/"source_commit").read_text().strip()
    except OSError:
        current=""
    return {
        "bridge_version":current[:12],
        "health":maintenance.get("result","UNKNOWN"),
        "fault":maintenance.get("fault_code","UNKNOWN"),
        "severity":maintenance.get("severity","UNKNOWN"),
        "last_checked":maintenance.get("last_checked_at"),
        "tunnel_state":maintenance.get("tunnel_state","UNKNOWN"),
        "active_generation_matches":current==route.get("generation")==update.get("current_generation"),
        "update_phase":update.get("phase","UNKNOWN"),
        "battery_mode":energy.get("battery_mode","UNKNOWN"),
        "battery_percent":energy.get("battery_percent"),
        "check_interval_seconds":energy.get("check_interval_seconds"),
        "storage_free_mb":maintenance.get("free_storage_mb"),
        "queue":queue_summary,
        "adapters":adapters or {},
        "backups":len(backup_list(root)),
        "last_backup_epoch":backup.get("last_success_epoch"),
        "alerts_active":maintenance.get("severity")=="CRITICAL",
        "remote_connectivity_verified":False,
    }


def render_dashboard(info):
    # Pure static HTML, no CDN, network requests, cookies or scripts.
    def val(x):
        return html.escape(str(x) if x is not None else "—",quote=True)
    rows=[
        ("Состояние",info["health"]),("Ошибка",info["fault"]),
        ("Туннель",info["tunnel_state"]),("Проверка",info["last_checked"]),
        ("Релиз",info["bridge_version"]),("Коммит согласован",info["active_generation_matches"]),
        ("Обновление",info["update_phase"]),("Батарея",str(info["battery_percent"])+"% / "+str(info["battery_mode"])),
        ("Свободно на диске",str(info["storage_free_mb"])+" МБ"),
        ("Резервных копий",info["backups"]),
        ("Удалённое подтверждение","Не подтверждено этой панелью"),
    ]
    fields="".join('<div class="metric"><span>'+val(a)+'</span><strong>'+val(b)+'</strong></div>' for a,b in rows)
    adapters="".join('<div class="metric"><span>'+val(k)+'</span><strong>'+val(v.get("status","UNKNOWN"))+' · ABI '+val(v.get("abi","—"))+'</strong></div>'
                     for k,v in info["adapters"].items())
    queue="".join('<div class="metric"><span>'+val(k)+'</span><strong>'+val(v)+'</strong></div>'
                  for k,v in info["queue"].items())
    return """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TermuxBridge · Ecosystem</title><style>
:root{color-scheme:dark;font-family:Inter,system-ui,-apple-system,sans-serif;background:#0c111b;color:#f2f5fa}
*{box-sizing:border-box}body{margin:0;padding:32px 18px}main{max-width:920px;margin:auto}
header{padding:14px 0 24px}small{color:#9ca7bd}h1{font-size:30px;letter-spacing:-.04em;margin:8px 0}
h2{font-size:16px;letter-spacing:-.02em;margin:0 0 16px}
p{color:#9ca7bd;line-height:1.6}section{background:#151d2b;border:1px solid #293548;
border-radius:18px;padding:24px;margin:16px 0} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}
.metric{border:1px solid #2b3950;border-radius:12px;padding:14px;display:grid;gap:6px}
.metric span{color:#96a6c1;font-size:12px}.metric strong{font-size:15px;overflow-wrap:anywhere}
footer{font-size:12px;padding:20px 0;color:#93a2bb}</style>
<main><header><small>LOCAL · PRIVACY FIRST</small><h1>TermuxBridge / Ecosystem</h1>
<p>Локальный снимок состояния устройства. Секреты, логи и личные файлы здесь не отображаются.</p></header>
<section><h2>Общая диагностика</h2><div class="grid">""" + fields + """</div></section>
<section><h2>Адаптеры</h2><div class="grid">""" + (adapters or "<p>Нет доступной телеметрии</p>") + """</div></section>
<section><h2>Очередь SQLite</h2><div class="grid">""" + (queue or "<p>Нет данных</p>") + """</div></section>
<footer>Отчёт статический. Для актуального состояния выполните termuxbridgectl dashboard.</footer></main></html>"""


def dashboard(root,*,adapters=None,html_file=False):
    info=snapshot(root,adapters=adapters)
    if html_file:
        path=Path(root)/"state/dashboard.html"
        temp=path.with_name(".dashboard.next")
        temp.write_text(render_dashboard(info),encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp,path)
        return {"path":str(path),"snapshot":info}
    return info


def auto_backup(root,*,now=None):
    root=Path(root)
    m=safe_json(root/"state/maintenance.json")
    if m.get("result")!="OK" or (m.get("free_storage_mb") or 0)<500:
        return {"result":"SKIPPED_UNHEALTHY_OR_LOW_STORAGE"}
    last=int(safe_json(root/"state/backup_status.json").get("last_success_epoch",0))
    now=int(time.time() if now is None else now)
    if now-last<86400:
        return {"result":"NOT_DUE"}
    try:
        record=backup_create(root)
        return {"result":"OK",**record}
    except (OSError,ValueError,sqlite3.Error,zipfile.BadZipFile):
        return {"result":"FAILED"}


def chaos_selftest():
    # Strictly synthetic. No real PIDs, network, installed paths or subprocess.
    try:
        from .adapter_runtime import AdapterRegistry
    except ImportError:
        from adapter_runtime import AdapterRegistry
    with tempfile.TemporaryDirectory(prefix="termuxbridge-chaos-") as temp:
        root=Path(temp)
        (root/"secrets").mkdir()
        (root/"secrets/router_token").write_text("A"*64)
        (root/"state").mkdir()
        (root/"state/energy.json").write_text('{"battery_mode":"BATTERY_NORMAL"}')
        reg=AdapterRegistry()
        class Fake:
            pass
        def unstable(_):
            raise RuntimeError("deliberate failure")
        def healthy(_):
            return {"ok":True}
        reg.register("google",Fake(),{"google_fake":({"type":"object"},unstable)})
        reg.register("gallery",Fake(),{"gallery_fake":({"type":"object"},healthy)})
        for _ in range(3):
            try:reg.invoke("google","google_fake",{})
            except RuntimeError:pass
        adapter_isolated=reg.status()["google"]["isolated"]
        healthy_works=reg.invoke("gallery","gallery_fake",{})["ok"]
        archive=backup_create(root)["name"]
        verified=backup_verify(root,archive)["verified"]
        with zipfile.ZipFile(backups_dir(root)/archive,"a") as z:
            z.writestr("extra.txt","unexpected")
        tampering_rejected=False
        try:backup_verify(root,archive)
        except ValueError:tampering_rejected=True
        return {"adapter_isolated":adapter_isolated,"healthy_adapter_available":healthy_works,
                "backup_verified":verified,"corrupted_archive_rejected":tampering_rejected,
                "real_services_touched":False}
