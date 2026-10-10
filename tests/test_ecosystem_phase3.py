"""Ecosystem Phase 3: adapter isolation, dashboard, privacy backups, chaos tests."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from runtime import adapter_runtime as ad
from runtime import ecosystem_core as core
from runtime import ecosystem_cli as cli


class EcosystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/"secrets").mkdir()
        (self.root/"secrets/router_token").write_text("S"*64)
        (self.root/"state").mkdir()
        (self.root/"state/energy.json").write_text('{"battery_mode":"BATTERY_NORMAL"}')
        (self.root/"state/notification_state.json").write_text('{"last_code":"NONE"}')
        (self.root/"state/adapter_policy.json").write_text('{"schema":1}')
        (self.root/"state/maintenance.json").write_text(
            '{"result":"OK","fault_code":"NONE","severity":"OK",'
            '"free_storage_mb":4096,"tunnel_state":"CLIENT_RUNNING"}')
        (self.root/"source_commit").write_text("a"*40)
        db=self.root/"state/queue.sqlite3"
        with sqlite3.connect(db) as sql:
            sql.execute("CREATE TABLE queue(id TEXT,state TEXT)")
            sql.execute("INSERT INTO queue VALUES ('test','DONE')")

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_compatibility_contract(self):
        for name in ad.BUILTINS:
            self.assertTrue(ad.AdapterRegistry.validate(name,"1.0.0",1,1))
        for version,abi,major in [("2.0.0",1,1),("x",1,1),("1.0.0",2,1),("1.0.0",1,2)]:
            with self.assertRaises(ValueError):
                ad.AdapterRegistry.validate("google",version,abi,major)
        with self.assertRaises(ValueError):
            ad.AdapterRegistry.validate("unknown","1.0.0",1,1)

    def test_faulty_adapter_does_not_break_others(self):
        reg=ad.AdapterRegistry()
        class Fake: pass
        def bad(_):raise RuntimeError("sensitive error text that must not be published")
        reg.register("google",Fake(),{"google_test":({"type":"object"},bad)})
        reg.register("gallery",Fake(),{"gallery_test":({"type":"object"},lambda _:{"ok":True})})
        for _ in range(3):
            with self.assertRaisesRegex(RuntimeError,"adapter operation failed|temporarily isolated"):
                reg.invoke("google","google_test",{})
        self.assertTrue(reg.status()["google"]["isolated"])
        self.assertEqual(reg.invoke("gallery","gallery_test",{}),{"ok":True})
        self.assertEqual(reg.status()["gallery"]["status"],"READY")

    def test_adapter_loader_import_failure_isolated(self):
        mod=self.root/"broken.py"
        mod.write_text('raise RuntimeError("failure with SECRET")\n')
        reg=ad.AdapterRegistry()
        self.assertIsNone(reg.load("google",mod))
        self.assertEqual(reg.status()["google"]["status"],"FAILED_LOAD")
        self.assertEqual(reg.status()["gallery"]["status"],"NOT_INSTALLED")
        self.assertNotIn("SECRET",json.dumps(reg.status()))

    def test_adapter_tool_namespace_validation(self):
        reg=ad.AdapterRegistry()
        with self.assertRaises(ValueError):
            reg.register("gallery",object(),{"google_steal":({"type":"object"},lambda a:a)})
        with self.assertRaises(ValueError):
            reg.register("phone",object(),{"bad.name":({"type":"object"},lambda a:a)})
        reg.register("phone",object(),{"get_device_state":({"type":"object"},lambda _:True)})
        self.assertTrue(reg.invoke("phone","phone_get_device_state",{}))

    def test_backup_contains_only_allowlisted_state(self):
        (self.root/"secrets/control_plane_api_key").write_text("NEVER_BACKUP_THIS_SECRET")
        (self.root/"logs").mkdir()
        (self.root/"logs/error.log").write_text("SENSITIVE_LOG")
        rec=core.backup_create(self.root)
        file=self.root/"backups"/rec["name"]
        self.assertEqual(file.stat().st_mode & 0o777,0o600)
        report=core.backup_verify(self.root,rec["name"])
        self.assertTrue(report["verified"])
        self.assertIn("state/queue.sqlite3",report["files"])
        raw=file.read_bytes()
        self.assertNotIn(b"NEVER_BACKUP_THIS_SECRET",raw)
        self.assertNotIn(b"SENSITIVE_LOG",raw)
        self.assertNotIn(b"router_token",raw)

    def test_backup_queue_snapshot_consistent(self):
        name=core.backup_create(self.root)["name"]
        file=self.root/"backups"/name
        with zipfile.ZipFile(file) as z:
            db=z.read("config/state/queue.sqlite3")
        snap=self.root/"queue-copy.sqlite3"
        snap.write_bytes(db)
        with sqlite3.connect(f"file:{snap}?mode=ro",uri=True) as connection:
            self.assertEqual(connection.execute("SELECT state FROM queue").fetchone()[0],"DONE")

    def test_backup_dry_run_and_safe_restore(self):
        record=core.backup_create(self.root)
        original=(self.root/"state/energy.json").read_text()
        (self.root/"state/energy.json").write_text('{"battery_mode":"BATTERY_LOW"}')
        dry=core.backup_restore(self.root,record["name"])
        self.assertTrue(dry["dry_run"])
        self.assertIn("BATTERY_LOW",(self.root/"state/energy.json").read_text())
        result=core.backup_restore(self.root,record["name"],confirm=True)
        self.assertEqual((self.root/"state/energy.json").read_text(),original)
        self.assertFalse(result["queue_restored"])

    def test_tampered_backup_and_wrong_key_rejected(self):
        name=core.backup_create(self.root)["name"]
        (self.root/"secrets/router_token").write_text("DIFFERENT"*8)
        with self.assertRaises(ValueError):
            core.backup_verify(self.root,name)
        (self.root/"secrets/router_token").write_text("S"*64)
        file=self.root/"backups"/name
        with zipfile.ZipFile(file,"a") as z:
            z.writestr("config/../../secrets/evil","malicious")
        with self.assertRaises(ValueError):
            core.backup_verify(self.root,name)
        self.assertFalse((self.root/"secrets/evil").exists())

    def test_backup_rejects_symlinks_and_arbitrary_archive_names(self):
        with self.assertRaises(ValueError):
            core.backup_verify(self.root,"../../secrets/router_token")
        path=self.root/"state/energy.json"
        path.unlink()
        path.symlink_to(self.root/"secrets/router_token")
        record=core.backup_create(self.root)
        self.assertNotIn("state/energy.json",core.backup_verify(self.root,record["name"])["files"])

    def test_backup_retention_and_daily_schedule(self):
        now=datetime(2026,10,10,tzinfo=timezone.utc)
        for _ in range(7):
            core.backup_create(self.root,clock=now)
        self.assertEqual(len(core.backup_list(self.root)),5)
        self.assertEqual(core.auto_backup(self.root,now=int(now.timestamp())+3600)["result"],"NOT_DUE")
        self.assertEqual(core.auto_backup(self.root,now=int(now.timestamp())+90000)["result"],"OK")
        (self.root/"state/maintenance.json").write_text('{"result":"DEGRADED","free_storage_mb":100}')
        self.assertEqual(core.auto_backup(self.root)["result"],"SKIPPED_UNHEALTHY_OR_LOW_STORAGE")

    def test_dashboard_is_static_escaped_and_private(self):
        adapters={"evil<script>":{"status":"READY","abi":1}}
        rec=core.dashboard(self.root,adapters=adapters,html_file=True)
        page=Path(rec["path"]).read_text()
        self.assertIn("evil&lt;script&gt;",page)
        self.assertNotIn("<script>",page)
        self.assertNotIn("https://",page)
        self.assertIn("TermuxBridge / Ecosystem",page)
        self.assertEqual(Path(rec["path"]).stat().st_mode & 0o777,0o600)
        self.assertEqual(rec["snapshot"]["queue"]["DONE"],1)
        self.assertFalse(rec["snapshot"]["remote_connectivity_verified"])

    def test_chaos_suite_never_touches_live_services(self):
        report=core.chaos_selftest()
        self.assertTrue(report["adapter_isolated"])
        self.assertTrue(report["healthy_adapter_available"])
        self.assertTrue(report["backup_verified"])
        self.assertTrue(report["corrupted_archive_rejected"])
        self.assertFalse(report["real_services_touched"])

    def test_cli_commands_are_defined(self):
        source=Path("termuxbridgectl").read_text()
        for name in ("dashboard","dashboard-html","backup-create","backup-list",
                     "backup-verify","backup-restore","chaos-test"):
            self.assertIn("  "+name+")",source)
        self.assertEqual(cli.main(["chaos-test",str(self.root)]),0)


if __name__=="__main__":
    unittest.main()
