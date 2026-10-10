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


    def test_full_ocr_complete_returns_to_normal_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"bridge"; private=Path(tmp)/"private"
            (root/"current").mkdir(parents=True); private.mkdir()
            (private/"gallery_full_ocr.enabled").touch()
            (private/"gallery_full_ocr_status.json").write_text('{"state":"COMPLETE"}')
            result=w.ensure_full_ocr(root,private)
            self.assertFalse(result["active"])
            self.assertEqual(result["result"],"FULL_OCR_COMPLETE")

    def test_full_ocr_missing_process_is_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"bridge"; private=Path(tmp)/"private"
            current=root/"current"; current.mkdir(parents=True); private.mkdir()
            (current/"gallery_full_ocr.py").write_text("raise SystemExit(0)\n")
            (private/"gallery_full_ocr.enabled").touch()
            (private/"gallery_full_ocr_status.json").write_text('{"state":"RUNNING"}')
            fake=type("P",(),{"pid":43210})()
            with patch.object(w.subprocess,"Popen",return_value=fake) as popen:
                result=w.ensure_full_ocr(root,private)
            self.assertTrue(result["active"])
            self.assertEqual(result["result"],"FULL_OCR_RESTARTED")
            self.assertEqual(result["pid"],43210)
            self.assertTrue(popen.called)

    def test_full_ocr_disabled_does_not_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"bridge"; private=Path(tmp)/"private"
            (root/"current").mkdir(parents=True); private.mkdir()
            with patch.object(w.subprocess,"Popen") as popen:
                result=w.ensure_full_ocr(root,private)
            self.assertFalse(result["active"])
            self.assertEqual(result["result"],"FULL_OCR_DISABLED")
            popen.assert_not_called()
