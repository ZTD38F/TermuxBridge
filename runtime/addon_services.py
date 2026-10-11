"""Start/stop locally installed TermuxBridge addon services safely."""
from __future__ import annotations
import argparse,json,os,signal,subprocess,sys,time
from pathlib import Path

def identity(pid):
    try:
        p=Path('/proc')/str(int(pid));
        if p.stat().st_uid!=os.getuid():return None
        args=[x.decode(errors='replace') for x in (p/'cmdline').read_bytes().split(b'\0') if x]
        fields=(p/'stat').read_text().rsplit(') ',1)[1].split()
        if fields[0]=='Z':return None
        return {'pid':pid,'argv':args,'start_ticks':int(fields[19])}
    except Exception:return None

def load(addon_dir):
    current=(addon_dir/'current').resolve()
    if not current.is_dir() or not current.is_relative_to(addon_dir.resolve()):return None
    manifest=current/'addon.json'
    if not manifest.is_file():return None
    try:data=json.loads(manifest.read_text())
    except Exception:return None
    service=data.get('service')
    if not isinstance(service,str) or Path(service).name!=service:return None
    script=current/service
    if not script.is_file():return None
    return data,current,script

def live_managed(addon_dir,service_name):
    pidfile=addon_dir/'state/service.pid'
    try:pid=int(pidfile.read_text().strip())
    except Exception:return None
    ident=identity(pid)
    if not ident:return None
    if len(ident['argv'])<2:return None
    script=Path(ident['argv'][1]).resolve()
    if script.name!=service_name or not script.is_relative_to(addon_dir.resolve()):return None
    return ident

def start_one(addon_dir):
    loaded=load(addon_dir)
    if not loaded:return {'name':addon_dir.name,'state':'NOT_STARTABLE'}
    data,current,script=loaded; state=addon_dir/'state';logs=addon_dir/'logs';state.mkdir(parents=True,exist_ok=True);logs.mkdir(parents=True,exist_ok=True)
    ident=live_managed(addon_dir,script.name)
    if ident:
        active_script=Path(ident['argv'][1]).resolve()
        if active_script==script.resolve():return {'name':data['name'],'state':'RUNNING','pid':ident['pid']}
        os.kill(ident['pid'],signal.SIGTERM)
        for _ in range(40):
            if identity(ident['pid']) is None:break
            time.sleep(.2)
        if identity(ident['pid']) is not None:return {'name':data['name'],'state':'OLD_VERSION_STILL_RUNNING','pid':ident['pid']}
    log=(logs/'service-launch.log').open('ab',buffering=0);env=os.environ.copy();env['TERMUXBRIDGE_ADDON_HOME']=str(addon_dir);env['TERMUXBRIDGE_ADDON_CURRENT']=str(current)
    p=subprocess.Popen([sys.executable,str(script)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,env=env)
    time.sleep(.5);return {'name':data['name'],'state':'STARTED' if identity(p.pid) else 'FAILED','pid':p.pid}

def stop_one(addon_dir):
    loaded=load(addon_dir); service_name=loaded[2].name if loaded else 'service.py';ident=live_managed(addon_dir,service_name)
    if not ident:return {'name':addon_dir.name,'state':'STOPPED'}
    os.kill(ident['pid'],signal.SIGTERM)
    for _ in range(40):
        if identity(ident['pid']) is None:break
        time.sleep(.2)
    return {'name':addon_dir.name,'state':'STOPPED' if identity(ident['pid']) is None else 'STOPPING','pid':ident['pid']}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['start','stop','status','claims']);ap.add_argument('--root',default=str(Path.home()/'.termux-mcp-bridge/addons'));ap.add_argument('--namespace',default='');args=ap.parse_args();root=Path(args.root);results=[]
    if args.action=='claims':
        claimed=False
        if root.is_dir():
            for addon in root.iterdir():
                loaded=load(addon) if addon.is_dir() else None
                if loaded and loaded[0].get('namespace')==args.namespace: claimed=True; break
        print('yes' if claimed else 'no'); return
    if root.is_dir():
        for addon in sorted(root.iterdir()):
            if not addon.is_dir():continue
            if args.action=='start':results.append(start_one(addon))
            elif args.action=='stop':results.append(stop_one(addon))
            else:
                loaded=load(addon);ident=live_managed(addon,loaded[2].name if loaded else 'service.py');results.append({'name':addon.name,'state':'RUNNING' if ident else 'STOPPED','pid':ident['pid'] if ident else None})
    print(json.dumps({'ok':True,'addons':results},separators=(',',':')))
if __name__=='__main__':main()
