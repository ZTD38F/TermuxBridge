"""TermuxBridge v1.2.8 tool capability, privacy, and lifecycle contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from runtime import bridge_server as b


class ExtendedTermuxToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root, self.old_jobs = b.ROOT, b.JOBS
        b.ROOT = self.root
        b.JOBS = self.root / "jobs"

    def tearDown(self):
        b.ROOT, b.JOBS = self.old_root, self.old_jobs
        self.tmp.cleanup()

    def test_command_arg_limit_and_output_limit(self):
        self.assertEqual(len(b.checked_argv(["true"] * 4096)), 4096)
        with self.assertRaises(ValueError):
            b.checked_argv(["true"] * 4097)
        result = b.run(["/bin/sh", "-c", "printf 'abcdefghijklmnop'"], str(self.root),
                       timeout=5, max_output_chars=5)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "lmnop")
        self.assertTrue(result["truncated"])

    def test_directory_pagination(self):
        for i in range(6): (self.root / f"{i:02d}.txt").write_text(str(i))
        a = b.call("list_files", {"path": ".", "limit": 2, "offset": 0})
        z = b.call("list_files", {"path": ".", "limit": 2, "offset": 4})
        self.assertEqual([x["name"] for x in a], ["00.txt", "01.txt"])
        self.assertEqual([x["name"] for x in z], ["04.txt", "05.txt"])

    def test_text_pagination_and_integrity(self):
        (self.root / "text.txt").write_text("abcdefghij", encoding="utf-8")
        a = b.call("read_text", {"path": "text.txt", "max_chars": 4, "offset": 4,
                                 "include_sha256": False})
        self.assertEqual(a["text"], "efgh")
        self.assertEqual(a["next_offset"], 8)
        self.assertEqual(a["size"], 10)
        self.assertIsNone(a["sha256"])
        z = b.call("read_text", {"path": "text.txt", "max_chars": 4, "offset": 8})
        self.assertEqual(z["text"], "ij")
        self.assertFalse(z["truncated"])
        self.assertIsNotNone(z["sha256"])

    def test_search_pages_and_bigger_files(self):
        (self.root / "a.txt").write_text("one\nneedle1\nneedle2\n")
        a = b.call("search_text", {"path": "a.txt", "query": "needle", "limit": 1, "offset": 0,
                                    "max_file_bytes": 1024})
        self.assertEqual(a["hits"][0]["line"], 2)
        self.assertEqual(a["next_offset"], 1)
        z = b.call("search_text", {"path": "a.txt", "query": "needle", "limit": 1, "offset": 1})
        self.assertEqual(z["hits"][0]["line"], 3)

    def test_job_metadata_does_not_persist_arguments_and_stop_checks_identity(self):
        result = b.call("start_job", {"argv": ["/bin/sh", "-c",
                  "sleep 30 # synthetic_secret_do_not_store"]})
        job = result["job_id"]
        try:
            meta_path = b.JOBS / (job + ".json")
            metadata = meta_path.read_text()
            self.assertNotIn("synthetic_secret_do_not_store", metadata)
            self.assertEqual(meta_path.stat().st_mode & 0o777, 0o600)
            obj = json.loads(metadata)
            self.assertEqual(obj["argv_count"], 3)
            self.assertIsNotNone(obj["start_ticks"])
            self.assertTrue(b.call("job_status", {"job_id": job})["running"])
            stopped = b.call("stop_job", {"job_id": job})
            self.assertTrue(stopped["signal_sent"])
            obj["start_ticks"] += 1
            meta_path.write_text(json.dumps(obj))
            self.assertFalse(b.call("stop_job", {"job_id": job})["signal_sent"])
        finally:
            try:
                import signal
                os.kill(result["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass

    def test_diagnostics_only_include_sanitized_fields(self):
        p = self.root / "state"
        p.mkdir()
        (p / "route.json").write_text(json.dumps({"generation": "test", "port": 18771,
                                                    "router_token": "TOP_SECRET"}))
        with mock.patch.dict(os.environ, {"TERMUXBRIDGE_ROOT": str(self.root)}):
            d = b.bridge_diagnostics()
        self.assertEqual(d["route"], {"generation": "test", "port": 18771})
        self.assertNotIn("TOP_SECRET", json.dumps(d))
        self.assertIn("unmanaged_tunnel_instances", d)


if __name__ == "__main__":
    unittest.main()
