"""Regression tests for Sonoryx Photo Intelligence by Dāvids Krūmiņš."""
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest

BASE = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(BASE))
from gallery_intelligence import install
try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipUnless(Image is not None, "Pillow not installed")
class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.shared = base / "storage"
        self.shared.mkdir()
        self.state = base / "private"
        self.state.mkdir()
        self.camera = self.shared / "DCIM" / "Camera"
        self.camera.mkdir(parents=True)
        self.first = self.camera / "test-a.jpg"
        im = Image.new("RGB", (500, 240), "#88aabb")
        exif = Image.Exif()
        exif[36867] = "2026:10:10 08:30:01"
        im.save(self.first, exif=exif)
        self.second = self.camera / "test-copy.jpg"
        shutil.copy2(self.first, self.second)
        self.third = self.camera / "other.png"
        Image.new("RGB", (60, 90), "black").save(self.third)
        (self.shared / ".hidden").mkdir()
        shutil.copy2(self.first, self.shared / ".hidden" / "hidden.jpg")
        (self.camera / "external.jpg").symlink_to(self.first)
        code = (BASE / "gallery_bridge_tools.py").read_text()
        code = code.split("\n# Sonoryx Photo Intelligence v2")[0]
        m = types.ModuleType("gallery_isolated")
        exec(compile(code, str(BASE / "gallery_bridge_tools.py"), "exec"), m.__dict__)
        m.SHARED_ROOT = self.shared
        m.STATE_DIR = self.state
        m.DB_PATH = self.state / "gallery.sqlite3"
        install(m.__dict__)
        self.m = m

    def test_scan_and_rescan_stable_ids(self):
        first = self.m.gallery_scan({})
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["indexed"], 3)
        self.assertFalse(first["photos_modified"])
        items = self.m.gallery_list({"limit": 20})["items"]
        initial = {x["path"]:x["id"] for x in items}
        second = self.m.gallery_scan({})
        self.assertEqual(second["removed_missing"],0)
        self.assertEqual({x["path"]:x["id"] for x in self.m.gallery_list({"limit":20})["items"]},initial)

    def test_metadata_has_source_dimensions_and_no_gps(self):
        self.m.gallery_scan({})
        items = self.m.gallery_list({"query":"test-a","limit":5})["items"]
        key = items[0]["id"]
        preview = self.m.gallery_thumbnail({"id":key,"max_edge":128})
        self.assertEqual(preview["__mcp_content__"][0]["type"],"image")
        info = self.m.gallery_metadata({"id":key})
        self.assertEqual((info["pixel_width"],info["pixel_height"]),(500,240))
        self.assertEqual(info["exif_date_taken"],"2026:10:10 08:30:01")
        self.assertFalse(info["gps_included"])
        # Preview dimensions must not corrupt original metadata.
        with self.m._db() as conn:
            row = conn.execute("SELECT width,height FROM images WHERE id=?", (key,)).fetchone()
        self.assertEqual((row["width"],row["height"]),(None,None))

    def test_duplicate_and_similarity(self):
        self.m.gallery_scan({})
        res = self.m.gallery_find_duplicates({"max_files": 100, "max_megabytes":10})
        self.assertTrue(res["complete"],res)
        self.assertEqual(len(res["groups"]),1)
        self.assertEqual(len(res["groups"][0]["ids"]),2)
        similar = self.m.gallery_similar({"ids":res["groups"][0]["ids"]})
        self.assertEqual(similar["pairs"][0]["distance"],0)

    def test_missing_storage_preserves_index(self):
        self.m.gallery_scan({})
        self.shared.rename(self.shared.with_name("storage_offline"))
        failed = self.m.gallery_scan({})
        self.assertFalse(failed["ok"])
        self.assertEqual(self.m.gallery_status({})["indexed"],3)

    def test_stale_file_rejected_then_reconciled(self):
        self.m.gallery_scan({})
        row = self.m.gallery_list({"query":"other.png"})["items"][0]
        Image.new("RGB",(15,15),"white").save(self.third)
        with self.assertRaisesRegex(ValueError,"changed"):
            self.m.gallery_metadata({"id":row["id"]})
        updated = self.m.gallery_scan({})
        self.assertTrue(updated["ok"])
        self.assertEqual(self.m.gallery_metadata({"id":row["id"]})["pixel_width"],15)

    def test_sudden_mass_disappearance_never_prunes(self):
        for i in range(120):
            shutil.copy2(self.first, self.camera / ("extra-%03d.jpg" % i))
        self.m.gallery_scan({})
        for i in range(120):
            (self.camera / ("extra-%03d.jpg" % i)).unlink()
        result = self.m.gallery_scan({})
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.m.gallery_status({})["indexed"],123)

    def test_no_original_mutations(self):
        originals = [(p, p.read_bytes(), p.stat().st_mtime_ns)
                     for p in (self.first,self.second,self.third)]
        self.m.gallery_scan({})
        ids = [x["id"] for x in self.m.gallery_list({"limit":20})["items"]]
        self.m.gallery_similar({"ids":ids})
        self.m.gallery_find_duplicates({"max_files":100,"max_megabytes":10})
        for path, content, mtime in originals:
            self.assertEqual(path.read_bytes(),content)
            self.assertEqual(path.stat().st_mtime_ns,mtime)

    def test_status_health(self):
        self.m.gallery_scan({})
        s = self.m.gallery_status({})
        self.assertEqual(s["db_integrity"],"ok")
        self.assertFalse(s["semantic_embeddings"])
        self.assertFalse(s["ocr_available"])
        self.assertEqual(s["indexed"],3)

if __name__ == "__main__":
    unittest.main()
