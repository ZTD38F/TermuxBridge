"""Synthetic, confined file-probing regression; no real app data is touched."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "android_data_native", ROOT/"runtime/android_data_native.py")
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class NativeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="android-data-safe-")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"Android"/"data"
        self.root.mkdir(parents=True)
        self.app=self.root/"test.example"
        self.app.mkdir()
        (self.app/"photo.jpg").write_bytes(b"\xff\xd8\xff\x00")
        (self.app/"empty.txt").touch()
        self.neighbor=Path(self.temp.name)/"secret"
        self.neighbor.write_text("private")

    def test_metadata_status_does_not_claim_file_access(self):
        info=mod.status(self.root)
        self.assertTrue(info["metadata_access"])
        self.assertFalse(info["file_bytes_verified"])
        self.assertEqual(info["file_access_status"],"UNVERIFIED")
        self.assertEqual(info["directories"],1)

    def test_real_byte_read_required_for_success(self):
        ok=mod.read_probe(self.root,"test.example/photo.jpg")
        self.assertTrue(ok["file_bytes_verified"])
        self.assertEqual(ok["reason"],"READABLE")

    def test_empty_file_not_treated_as_readable_content(self):
        self.assertFalse(mod.read_probe(self.root,"test.example/empty.txt")["ok"])

    def test_nonexistent_file_fails_closed(self):
        got=mod.read_probe(self.root,"test.example/nonexistent")
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"],"ENOENT")

    def test_directory_is_not_a_file(self):
        self.assertFalse(mod.read_probe(self.root,"test.example")["file_bytes_verified"])

    def test_parent_traversal_forbidden(self):
        for p in ("../secret","test.example/../../secret",
                  "/tmp/secret","", "./test.example/photo.jpg",
                  "test.example//photo.jpg"):
            self.assertFalse(mod.read_probe(self.root,p)["ok"],p)

    def test_symlink_escape_forbidden(self):
        (self.app/"shortcut").symlink_to(self.neighbor)
        self.assertFalse(mod.read_probe(self.root,"test.example/shortcut")["ok"])
        self.assertFalse(mod.list_dir(self.root,"test.example/shortcut")["ok"])

    def test_synthetic_source_is_not_modified(self):
        before={p.relative_to(self.root):p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}
        mod.status(self.root)
        mod.list_dir(self.root)
        mod.read_probe(self.root,"test.example/photo.jpg")
        after={p.relative_to(self.root):p.read_bytes()
               for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before,after)

    def test_failure_does_not_claim_access(self):
        result=mod.status(self.root/"missing")
        self.assertFalse(result["ok"])
        self.assertFalse(result["file_bytes_verified"])

    def test_root_list_is_bounded(self):
        result=mod.list_dir(self.root,limit=0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"],[])


if __name__ == "__main__":
    unittest.main()
