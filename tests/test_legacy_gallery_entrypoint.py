"""Regression for old TermuxService Android intents after photo addon split."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest

HERE=Path(__file__).resolve().parents[1]
MODULE=HERE/"runtime/gallery_maintenance.py"
spec=importlib.util.spec_from_file_location("legacy_gallery_compat",MODULE)
legacy=importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(legacy)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="legacy-gallery-job-")
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.root=self.home/".termux-mcp-bridge/addons/photo-intelligence"
        self.r=self.root/"releases/0.6.3"
        (self.r/"runtime").mkdir(parents=True)
        self.proc=self.home/"fake-proc"
        self.proc.mkdir()
        self.s=self.root/"state"
        self.s.mkdir()
        (self.r/"service.py").write_text("# synthetic only\n")
        (self.r/"runtime/pipeline_scheduler.py").write_text("# synthetic only\n")
        (self.root/"current").symlink_to(self.r)
        self.now=time.time()
        self.write_state()

    def _pid(self,pid,script):
        proc=self.proc/str(pid)
        proc.mkdir(exist_ok=True)
        (proc/"cmdline").write_bytes(b"python\0"+str(script).encode()+b"\0")

    def write_state(self,worker=9102,reported=9102,lock=9102):
        self._pid(9101,self.r/"service.py")
        self._pid(9102,self.r/"runtime/pipeline_scheduler.py")
        (self.s/"service_status.json").write_text(json.dumps({
            "state":"RUNNING","timestamp":self.now,"pid":9101,
            "children":{"pipeline":{"pid":worker,"running":True}}}))
        (self.s/"pipeline_status.json").write_text(json.dumps({
            "state":"DEFERRED_SOURCE","timestamp":self.now,"pid":reported}))
        (self.s/"pipeline_scheduler.pid").write_text(str(lock))

    def test_delegation_when_real_scheduler_owns_lock(self):
        before={p.relative_to(self.home):(p.stat().st_size,p.stat().st_mtime_ns)
                for p in self.home.rglob("*") if p.is_file()}
        result=legacy.inspect(self.home,proc_root=self.proc,now=self.now+5)
        self.assertEqual(result,{"ok":True,"mode":"delegated_to_photo_intelligence",
                                 "state":"DEFERRED_SOURCE"})
        after={p.relative_to(self.home):(p.stat().st_size,p.stat().st_mtime_ns)
               for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before,after,"compat must never write state or media")

    def test_stale_orphan_even_with_heartbeat_is_not_accepted(self):
        self.write_state(worker=9999)
        self.assertEqual(legacy.inspect(self.home,proc_root=self.proc,now=self.now)["reason"],
                         "PHOTO_INTELLIGENCE_PIPELINE_UNVERIFIED")

    def test_stale_heartbeat_rejected(self):
        self.assertFalse(legacy.inspect(self.home,proc_root=self.proc,now=self.now+121)["ok"])

    def test_wrong_process_identity_rejected(self):
        (self.proc/"9102/cmdline").write_bytes(b"python\0/tmp/evil-gallery.py\0")
        self.assertFalse(legacy.inspect(self.home,proc_root=self.proc,now=self.now)["ok"])

    def test_missing_addon_gives_clear_nonzero_reason(self):
        self.assertEqual(legacy.inspect(self.home/"another",proc_root=self.proc)["reason"],
                         "PHOTO_INTELLIGENCE_NOT_INSTALLED")

    def test_missing_pidfile_does_not_start_a_worker(self):
        (self.s/"pipeline_scheduler.pid").unlink()
        self.assertEqual(legacy.inspect(self.home,proc_root=self.proc,now=self.now),
                         {"ok":False,"reason":"PHOTO_INTELLIGENCE_PIPELINE_UNVERIFIED"})
        self.assertFalse((self.s/"pipeline_scheduler.pid").exists())

    def test_termuxservice_exec_contract(self):
        self.assertTrue(MODULE.read_text().startswith(
            "#!/data/data/com.termux/files/usr/bin/python3\n"))
        self.assertTrue(MODULE.stat().st_mode & stat.S_IXUSR)
        src=MODULE.read_text()
        self.assertNotIn("subprocess.",src)
        self.assertNotIn("os.kill(",src)
        self.assertNotIn("unlink(",src)


if __name__=="__main__":
    unittest.main()
