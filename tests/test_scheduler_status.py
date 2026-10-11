"""Regression tests for Termux:API JobScheduler pending-list NPE handling.
Author: @dvkrum
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / "termuxbridgectl"
WRAPPER = ROOT / "runtime/scheduled_maintain_bridge.sh"


class SchedulerStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.wrapper = self.root / "scheduled_maintain_bridge.sh"
        shutil.copyfile(WRAPPER, self.wrapper)
        self.wrapper.chmod(0o700)
        self.maint = self.root / "maintain_bridge.sh"
        self.maint.write_text("#!" + shutil.which("bash") + "\nexit 0\n")
        self.maint.chmod(0o700)

    def scheduler(self, body):
        path = self.bin / "termux-job-scheduler"
        path.write_text("#!" + shutil.which("bash") + "\nset -eu\n" + body)
        path.chmod(0o700)

    def invoke(self, *args):
        return subprocess.run(
            ["bash", str(CTL), *args],
            env={**os.environ,
                 "TERMUXBRIDGE_ROOT": str(self.root),
                 "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]},
            capture_output=True, text=True, timeout=15,
        )

    def read(self, name):
        return json.loads((self.root / "state" / name).read_text())

    def test_unverified_registration_does_not_query_buggy_pending_api(self):
        self.scheduler(
            'if [[ " $* " == *" --pending "* ]]; then echo pending-bug >&2; exit 71; fi\n'
            'if [[ " $* " == *" --script "* ]]; then exit 0; fi\n'
        )
        reg = self.invoke("auto-update-enable")
        self.assertEqual(reg.returncode, 0, reg.stderr)
        self.assertIn("SUBMITTED_UNVERIFIED", reg.stdout)
        marker = self.read("auto_update.json")
        self.assertTrue(marker["enabled"])
        self.assertEqual(marker["job_id"], 38038)
        status = self.invoke("auto-update-status")
        self.assertEqual(status.returncode, 1)
        self.assertIn("SUBMITTED_UNVERIFIED", status.stdout)
        self.assertNotIn("pending-bug", reg.stderr + status.stderr)

    def test_scheduled_wrapper_proves_matching_receipt_only(self):
        self.scheduler('exit 0\n')
        self.assertEqual(self.invoke("auto-update-enable").returncode, 0)
        done = subprocess.run(["bash", str(self.wrapper)],
                              env={**os.environ,"TERMUXBRIDGE_ROOT":str(self.root)},
                              capture_output=True,text=True,timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        marker = self.read("auto_update.json")
        execution = self.read("scheduled_maintenance.json")
        self.assertEqual(marker["request_id"], execution["request_id"])
        self.assertEqual(execution["exit_code"], 0)
        self.assertEqual(self.invoke("auto-update-status").returncode, 0)
        # An obsolete receipt must never verify a new registration.
        self.assertEqual(self.invoke("auto-update-enable").returncode, 0)
        fresh = self.invoke("auto-update-status")
        self.assertEqual(fresh.returncode, 1)
        self.assertIn("SUBMITTED_UNVERIFIED",fresh.stdout)

    def test_failure_is_reported_not_concealed(self):
        self.scheduler('exit 0\n')
        self.maint.write_text("#!" + shutil.which("bash") + "\nexit 9\n")
        self.assertEqual(self.invoke("auto-update-enable").returncode, 0)
        done = subprocess.run(["bash",str(self.wrapper)],
                              env={**os.environ,"TERMUXBRIDGE_ROOT":str(self.root)},
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(done.returncode, 9)
        status=self.invoke("auto-update-status")
        self.assertEqual(status.returncode, 1)
        self.assertIn("SCHEDULED_EXECUTION_FAILED",status.stdout)

    def test_stale_manual_maintenance_does_not_verify_schedule(self):
        self.scheduler('exit 0\n')
        self.assertEqual(self.invoke("auto-update-enable").returncode, 0)
        (self.root/"state/maintenance.json").write_text(json.dumps({
            "result":"OK","consecutive_failures":0,
            "last_checked_at":"2099-12-12T00:00:00Z",
        }))
        self.assertEqual(self.invoke("auto-update-status").returncode, 1)

    def test_disable_is_explicit_and_failure_does_not_claim_success(self):
        self.scheduler('exit 0\n')
        self.assertEqual(self.invoke("auto-update-enable").returncode, 0)
        self.assertEqual(self.invoke("auto-update-disable").returncode, 0)
        self.assertFalse(self.read("auto_update.json")["enabled"])
        self.assertEqual(self.invoke("auto-update-status").returncode, 1)
        self.scheduler('exit 7\n')
        bad=self.invoke("auto-update-disable")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("cannot confirm",bad.stderr)

if __name__ == "__main__":
    unittest.main()
