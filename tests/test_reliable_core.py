"""Phase 1: streaming, resource budgets, durable queue and safe rollback tests."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from runtime import execution_core as e
from runtime import queue_core as q
from runtime import rollback_guard as rb


class StreamingTests(unittest.TestCase):
    def test_bounded_two_megabyte_output(self):
        out=e.execute([os.sys.executable,"-c",
                       "import sys;sys.stdout.write('x'*2000000)"],
                      os.getcwd(),timeout=10,max_output_chars=73)
        self.assertEqual(out["exit_code"],0,out)
        self.assertEqual(out["total_output_bytes"],2000000)
        self.assertEqual(len(out["output"]),73)
        self.assertTrue(out["truncated"])

    def test_timeout_terminates_child_process(self):
        out=e.execute([os.sys.executable,"-c","import time;time.sleep(5)"],
                      os.getcwd(),timeout=1)
        self.assertTrue(out["timed_out"])
        self.assertNotEqual(out["exit_code"],0)

    def test_resource_budget_and_argument_validation(self):
        for memory in (1,8192):
            with self.assertRaises(ValueError):
                e.execute(["true"],os.getcwd(),memory_mb=memory)
        with self.assertRaises(ValueError):
            e.execute(["true"],os.getcwd(),cpu_seconds=0)
        self.assertEqual(e.SLOTS._initial_value,2)

    def test_cancel_event_stops_owned_child(self):
        stop=threading.Event()
        answer={}
        th=threading.Thread(target=lambda:answer.update(
            e.execute([os.sys.executable,"-c","import time;time.sleep(5)"],
                      os.getcwd(),timeout=8,cancel=stop)))
        th.start()
        time.sleep(.35)
        stop.set()
        th.join(timeout=4)
        self.assertFalse(th.is_alive())
        self.assertTrue(answer["cancelled"])


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/"work.sh").write_text("#!/bin/sh\nprintf PHASE_ONE_DONE")
        (self.root/"secret.sh").write_text("#!/bin/sh\nprintf confidential_output_should_not_be_stored")
    def tearDown(self):
        self.tmp.cleanup()
    def test_persistence_priority_and_result_privacy(self):
        low=q.submit("script","work.sh",priority=1,root=self.root)
        high=q.submit("script","secret.sh",priority=9,root=self.root)
        self.assertEqual(q.claim(self.root)[0],high["id"])
        q.recover(self.root)
        self.assertEqual(q.status(high["id"],root=self.root)["state"],"NEEDS_REVIEW")
        q.retry(high["id"],root=self.root)
        self.assertTrue(q.process_next(self.root))
        self.assertEqual(q.status(high["id"],root=self.root)["state"],"DONE")
        self.assertTrue(q.process_next(self.root))
        self.assertEqual(q.status(low["id"],root=self.root)["state"],"DONE")
        db_bytes=(self.root/"state/queue.sqlite3").read_bytes()
        self.assertNotIn(b"confidential_output_should_not_be_stored",db_bytes)
        self.assertEqual((self.root/"state/queue.sqlite3").stat().st_mode & 0o777,0o600)
        self.assertEqual(q.listing(root=self.root)["counts"]["DONE"],2)
    def test_cancel_pending_is_not_executed(self):
        job=q.submit("script","work.sh",root=self.root)
        self.assertEqual(q.cancel(job["id"],root=self.root)["state"],"CANCELLED")
        self.assertFalse(q.process_next(self.root))
    def test_restart_safe_recovery(self):
        health=q.submit("health",root=self.root)
        script=q.submit("script","work.sh",root=self.root)
        with q.queue_db(self.root) as db:
            db.execute("UPDATE queue SET state='RUNNING'")
        q.recover(self.root)
        self.assertEqual(q.status(health["id"],root=self.root)["state"],"QUEUED")
        self.assertEqual(q.status(script["id"],root=self.root)["state"],"NEEDS_REVIEW")
    def test_script_scope_blocks_escape(self):
        with self.assertRaises(ValueError):
            q.submit("script","/etc/passwd",root=self.root)
        with self.assertRaises(ValueError):
            q.submit("script","../escape.sh",root=self.root)
        with self.assertRaises(ValueError):
            q.submit("arbitrary-exec",root=self.root)
    def test_running_cancellation_request(self):
        job=q.submit("script","work.sh",root=self.root)
        q.claim(self.root)
        self.assertTrue(q.cancel(job["id"],root=self.root)["cancel_requested"])


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/"releases").mkdir()
        (self.root/"state").mkdir()
        self.a="a"*40
        self.b="b"*40
        (self.root/"releases"/self.a).mkdir()
        (self.root/"releases"/self.b).mkdir()
        (self.root/"current").symlink_to(self.root/"releases"/self.a)
        (self.root/"previous").symlink_to(self.root/"releases"/self.b)
        (self.root/"state/update.json").write_text(json.dumps({
            "phase":"COMMITTED","current_generation":self.a,"previous_generation":self.b}))
        (self.root/"state/route.json").write_text(json.dumps({"generation":self.a,"port":18771}))
        self.failure={"consecutive_failures":3,"reason":"LOCAL_SERVICES_UNHEALTHY",
                      "local_processes":{"supervisor":True,"supervisor_http":True,"mcp_http":False}}
    def tearDown(self):
        self.tmp.cleanup()
    def test_repeated_backend_only_failures_eligible(self):
        self.assertTrue(rb.eligible(self.root,self.failure))
        self.assertFalse(rb.eligible(self.root,{**self.failure,"consecutive_failures":2}))
        self.assertFalse(rb.eligible(self.root,{**self.failure,"local_processes":{"supervisor":False,"supervisor_http":False,"mcp_http":False}}))
        self.assertFalse(rb.eligible(self.root,{**self.failure,"local_processes":{"supervisor":True,"supervisor_http":True,"mcp_http":True}}))
        self.assertFalse(rb.eligible(self.root,{**self.failure,"reason":"NETWORK"}))
    def test_failed_or_missing_previous_never_rolls_back(self):
        (self.root/"previous").unlink()
        self.assertFalse(rb.eligible(self.root,self.failure))
        response=rb.attempt(self.root)
        self.assertEqual(response["result"],"NOT_ELIGIBLE")
        self.assertEqual(rb.load(self.root/"state/route.json")["generation"],self.a)
    def test_quarantine_rejects_automatic_reinstall(self):
        src=Path("update_termux.sh").read_text()
        self.assertIn("skipped quarantined release",src)
        self.assertIn('"${1:-}" != "--force"',src)
        self.assertIn('rollback_script="$ROOT/current/rollback_guard.py"',Path("runtime/maintain_bridge.sh").read_text())
    def test_atomic_json_save(self):
        file=self.root/"state/quarantine.json"
        rb.save_json(file,{"generation":self.a})
        self.assertEqual(rb.load(file)["generation"],self.a)
        self.assertEqual(file.stat().st_mode & 0o777,0o600)


if __name__=="__main__":
    unittest.main()
