"""Phase 2 autonomous maintenance tests; no changes to the active phone."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from runtime import autonomy_engine as a

ROOT=Path(__file__).resolve().parents[1]


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/"state").mkdir()
    def tearDown(self):
        self.temp.cleanup()

    def test_classifies_component_failures_without_leaking_logs(self):
        good={"supervisor":True,"supervisor_http":True,"mcp":True,"mcp_http":True,
              "tunnel":True,"watchdog":True,"proxy":True}
        base={"local_processes":good,"verified_active_generation":True,
              "updater_exit_code":0,"free_storage_mb":900,"tunnel_state":"CLIENT_RUNNING"}
        self.assertEqual(a.classify(base)["fault_code"],"NONE")
        backend={**base,"local_processes":{**good,"mcp_http":False}}
        self.assertEqual(a.classify(backend)["fault_code"],"MCP_BACKEND_DOWN")
        self.assertEqual(a.classify(backend)["severity"],"CRITICAL")
        self.assertEqual(a.classify({**base,"updater_exit_code":19})["fault_code"],"UPDATE_FAILED")
        self.assertEqual(a.classify({**base,"free_storage_mb":100})["fault_code"],"STORAGE_CRITICAL")
        self.assertEqual(a.classify({**base,"verified_active_generation":False})["fault_code"],"VERSION_MISMATCH")
        offline={**base,"local_processes":{**good,"tunnel":False}}
        self.assertEqual(a.classify(offline)["fault_code"],"TUNNEL_DOWN")
        self.assertFalse(a.classify(backend)["remote_connectivity_verified"])

    def test_adaptive_power_respects_battery_and_first_run(self):
        at=10_000_000
        low={"mode":"BATTERY_LOW","percent":20}
        first=a.update_policy(self.root,now=at,battery=low)
        self.assertTrue(first["network_check_due"])
        a.mark_update_success(self.root,when=at)
        within=a.update_policy(self.root,now=at+4*3600,battery=low)
        self.assertFalse(within["network_check_due"])
        at_six=a.update_policy(self.root,now=at+6*3600,battery=low)
        self.assertTrue(at_six["network_check_due"])
        charged={"mode":"CHARGING","percent":80}
        self.assertTrue(a.update_policy(self.root,now=at+3600,battery=charged)["network_check_due"])
        self.assertEqual(a.battery_state({"plugged":"UNPLUGGED","status":"DISCHARGING","percentage":12})["mode"],"BATTERY_LOW")
        self.assertEqual(a.battery_state({"plugged":"PLUGGED_USB","percentage":12})["mode"],"CHARGING")
        self.assertTrue(a.update_policy(self.root,now=at+3600,battery=low,force=True)["network_check_due"])

    def test_missing_android_battery_falls_back_safely(self):
        self.assertEqual(a.battery_state({"percentage":"unknown"})["mode"],"UNKNOWN")
        self.assertEqual(a.battery_state({})["mode"],"UNKNOWN")
        self.assertEqual(a.read_battery().__class__,dict)

    def test_heartbeat_stalls_and_age_threshold(self):
        db=self.root/"state/queue.sqlite3"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE queue (id TEXT, state TEXT, started_at INTEGER, updated_at INTEGER)")
            conn.execute("INSERT INTO queue VALUES (?,?,?,?)",("a"*32,"RUNNING",1000,1000))
        self.assertEqual(a.hung_operations(self.root,now=1100),[])
        issues=a.hung_operations(self.root,now=1150)
        self.assertEqual(issues[0]["kind"],"QUEUE_STALLED")
        self.assertEqual(len(issues[0]["id"]),32)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE queue SET updated_at=1700")
        self.assertEqual(a.hung_operations(self.root,now=1800)[0]["kind"],"QUEUE_STALLED")
        snapshot={"result":"OK","reason":"NONE","local_processes":{},
                  "verified_active_generation":True}
        result=a.enrich(self.root,snapshot,now=1800)
        self.assertEqual(result["result"],"DEGRADED")
        self.assertEqual(result["reason"],"HUNG_OPERATION_DETECTED")

    def test_transaction_stall_only_detected_not_modified(self):
        file=self.root/"state/update.json"
        file.write_text(json.dumps({"phase":"DRAINING_OLD","updated_at":1}))
        issues=a.hung_operations(self.root,now=1000)
        self.assertEqual(issues[0]["kind"],"UPDATE_STALLED")
        self.assertEqual(json.loads(file.read_text())["phase"],"DRAINING_OLD")

    def test_notifications_suppressed_until_three_failures(self):
        state={"fault_code":"MCP_BACKEND_DOWN","severity":"CRITICAL",
               "consecutive_failures":2}
        self.assertFalse(a.notification_plan(state,{},now=10000)["send"])
        state["consecutive_failures"]=3
        first=a.notification_plan(state,{},now=10000)
        self.assertTrue(first["send"])
        old={"last_code":"MCP_BACKEND_DOWN","last_sent_epoch":10000}
        self.assertEqual(a.notification_plan(state,old,now=10010)["reason"],"COOLDOWN")
        self.assertTrue(a.notification_plan(state,old,now=10000+a.ALERT_COOLDOWN)["send"])
        self.assertFalse(a.notification_plan({"fault_code":"NONE","consecutive_failures":9},old,now=999999)["send"])

    def test_notifications_fail_closed_and_private(self):
        status=self.root/"state/maintenance.json"
        status.write_text(json.dumps({"fault_code":"MCP_BACKEND_DOWN",
                                      "consecutive_failures":3}))
        class Fake:
            returncode=0
        calls=[]
        def fake(args,**kwargs):
            calls.append(args)
            return Fake()
        with patch.object(a.shutil,"which",return_value="/usr/bin/termux-notification"):
            result=a.send_notification(self.root,now=10_000,run=fake)
        self.assertTrue(result["send"])
        self.assertEqual(len(calls),1)
        self.assertNotIn("secret",json.dumps(calls))
        self.assertEqual(a.send_notification(self.root,now=10_001,run=fake)["reason"],"COOLDOWN")
        status.write_text(json.dumps({"fault_code":"SUPERVISOR_DOWN","consecutive_failures":3}))
        class Failure:
            returncode=1
        with patch.object(a.shutil,"which",return_value="/usr/bin/termux-notification"):
            self.assertEqual(a.send_notification(self.root,now=10_002,run=lambda *x,**y:Failure())["reason"],"NOTIFICATION_FAILED")
        self.assertEqual(a.load(self.root/"state/notification_state.json")["last_code"],"MCP_BACKEND_DOWN")

    def test_boot_integration_is_idempotent_and_preserves_custom_scripts(self):
        script=(ROOT/"runtime/boot_bridge.sh").read_text()
        self.assertIn("TermuxBridge managed boot hook",script)
        self.assertIn('"$ROOT/start_bridge.sh"',script)
        self.assertIn("TERMUXBRIDGE_BOOT_TEST",script)
        self.assertIn('termuxbridgectl auto-update-enable',script)
        updater=(ROOT/"update_termux.sh").read_text()
        self.assertIn("custom Termux:Boot hook exists; left unchanged",updater)
        self.assertIn('boot_target="$HOME/.termux/boot/20-termuxbridge.sh"',updater)

    def test_queue_worker_writes_heartbeat(self):
        src=(ROOT/"runtime/queue_core.py").read_text()
        self.assertIn("last_beat",src)
        self.assertIn("UPDATE queue SET updated_at=?",src)

    def test_maintenance_policy_and_alerts_are_wired(self):
        script=(ROOT/"runtime/maintain_bridge.sh").read_text()
        self.assertIn('policy "$ROOT"',script)
        self.assertIn('network_due" == "SKIP"',script)
        self.assertIn('record-update "$ROOT"',script)
        self.assertIn('alerts "$ROOT"',script)
        self.assertIn('python "$health_script"',script)

if __name__=="__main__":
    unittest.main()
