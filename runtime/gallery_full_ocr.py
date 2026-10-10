#!/usr/bin/env python3
"""Complete Sonoryx gallery OCR coverage runner.

Author: Dāvids Krūmiņš.
Processes every current indexed image to a terminal OCR state (success or recorded
failure). Never modifies source photos. Resumes safely after interruption.
"""
from __future__ import annotations
import fcntl, json, os, time
from pathlib import Path
import gallery_bridge_tools as g

STATE=Path.home()/'.termux-mcp-bridge'
STATUS=STATE/'gallery_full_ocr_status.json'
LOCK=STATE/'gallery_full_ocr.lock'
PID=STATE/'gallery_full_ocr.pid'
ENABLED=STATE/'gallery_full_ocr.enabled'
LANG='rus+lav+eng'

def snapshot():
    with g._db() as c:
        total=c.execute('SELECT COUNT(*) FROM images').fetchone()[0]
        done=c.execute('''SELECT COUNT(*) FROM gallery_ocr_index o JOIN images i ON i.id=o.image_id
                          WHERE o.size=i.size AND o.mtime=i.mtime AND o.languages=?''',(LANG,)).fetchone()[0]
        ok=c.execute('''SELECT COUNT(*) FROM gallery_ocr_index o JOIN images i ON i.id=o.image_id
                        WHERE o.size=i.size AND o.mtime=i.mtime AND o.languages=? AND o.error IS NULL''',(LANG,)).fetchone()[0]
        failed=done-ok
    return total,done,ok,failed

def write_status(result, state='RUNNING'):
    total,done,ok,failed=snapshot()
    payload={"ok":True,"state":state,"total":total,"processed":done,"recognized":ok,"failed":failed,
             "remaining":max(0,total-done),"percent":round(done*100/total,2) if total else 100.0,
             "last_batch":result,"timestamp":time.time()}
    tmp=STATUS.with_name('.'+STATUS.name+'.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False)+'\n')
    tmp.chmod(0o600); os.replace(tmp,STATUS)
    return payload

def main():
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with LOCK.open('a+') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return 0
        PID.write_text(str(os.getpid())+'\n',encoding='ascii'); PID.chmod(0o600)
        ENABLED.touch(exist_ok=True); ENABLED.chmod(0o600)
        write_status({},'STARTING')
        try:
            while True:
                total,done,ok,failed=snapshot()
                if done>=total:
                    write_status({},'COMPLETE'); return 0
                result=g.gallery_ocr_index({"max_images":16,"max_seconds":150,
                                            "languages":LANG,"allow_on_battery":True})
                write_status(result,'RUNNING' if result.get('ok') else 'PAUSED')
                if not result.get('ok') or result.get('paused'):
                    time.sleep(60)
                    continue
                if not result.get('recognized_ids') and not result.get('errors'):
                    g.gallery_scan({})
                    total2,done2,_,_=snapshot()
                    if done2>=total2:
                        write_status({},'COMPLETE'); return 0
                    time.sleep(5)
                else:
                    time.sleep(0.2)
        finally:
            try:
                if PID.exists() and PID.read_text().strip()==str(os.getpid()):
                    PID.unlink()
            except OSError:
                pass

if __name__=='__main__':
    raise SystemExit(main())
