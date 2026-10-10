#!/usr/bin/env python3
"""Sonoryx Photo Intelligence extensions for TermuxBridge.

Author: Dāvids Krūmiņš.
Never writes to the user's photo library. SQLite index only.
"""
from __future__ import annotations

import collections
from contextlib import contextmanager
import fcntl
import hashlib
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

VERSION = "2.0.0"


def install(g: dict[str, Any]) -> None:
    root = g["SHARED_ROOT"]
    state = g["STATE_DIR"]
    original_db = g["_db"]
    @contextmanager
    def managed_db():
        conn = original_db()
        try:
            with conn:
                yield conn
        finally:
            conn.close()
    g["_db"] = managed_db
    db = managed_db
    schema = g["_schema"]
    tools = g["TOOLS"]
    descriptions = g["DESCRIPTIONS"]
    readonly = g["READ_ONLY"]
    old_status = g["gallery_status"]
    old_list = g["gallery_list"]
    original_scan = g["gallery_scan"]
    image_exts = g["IMAGE_EXTS"]
    image_exts.update({".dng", ".arw", ".cr2", ".nef", ".raf", ".rw2"})
    g["MIME_BY_EXT"].update({".dng": "image/x-adobe-dng", ".arw": "image/x-sony-arw", ".cr2": "image/x-canon-cr2",
                             ".nef": "image/x-nikon-nef", ".raf": "image/x-fuji-raf", ".rw2": "image/x-panasonic-rw2"})

    def _extras(c: sqlite3.Connection) -> None:
        c.execute("""
        CREATE TABLE IF NOT EXISTS gallery_extra (
          image_id INTEGER PRIMARY KEY,
          size INTEGER NOT NULL,
          mtime REAL NOT NULL,
          pixel_width INTEGER,
          pixel_height INTEGER,
          exif_taken TEXT,
          sha256 TEXT,
          dhash TEXT,
          FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS gallery_extra_hash_idx ON gallery_extra(sha256)")
        c.execute("CREATE TABLE IF NOT EXISTS gallery_audit (id INTEGER PRIMARY KEY, started REAL NOT NULL, "
                  "finished REAL NOT NULL, scanned INTEGER NOT NULL, removed INTEGER NOT NULL, "
                  "errors INTEGER NOT NULL, success INTEGER NOT NULL)")

    def _safe_path(row: sqlite3.Row) -> Path:
        path = root / row["path"]
        if path.is_symlink():
            raise ValueError("symbolic links are not permitted")
        real = path.resolve()
        if not real.is_relative_to(root) or not real.is_file():
            raise ValueError("indexed file is unavailable or outside shared storage")
        s = real.stat()
        if s.st_size != row["size"] or abs(s.st_mtime - row["mtime"]) > 0.00001:
            raise ValueError("file changed since index; run gallery_scan")
        return real

    def scan(_: dict[str, Any]) -> dict[str, Any]:
        """Atomic catalog scan. Never prune on inaccessible or suspiciously small input."""
        start = time.time()
        if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            return {"ok": False, "error": "shared storage unavailable; index preserved"}
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (state / "gallery_scan.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"ok": False, "error": "scan already running"}
            errors: list[str] = []
            candidates: list[tuple[str, str, str, int, float, int, str | None]] = []
            stamp = time.time_ns()
            def err(exc: OSError):
                if len(errors) < 12:
                    errors.append(type(exc).__name__)
            for parent, dirs, filenames in os.walk(root, followlinks=False, onerror=err):
                parent_path = Path(parent)
                relative_parent = str(parent_path.relative_to(root))
                dirs[:] = [d for d in dirs if g["_visible_dir"](d, relative_parent)
                           and not (parent_path / d).is_symlink()]
                for name in filenames:
                    p = parent_path / name
                    if p.suffix.lower() not in image_exts or p.is_symlink():
                        continue
                    try:
                        s = p.stat()
                        if not stat.S_ISREG(s.st_mode) or s.st_size <= 0:
                            continue
                        rel = str(p.relative_to(root))
                        album = str(p.parent.relative_to(root))
                        candidates.append((rel, name, album if album != "." else "Shared storage",
                                           int(s.st_size), float(s.st_mtime), stamp,
                                           g["MIME_BY_EXT"].get(p.suffix.lower())))
                    except (OSError, ValueError) as exc:
                        err(exc)
            with db() as c:
                old = c.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            if not candidates or (old >= 100 and len(candidates) < old * 0.6):
                return {"ok": False, "error": "scan unexpectedly incomplete; index preserved",
                        "found": len(candidates), "previous": old, "read_errors": len(errors)}
            removed = 0
            with db() as c:
                _extras(c)
                c.execute("BEGIN IMMEDIATE")
                c.executemany("""
                    INSERT INTO images(path,name,album,size,mtime,seen,mime) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                      name=excluded.name,album=excluded.album,size=excluded.size,
                      mtime=excluded.mtime,seen=excluded.seen,mime=excluded.mime,
                      width=CASE WHEN images.size<>excluded.size OR images.mtime<>excluded.mtime
                                 THEN NULL ELSE images.width END,
                      height=CASE WHEN images.size<>excluded.size OR images.mtime<>excluded.mtime
                                  THEN NULL ELSE images.height END
                """, candidates)
                if not errors:
                    removed = c.execute("DELETE FROM images WHERE seen<>?", (stamp,)).rowcount
                    c.execute("DELETE FROM gallery_extra WHERE image_id NOT IN (SELECT id FROM images)")
                c.execute("INSERT INTO meta(key,value) VALUES('last_scan',?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
                c.execute("INSERT INTO gallery_audit(started,finished,scanned,removed,errors,success) "
                          "VALUES(?,?,?,?,?,?)",
                          (start, time.time(), len(candidates), removed, len(errors), int(not errors)))
                c.commit()
                total = c.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            return {"ok": True, "indexed": total, "scanned": len(candidates),
                    "removed_missing": removed, "read_errors": len(errors),
                    "partial": bool(errors), "pruned": not errors,
                    "seconds": round(time.time()-start,2), "photos_modified": False, "engine": VERSION}

    def _row(photo_id: int) -> sqlite3.Row:
        with db() as c:
            row = c.execute("SELECT * FROM images WHERE id=?", (photo_id,)).fetchone()
        if row is None:
            raise ValueError("unknown image ID")
        return row

    def metadata(args: dict[str, Any]) -> dict[str, Any]:
        row = _row(int(args["id"]))
        path = _safe_path(row)
        result = dict(g["_item"](row))
        result.update({"ok": True, "pixel_width": None, "pixel_height": None,
                       "exif_date_taken": None, "gps_included": False})
        if g["PIL_AVAILABLE"]:
            try:
                with g["Image"].open(path) as im:
                    result["pixel_width"], result["pixel_height"] = im.size
                    exif = im.getexif()
                    for tag in (36867, 36868, 306):
                        value = exif.get(tag)
                        if isinstance(value, (str, bytes)):
                            result["exif_date_taken"] = (value.decode(errors="replace") if isinstance(value, bytes) else value)[:40]
                            break
            except Exception as exc:
                result["decode_error"] = type(exc).__name__
        # Never disclose GPS by default. GPS is not included in this tool.
        with db() as c:
            _extras(c)
            c.execute("""
              INSERT INTO gallery_extra(image_id,size,mtime,pixel_width,pixel_height,exif_taken)
              VALUES(?,?,?,?,?,?)
              ON CONFLICT(image_id) DO UPDATE SET
                size=excluded.size,mtime=excluded.mtime,
                pixel_width=excluded.pixel_width,pixel_height=excluded.pixel_height,
                exif_taken=excluded.exif_taken,
                sha256=CASE WHEN gallery_extra.size<>excluded.size
                                 OR gallery_extra.mtime<>excluded.mtime THEN NULL ELSE gallery_extra.sha256 END,
                dhash=CASE WHEN gallery_extra.size<>excluded.size
                                OR gallery_extra.mtime<>excluded.mtime THEN NULL ELSE gallery_extra.dhash END
            """, (row["id"],row["size"],row["mtime"],result["pixel_width"],result["pixel_height"],result["exif_date_taken"]))
        return result

    def similar(args: dict[str, Any]) -> dict[str, Any]:
        ids = args.get("ids", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 64:
            raise ValueError("ids must be a list of 1 to 64 images")
        if not g["PIL_AVAILABLE"]:
            return {"ok": False, "error": "Pillow unavailable"}
        items = []
        for raw in ids:
            row = _row(int(raw))
            try:
                path = _safe_path(row)
                with g["Image"].open(path) as src:
                    img = g["ImageOps"].exif_transpose(src).convert("L").resize((9, 8))
                    pixels = list(img.get_flattened_data()) if hasattr(img, "get_flattened_data") else list(img.getdata())
                bits = 0
                for y in range(8):
                    for x in range(8):
                        bits = (bits << 1) | int(pixels[y*9+x] > pixels[y*9+x+1])
                items.append({"id":row["id"],"dhash":f"{bits:016x}"})
            except Exception as exc:
                items.append({"id":row["id"],"error":type(exc).__name__})
        pairs = []
        for i,a in enumerate(items):
            if "dhash" not in a: continue
            for b in items[i+1:]:
                if "dhash" not in b: continue
                distance = (int(a["dhash"],16)^int(b["dhash"],16)).bit_count()
                if distance <= max(0,min(64,int(args.get("threshold",8)))):
                    pairs.append({"a":a["id"],"b":b["id"],"distance":distance})
        pairs.sort(key=lambda item: item["distance"])
        return {"ok":True,"tested":len(items),"pairs":pairs[:100],"method":"difference-hash",
                "note":"visual similarity only; not proof of identical photos"}

    def duplicates(args: dict[str, Any]) -> dict[str, Any]:
        max_files = max(2,min(1000,int(args.get("max_files",200))))
        max_bytes = max(1,min(1024,int(args.get("max_megabytes",128))))*1048576
        with db() as c:
            _extras(c)
            rows = c.execute("""
                SELECT i.* FROM images i JOIN
                 (SELECT size FROM images GROUP BY size HAVING COUNT(*) > 1) dup USING(size)
                ORDER BY i.size ASC,i.id ASC
            """).fetchall()
        tested = 0
        consumed = 0
        groups: dict[tuple[int,str],list[int]] = collections.defaultdict(list)
        errors = 0
        for row in rows:
            if tested >= max_files or consumed+row["size"] > max_bytes:
                break
            try:
                p = _safe_path(row)
                with db() as c:
                    _extras(c)
                    cached = c.execute("SELECT sha256 FROM gallery_extra WHERE image_id=? "
                                       "AND size=? AND mtime=?", (row["id"],row["size"],row["mtime"])).fetchone()
                digest = cached["sha256"] if cached and cached["sha256"] else None
                if digest is None:
                    h = hashlib.sha256()
                    with p.open("rb") as f:
                        for block in iter(lambda: f.read(1024*1024), b""):
                            h.update(block)
                    digest = h.hexdigest()
                    with db() as c:
                        _extras(c)
                        c.execute("INSERT INTO gallery_extra(image_id,size,mtime,sha256) VALUES(?,?,?,?) "
                                  "ON CONFLICT(image_id) DO UPDATE SET size=excluded.size, "
                                  "mtime=excluded.mtime,sha256=excluded.sha256",
                                  (row["id"],row["size"],row["mtime"],digest))
                groups[(row["size"],digest)].append(row["id"])
                tested += 1
                consumed += row["size"]
            except (OSError,ValueError):
                errors += 1
        return {"ok":True,"candidate_files":len(rows),"tested_files":tested,
                "hashed_megabytes":round(consumed/1048576,2),"errors":errors,
                "complete":tested==len(rows) and errors==0,
                "groups":[{"ids":ids,"size":size,"sha256":digest} for (size,digest),ids in groups.items() if len(ids)>1][:50],
                "originals_modified":False}

    def health(args: dict[str, Any]) -> dict[str, Any]:
        data = old_status({})
        with db() as c:
            _extras(c)
            integrity = c.execute("PRAGMA quick_check").fetchone()[0]
            cached = c.execute("SELECT COUNT(*) FROM gallery_extra").fetchone()[0]
            last_audit = c.execute("SELECT * FROM gallery_audit ORDER BY id DESC LIMIT 1").fetchone()
        v = os.statvfs(str(state))
        return {**data,"engine":VERSION,"db_integrity":integrity,"details_cached":cached,
                "free_storage_gib":round(v.f_bavail*v.f_frsize/(1024**3),2),
                "last_scan_audit":dict(last_audit) if last_audit else None,
                "source_writable":False,"semantic_embeddings":False,"ocr_available":False}

    def search(args: dict[str, Any]) -> dict[str, Any]:
        # Metadata query. Optical / semantic content search is deliberately not claimed.
        page = old_list(args)
        page["search_mode"]="metadata"
        page["engine"]=VERSION
        return page

    def _no_thumb_dimensions(*unused: Any) -> None:
        # Rendered preview dimensions are NOT source photo dimensions.
        return None

    g["_update_dimensions"] = _no_thumb_dimensions
    g["gallery_scan"] = scan
    g["gallery_status"] = health
    g["gallery_metadata"] = metadata
    g["gallery_find_duplicates"] = duplicates
    g["gallery_similar"] = similar
    g["gallery_search"] = search
    tools["gallery_scan"] = (schema(), scan)
    tools["gallery_status"] = (schema(),health)
    tools["gallery_search"] = (tools["gallery_list"][0],search)
    tools["gallery_metadata"] = (schema({"id":{"type":"integer","minimum":1}},["id"]),metadata)
    tools["gallery_find_duplicates"] = (schema({
        "max_files":{"type":"integer","minimum":2,"maximum":1000},
        "max_megabytes":{"type":"integer","minimum":1,"maximum":1024}
    }),duplicates)
    tools["gallery_similar"] = (schema({
        "ids":{"type":"array","items":{"type":"integer","minimum":1},"minItems":1,"maxItems":64},
        "threshold":{"type":"integer","minimum":0,"maximum":64}
    },["ids"]),similar)
    descriptions.update({
      "gallery_search":"Search local image metadata. Does not claim visual semantic or OCR search.",
      "gallery_metadata":"Read actual original image dimensions and limited EXIF date; excludes GPS.",
      "gallery_find_duplicates":"Hash a bounded number of same-size files and report exact duplicates without deleting anything.",
      "gallery_similar":"Compare chosen images using on-device dHash; no biometric identification."
    })
    readonly.update({"gallery_search","gallery_metadata","gallery_find_duplicates","gallery_similar"})
