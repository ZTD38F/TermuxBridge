"""Tests for on-device OCR, index security and search. Author: Dāvids Krūmiņš."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0,str(RUNTIME))
import gallery_intelligence
import gallery_ocr
try:
    from PIL import Image,ImageDraw,ImageFont
except ImportError:
    Image = None


@unittest.skipUnless(Image is not None, "Pillow unavailable")
class OcrTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        root = self.tmp / "android"
        root.mkdir()
        state = self.tmp / "state"
        state.mkdir()
        folder = root / "Pictures"
        folder.mkdir()
        self.img = folder / "board.png"
        image = Image.new("RGB",(900,230),"white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.load_default(size=50)
        except TypeError:
            font = ImageFont.load_default()
        draw.text((35,65),"SONORYX ORBIT 4729",font=font,fill="black")
        image.save(self.img)
        core = types.ModuleType("gallery_sandbox")
        code = (RUNTIME / "gallery_bridge_tools.py").read_text().split("\n# Sonoryx Photo Intelligence v2")[0]
        exec(compile(code, str(RUNTIME / "gallery_bridge_tools.py"),"exec"), core.__dict__)
        core.SHARED_ROOT = root
        core.STATE_DIR = state
        core.DB_PATH = state/"gallery.sqlite3"
        gallery_intelligence.install(core.__dict__)
        gallery_ocr.install(core.__dict__)
        self.g = core
        scanned = self.g.gallery_scan({})
        self.assertEqual(scanned["indexed"],1)
        self.photo_id = self.g.gallery_list({"limit":1})["items"][0]["id"]

    def test_unicode_fts_and_combined_search(self):
        self.g.gallery_status({})
        with self.g._db() as conn:
            conn.execute("INSERT INTO gallery_ocr_fts(rowid,content) VALUES (?,?)",
                         (self.photo_id, "Привет Рига Latvija 4729"))
            conn.execute("INSERT INTO gallery_ocr_index(image_id,size,mtime,languages,processed_at,text_length) "
                         "SELECT id,size,mtime,'rus+lav+eng',1000,23 FROM images WHERE id=?", (self.photo_id,))
        self.assertEqual(self.g.gallery_ocr_search({"query":"Привет"})["total"],1)
        self.assertEqual(self.g.gallery_ocr_search({"query":"Latvija"})["total"],1)
        self.assertEqual(self.g.gallery_search({"query":"Рига"})["total"],1)
        self.assertEqual(self.g.gallery_search({"query":"board.png"})["total"],1)
        self.assertEqual(self.g.gallery_ocr_text({"id":self.photo_id})["text"], "Привет Рига Latvija 4729")

    def test_fts_query_injection_is_not_executed(self):
        self.assertEqual(gallery_ocr._fts_terms('" OR everything; DROP TABLE images;--'),
                         '"OR" AND "everything" AND "DROP" AND "TABLE" AND "images"')
        with self.assertRaises(ValueError):
            self.g.gallery_ocr_search({"query":"---"})
        self.assertEqual(self.g.gallery_ocr_search({"query":'"; DROP TABLE images; --'})["total"],0)
        self.assertEqual(self.g.gallery_status({})["indexed"],1)

    def test_stale_index_is_not_searched(self):
        self.g.gallery_status({})
        with self.g._db() as conn:
            conn.execute("INSERT INTO gallery_ocr_fts(rowid,content) VALUES (?,?)", (self.photo_id,"OLD TEXT"))
            conn.execute("INSERT INTO gallery_ocr_index(image_id,size,mtime,languages,processed_at,text_length) "
                         "SELECT id,size,mtime,'eng',1000,8 FROM images WHERE id=?", (self.photo_id,))
        self.assertEqual(self.g.gallery_ocr_search({"query":"OLD"})["total"],1)
        Image.new("RGB",(60,50),"black").save(self.img)
        self.g.gallery_scan({})
        self.assertEqual(self.g.gallery_ocr_search({"query":"OLD"})["total"],0)
        self.assertFalse(self.g.gallery_ocr_text({"id":self.photo_id})["ok"])

    def test_low_battery_blocks_background_batch(self):
        with patch.object(gallery_ocr,"battery_status",return_value={"available":True,"level":17,"charging":False}), \
             patch.object(gallery_ocr,"ocr_languages",return_value=["eng"]):
            result=self.g.gallery_ocr_index({"max_images":2,"languages":"eng"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["battery"]["level"],17)

    @unittest.skipUnless(gallery_ocr.ocr_languages(), "Tesseract binary unavailable")
    def test_real_tesseract_single_photo(self):
        with patch.object(gallery_ocr,"battery_status",return_value={"available":True,"level":17,"charging":False}):
            result=self.g.gallery_ocr_index({"ids":[self.photo_id],"max_images":1,
                                              "max_seconds":20,"languages":"eng"})
        self.assertTrue(result["ok"],result)
        self.assertEqual(result["recognized_ids"],[self.photo_id])
        txt=self.g.gallery_ocr_text({"id":self.photo_id})
        self.assertTrue(txt["ok"])
        self.assertIn("SONORYX",txt["text"].upper())
        self.assertEqual(self.g.gallery_ocr_search({"query":"SONORYX"})["total"],1)
        self.assertEqual(self.g.gallery_search({"query":"ORBIT"})["total"],1)

if __name__ == "__main__":
    unittest.main()
