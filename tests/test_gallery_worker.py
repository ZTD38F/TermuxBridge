"""Gallery worker fallback regression. Author: Dāvids Krūmiņš."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]/"runtime"
sys.path.insert(0,str(BASE))
import gallery_worker as w

class WorkerTests(unittest.TestCase):
    def test_missing_release_is_nondestructive(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=w.single_iteration(Path(tmp))
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"],"MISSING_RELEASE_HELPER")

    def test_one_iteration_with_fake_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            current=base/"current"
            current.mkdir()
            (current/"gallery_maintenance.py").write_text("raise SystemExit(0)\n")
            result=w.single_iteration(base,timeout=5)
            self.assertTrue(result["ok"],result)
            self.assertEqual(result["result"],"COMPLETED")

    def test_status_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"test.json"
            w.write_status({"ok":True,"result":"SKIPPED_POWER"},p)
            self.assertEqual(json.loads(p.read_text())["result"],"SKIPPED_POWER")
            self.assertTrue("timestamp" in json.loads(p.read_text()))
