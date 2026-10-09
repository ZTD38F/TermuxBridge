"""Production health and privacy regression tests. No Android credentials required."""
from __future__ import annotations

import datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from runtime import maintenance_health as h


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        (self.root / "jobs").mkdir()
        self.generation = "a" * 40
        (self.root / "source_commit").write_text(self.generation)
        (self.root / "state/route.json").write_text(json.dumps({"generation": self.generation, "port": 18771}))
        (self.root / "state/update.json").write_text(json.dumps({"phase": "COMMITTED", "current_generation": self.generation}))
        (self.root / "state/tunnel_status.json").write_text(json.dumps({"state": "CLIENT_RUNNING"}))
        self.when = dt.datetime(2026, 10, 9, 10, 0, tzinfo=dt.timezone.utc)
        self.all_healthy = dict.fromkeys([*h.ROLES, "supervisor_http", "mcp_http"], True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_true_ok_requires_authenticated_service_probes(self):
        ok = h.evaluate(self.root, 0, 0, services=self.all_healthy, now=self.when)
        self.assertEqual(ok["result"], "OK")
        self.assertEqual(ok["reason"], "NONE")
        self.assertEqual(ok["consecutive_failures"], 0)
        self.assertEqual(ok["last_success_at"], self.when.isoformat(timespec="seconds"))
        self.assertTrue(ok["tunnel_local_only"])

    def test_no_servers_must_never_appear_ok(self):
        unhealthy = {**self.all_healthy, "mcp_http": False}
        result = h.evaluate(self.root, 0, 0, services=unhealthy, now=self.when)
        self.assertEqual(result["result"], "DEGRADED")
        self.assertEqual(result["reason"], "LOCAL_SERVICES_UNHEALTHY")
        self.assertEqual(result["consecutive_failures"], 1)
        self.assertEqual(result["local_processes"]["mcp_http"], False)

    def test_failures_accumulate_until_real_recovery(self):
        (self.root / "state/maintenance.json").write_text(json.dumps({
            "result": "DEGRADED", "consecutive_failures": 2,
            "last_success_at": "2026-10-08T08:00:00+00:00"}))
        first = h.evaluate(self.root, 0, 19, services=self.all_healthy, now=self.when)
        self.assertEqual(first["consecutive_failures"], 3)
        self.assertEqual(first["reason"], "STARTUP_OR_UPDATE_FAILED")
        self.assertEqual(first["last_success_at"], "2026-10-08T08:00:00+00:00")
        repaired = h.evaluate(self.root, 0, 0, services=self.all_healthy, now=self.when)
        self.assertEqual(repaired["consecutive_failures"], 0)
        self.assertEqual(repaired["result"], "OK")

    def test_generation_mismatch_and_busy(self):
        (self.root / "state/route.json").write_text(json.dumps({"generation": "b" * 40, "port": 18771}))
        degraded = h.evaluate(self.root, 0, 0, services=self.all_healthy, now=self.when)
        self.assertEqual(degraded["reason"], "GENERATION_MISMATCH")
        busy = h.evaluate(self.root, 0, 75, services=self.all_healthy, now=self.when)
        self.assertEqual(busy["result"], "BUSY")
        self.assertEqual(busy["consecutive_failures"], 0)

    def test_remove_only_legacy_plaintext_argv(self):
        name = "20261009T100000Z-18480.json"
        meta = self.root / "jobs" / name
        meta.write_text(json.dumps({"pid": 18480, "argv": ["private-password-not-for-output"],
                                    "cwd": "demo", "log": "demo.log"}))
        meta.chmod(0o644)
        outside = self.root / "untouched.json"
        outside.write_text(json.dumps({"argv": ["PRIVATE"]}))
        (self.root / "jobs" / "20261009T100001Z-1.json").symlink_to(outside)
        self.assertEqual(h.scrub_legacy_job_args(self.root), 1)
        cleaned = json.loads(meta.read_text())
        self.assertEqual(cleaned, {"pid": 18480, "cwd": "demo", "log": "demo.log"})
        self.assertEqual(meta.stat().st_mode & 0o777, 0o600)
        self.assertEqual(h.scrub_legacy_job_args(self.root), 0)
        self.assertIn("argv", outside.read_text())

    def test_status_atomic_and_private(self):
        record = h.evaluate(self.root, 0, 0, services=self.all_healthy, now=self.when)
        h.write_status(self.root, record)
        path = self.root / "state/maintenance.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["result"], "OK")

    def test_local_probe_enforces_header_and_pid(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                auth = self.headers.get("X-Bridge-Backend-Token")
                data = {"ok": auth == "T" * 40, "pid": os.getpid()}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        (self.root / "secrets").mkdir()
        (self.root / "secrets/backend_token").write_text("T" * 40)
        try:
            self.assertTrue(h.authenticated_local_health(
                self.root, server.server_port, "X-Bridge-Backend-Token", "backend_token", os.getpid()))
            self.assertFalse(h.authenticated_local_health(
                self.root, server.server_port, "X-Bridge-Backend-Token", "backend_token", os.getpid() + 1))
            (self.root / "secrets/backend_token").write_text("BAD")
            self.assertFalse(h.authenticated_local_health(
                self.root, server.server_port, "X-Bridge-Backend-Token", "backend_token", os.getpid()))
        finally:
            server.shutdown()
            server.server_close()
            th.join(timeout=2)

    def test_release_requires_complete_tests_before_publish(self):
        data = (Path(__file__).resolve().parents[1] /
                ".github/workflows/release.yml").read_text()
        self.assertIn("python -m unittest discover -s tests", data)
        self.assertLess(data.index("Run complete regression suite"), data.index("Publish stable release"))


if __name__ == "__main__":
    unittest.main()
