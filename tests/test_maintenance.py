"""Regression tests for hourly TermuxBridge self-maintenance.

Runs with harmless mock start/update scripts in a temporary root. Never touches
an Android installation, private credentials, or the real GitHub updater.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import fcntl

ROOT = Path(__file__).resolve().parents[1]
MAINTAIN = ROOT / "runtime/maintain_bridge.sh"
CTL = ROOT / "termuxbridgectl"
UPDATE = ROOT / "update_termux.sh"
SHA = "a" * 40


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()
        (self.root / "source_commit").write_text(SHA)
        (self.root / "state/route.json").write_text(json.dumps({"generation": SHA, "port": 18771}))
        (self.root / "state/update.json").write_text(json.dumps({"phase": "COMMITTED", "current_generation": SHA}))
        (self.root / "state/tunnel_status.json").write_text(json.dumps({"state": "CLIENT_RUNNING"}))
        self.script("start_bridge.sh", "#!/bin/bash\necho local-ok\n")
        self.script("update_termux.sh", "#!/bin/bash\necho current-release-ok\n")

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, name, content):
        path = self.root / name
        path.write_text(content)
        path.chmod(0o700)
        return path

    def invoke(self):
        return subprocess.run(["bash", str(MAINTAIN)], env={**os.environ, "TERMUXBRIDGE_ROOT": str(self.root)},
                              cwd=self.root, capture_output=True, text=True, timeout=12)

    def result(self):
        return json.loads((self.root / "state/maintenance.json").read_text())

    def test_successful_run_records_consistent_status(self):
        done = self.invoke()
        self.assertEqual(done.returncode, 0, done.stderr)
        data = self.result()
        self.assertEqual(data["result"], "OK")
        self.assertTrue(data["verified_active_generation"])
        self.assertEqual(data["tunnel_state"], "CLIENT_RUNNING")
        self.assertEqual(data["installed_generation"], SHA)
        self.assertNotIn("control_plane_api_key", json.dumps(data))
        self.assertEqual((self.root / "state/maintenance.json").stat().st_mode & 0o777, 0o600)

    def test_update_failure_is_reported_and_will_retry(self):
        self.script("update_termux.sh", "#!/bin/bash\nexit 19\n")
        failed = self.invoke()
        self.assertNotEqual(failed.returncode, 0)
        data = self.result()
        self.assertEqual(data["result"], "FAILED")
        self.assertEqual(data["updater_exit_code"], 19)

    def test_mismatched_route_is_not_falsely_reported_as_current(self):
        (self.root / "state/route.json").write_text(json.dumps({"generation": "b" * 40, "port": 18772}))
        done = self.invoke()
        self.assertEqual(done.returncode, 0)
        data = self.result()
        self.assertEqual(data["result"], "DEGRADED")
        self.assertFalse(data["verified_active_generation"])

    def test_lock_prevents_overlapping_jobs(self):
        with (self.root / ".maintenance.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke()
            self.assertEqual(result.returncode, 0)
            self.assertIn("skipped overlapping", result.stdout)
            self.assertFalse((self.root / "state/maintenance.json").exists())

    def test_candidate_cleanup_uses_valid_nul_parser_and_json_newline(self):
        source = UPDATE.read_text()
        self.assertIn(r'split(b"\0")', source)
        self.assertNotIn(r'split(b"\\0")', source)
        self.assertNotIn(r'+"\\n",encoding', source)
        self.assertIn(r'+"\n",encoding', source)

    def test_production_update_rejects_unpublished_sources(self):
        source = UPDATE.read_text()
        self.assertIn("Production maintenance accepts published checksum-verified releases only", source)
        self.assertIn('ERROR stable release metadata unavailable', source)
        self.assertIn('ERROR latest stable release metadata invalid', source)
        self.assertNotIn('if [[ -z "${TARGET:-}" ]]', source)
        self.assertIn('sha256sum -c source.sha256', source)

    def test_running_updater_isolated_from_atomic_install(self):
        u = UPDATE.read_text()
        m = MAINTAIN.read_text()
        t = CTL.read_text()
        self.assertIn('mv -f "$ROOT/update_termux.sh.next" "$ROOT/update_termux.sh"', u)
        self.assertIn('mv -f "$ROOT/maintain_bridge.sh.next" "$ROOT/maintain_bridge.sh"', u)
        self.assertNotIn('cp -a "$source_root/update_termux.sh" "$ROOT/update_termux.sh"', u)
        self.assertIn('updater_snapshot="$(mktemp', m)
        self.assertIn('"$updater_snapshot" 8>&-', m)
        self.assertIn('snapshot="$(mktemp', t)
        self.assertIn('"$snapshot" --force "$@"', t)

    def test_job_scheduler_hourly_persisted_and_mgmt_is_versioned(self):
        ctl = CTL.read_text()
        up = UPDATE.read_text()
        self.assertIn('--period-ms 3600000', ctl)
        self.assertIn('--persisted true', ctl)
        self.assertIn('--script "$ROOT/maintain_bridge.sh"', ctl)
        self.assertIn('maintain_bridge.sh)', up)
        self.assertIn('cp -a "$source_root/runtime/maintain_bridge.sh"', up)
        self.assertIn('8>&- >>"$LOG" 2>&1', MAINTAIN.read_text())


if __name__ == "__main__":
    unittest.main()
