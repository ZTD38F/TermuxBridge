#!/usr/bin/env python3
"""Sonoryx local Gallery OCR + SQLite FTS5.
Author: Dāvids Krūmiņš. No photos modified or uploaded.
"""
from __future__ import annotations

import fcntl
import io
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

OCR_VERSION = "2.1.0"
RECOGNITION_LANGUAGES = ("rus", "lav", "eng")


def battery_status() -> dict[str, Any]:
    exe = shutil.which("termux-battery-status")
    if not exe:
        return {"available": False}
    try:
        import json
        data = json.loads(subprocess.run([exe], text=True, capture_output=True,
                                         check=True, timeout=5).stdout)
        level = int(data.get("percentage", data.get("level", -1)))
        plugged = str(data.get("plugged", "UNPLUGGED")).upper() not in ("UNPLUGGED", "NONE", "UNKNOWN", "")
        return {"available": True, "level": level, "charging": plugged}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"available": False}


def ocr_languages() -> list[str]:
    exe = shutil.which("tesseract")
    if not exe:
        return []
    try:
        result = subprocess.run([exe, "--list-langs"], text=True, capture_output=True, timeout=8, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return [s.strip() for s in result.stdout.splitlines()[1:] if s.strip()]


def _fts_terms(query: str) -> str:
    """Prevent FTS5 syntax injection while supporting unicode words."""
    terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    terms = terms[:8]
    if not terms:
        raise ValueError("OCR search requires at least one letter or digit")
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def install(g: dict[str, Any]) -> None:
    db = g["_db"]
    schema = g["_schema"]
    tools = g["TOOLS"]
    descriptions = g["DESCRIPTIONS"]
    readonly = g["READ_ONLY"]
    state: Path = g["STATE_DIR"]
    root: Path = g["SHARED_ROOT"]
    old_status = g["gallery_status"]
    old_search = g["gallery_search"]

    def _ensure(c: sqlite3.Connection) -> None:
        c.execute("""
            CREATE TABLE IF NOT EXISTS gallery_ocr_index (
                image_id INTEGER PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                languages TEXT NOT NULL,
                processed_at REAL NOT NULL,
                text_length INTEGER NOT NULL,
                error TEXT
            )""")
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS gallery_ocr_fts USING fts5(content, tokenize='unicode61')")
        c.execute("CREATE INDEX IF NOT EXISTS gallery_ocr_update_idx ON gallery_ocr_index(processed_at)")

    def status(_: dict[str, Any]) -> dict[str, Any]:
        result = old_status({})
        with db() as c:
            _ensure(c)
            c.execute("SELECT 1 FROM gallery_ocr_fts LIMIT 1")
            counts = c.execute("""
               SELECT COUNT(*) total, SUM(CASE WHEN o.error IS NULL THEN 1 ELSE 0 END) recognized,
                 SUM(CASE WHEN o.error IS NOT NULL THEN 1 ELSE 0 END) failed
               FROM gallery_ocr_index o JOIN images i ON i.id=o.image_id
               WHERE i.size=o.size AND i.mtime=o.mtime
            """).fetchone()
        languages = ocr_languages()
        return {**result, "engine": OCR_VERSION, "ocr_available": bool(languages),
                "ocr_languages": languages, "ocr_indexed": int(counts["recognized"] or 0),
                "ocr_failed": int(counts["failed"] or 0),
                "ocr_backend": "tesseract-local" if languages else "not-installed",
                "semantic_embeddings": False}

    def _photo_row(image_id: int) -> sqlite3.Row:
        with db() as c:
            row = c.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        if row is None:
            raise ValueError("unknown gallery image id")
        return row

    def _path(row: sqlite3.Row) -> Path:
        value = (root / row["path"])
        if value.is_symlink():
            raise ValueError("symbolic link is forbidden")
        p = value.resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise ValueError("photo unavailable")
        st = p.stat()
        if st.st_size != row["size"] or abs(st.st_mtime-row["mtime"]) > 0.00001:
            raise ValueError("photo changed; rescan gallery")
        return p

    def _recognize(path: Path, languages: str, timeout: float) -> str:
        if not g["PIL_AVAILABLE"]:
            raise RuntimeError("Pillow missing")
        with g["Image"].open(path) as source:
            if source.width * source.height > 45_000_000:
                raise ValueError("image pixel count exceeds OCR safety limit")
            image = g["ImageOps"].exif_transpose(source)
            image.thumbnail((1800, 1800), g["Image"].Resampling.LANCZOS)
            if image.mode != "RGB":
                image = image.convert("RGB")
            out = io.BytesIO()
            image.save(out, format="PNG")
            raw = out.getvalue()
            if len(raw) > 18 * 1024 * 1024:
                raise ValueError("prepared image too large")
        proc = subprocess.run(["tesseract", "stdin", "stdout", "-l", languages, "--psm", "3"],
                              input=raw, capture_output=True, timeout=timeout, check=False)
        if proc.returncode:
            raise RuntimeError("OCR engine error")
        return proc.stdout.decode("utf-8", errors="replace")[:16000].strip()

    def index(arguments: dict[str, Any]) -> dict[str, Any]:
        available = ocr_languages()
        if not available:
            return {"ok":False, "error":"Tesseract OCR unavailable"}
        lang_arg = arguments.get("languages", "rus+lav+eng")
        requested = [x.strip() for x in str(lang_arg).split("+")]
        if not requested or any(x not in RECOGNITION_LANGUAGES or x not in available for x in requested):
            raise ValueError("only installed rus, lav, eng languages are supported")
        languages = "+".join(dict.fromkeys(requested))
        cap = max(1, min(8, int(arguments.get("max_images", 2))))
        seconds = max(3, min(60, int(arguments.get("max_seconds", 24))))
        requested_ids = arguments.get("ids")
        if requested_ids is not None:
            if not isinstance(requested_ids, list) or not 1 <= len(requested_ids) <= 8:
                raise ValueError("ids must have 1..8 entries")
            cap = min(cap, len(requested_ids))
        battery = battery_status()
        # Power policy: never process at critical charge, even if USB/AC is connected.
        # Unattended OCR always requires a healthy battery and actual charging.
        level = battery.get("level", -1) if battery.get("available") else -1
        charging = battery.get("charging", False)
        if level < 15 or (requested_ids is None and (level < 35 or not charging)):
            return {"ok": False, "error": "OCR paused: battery too low or charging required",
                    "battery": battery, "minimum_battery": 35 if requested_ids is None else 15}
        if (not charging or level < 35) and (requested_ids is None or cap > 1):
            return {"ok":False,"error":"batch OCR requires charging and battery >=35%",
                    "battery":battery}
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (state / "gallery_ocr.lock").open("a+") as file_lock:
            try:
                fcntl.flock(file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"ok": False, "error": "OCR is already running"}
            with db() as c:
                _ensure(c)
                if requested_ids:
                    rows = [c.execute("SELECT * FROM images WHERE id=?", (int(x),)).fetchone() for x in requested_ids]
                    rows = [r for r in rows if r is not None]
                else:
                    album = str(arguments.get("album", "")).strip()
                    # Prefer screenshots for text search while handling all other image folders too.
                    sql = """SELECT i.* FROM images i
                         LEFT JOIN gallery_ocr_index o ON o.image_id=i.id
                         WHERE (o.image_id IS NULL OR o.size<>i.size OR o.mtime<>i.mtime OR o.languages<>?)
                         """
                    params: list[Any] = [languages]
                    if album:
                        sql += " AND i.album=?"
                        params.append(album)
                    sql += (" ORDER BY CASE WHEN lower(i.album) LIKE '%screenshot%' THEN 0 ELSE 1 END,"
                            " i.mtime DESC LIMIT ?")
                    params.append(cap)
                    rows = c.execute(sql, params).fetchall()
            processed: list[int] = []
            skipped: list[int] = []
            failed: list[dict[str, Any]] = []
            start = time.monotonic()
            for row in rows[:cap]:
                if time.monotonic()-start >= seconds - 2:
                    break
                with db() as c:
                    _ensure(c)
                    existing = c.execute("SELECT size,mtime,languages,error FROM gallery_ocr_index WHERE image_id=?",
                                         (row["id"],)).fetchone()
                if existing and existing["size"] == row["size"] and existing["mtime"] == row["mtime"] and existing["languages"] == languages and not existing["error"]:
                    skipped.append(row["id"])
                    continue
                try:
                    original = _path(row)
                    text = _recognize(original, languages, min(20.0, max(2.0, seconds-(time.monotonic()-start))))
                    # Check source didn't change while we were reading it.
                    _path(row)
                    with db() as c:
                        _ensure(c)
                        c.execute("DELETE FROM gallery_ocr_fts WHERE rowid=?", (row["id"],))
                        c.execute("INSERT INTO gallery_ocr_fts(rowid,content) VALUES(?,?)", (row["id"],text))
                        c.execute("""
                        INSERT INTO gallery_ocr_index(image_id,size,mtime,languages,processed_at,text_length,error)
                        VALUES (?,?,?,?,?,?,NULL)
                        ON CONFLICT(image_id) DO UPDATE SET
                          size=excluded.size,mtime=excluded.mtime,languages=excluded.languages,
                          processed_at=excluded.processed_at,text_length=excluded.text_length,error=NULL
                        """, (row["id"],row["size"],row["mtime"],languages,time.time(),len(text)))
                    processed.append(row["id"])
                except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
                    failed.append({"id":row["id"],"error":type(exc).__name__})
            return {"ok":True,"recognized_ids":processed,"skipped_cached_ids":skipped,
                    "errors":failed,"elapsed_seconds":round(time.monotonic()-start,2),
                    "battery":battery,"model":"tesseract-local","source_photos_modified":False}

    def text(args: dict[str, Any]) -> dict[str, Any]:
        image_id = int(args["id"])
        row = _photo_row(image_id)
        with db() as c:
            _ensure(c)
            doc = c.execute("""
                 SELECT o.*,f.content FROM gallery_ocr_index o
                 JOIN gallery_ocr_fts f ON f.rowid=o.image_id
                 WHERE o.image_id=? AND o.size=? AND o.mtime=? AND o.error IS NULL
                """, (image_id,row["size"],row["mtime"])).fetchone()
        if doc is None:
            return {"ok":False,"id":image_id,"error":"image not OCR-indexed or changed"}
        max_chars = max(1,min(16000,int(args.get("max_chars",8000))))
        return {"ok":True,"id":image_id,"text":doc["content"][:max_chars],
                "truncated":len(doc["content"])>max_chars,
                "languages":doc["languages"],"processed_at":doc["processed_at"]}

    def search(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("search query must not be empty")
        match = _fts_terms(query)
        limit = max(1,min(100,int(args.get("limit",30))))
        offset = max(0,min(100000,int(args.get("offset",0))))
        album = str(args.get("album", "")).strip()
        where = ["gallery_ocr_fts MATCH ?", "i.size=o.size", "i.mtime=o.mtime"]
        params: list[Any] = [match]
        if album:
            where.append("i.album=?")
            params.append(album)
        clause = " AND ".join(where)
        with db() as c:
            _ensure(c)
            total = c.execute(f"""SELECT COUNT(*) FROM gallery_ocr_fts
                JOIN gallery_ocr_index o ON o.image_id=gallery_ocr_fts.rowid
                JOIN images i ON i.id=o.image_id WHERE {clause}""",params).fetchone()[0]
            rows = c.execute(f"""SELECT i.* FROM gallery_ocr_fts
                JOIN gallery_ocr_index o ON o.image_id=gallery_ocr_fts.rowid
                JOIN images i ON i.id=o.image_id WHERE {clause}
                ORDER BY bm25(gallery_ocr_fts),i.mtime DESC LIMIT ? OFFSET ?""",[*params,limit,offset]).fetchall()
        return {"ok":True,"total":total,"count":len(rows),"items":[g["_item"](r) for r in rows],
                "offset":offset,"next_offset":offset+len(rows) if offset+len(rows)<total else None,
                "search_mode":"local-ocr-fts5"}

    def combined(args: dict[str, Any]) -> dict[str, Any]:
        # Combining metadata and OCR on ID allows normal gallery searches to find text inside screenshots.
        query = str(args.get("query","")).strip()
        if not query:
            return old_search(args)
        try:
            match = _fts_terms(query)
        except ValueError:
            return old_search(args)
        limit = max(1,min(200,int(args.get("limit",50))))
        offset = max(0,min(100000,int(args.get("offset",0))))
        album = str(args.get("album","")).strip()
        needle = "%" + query.lower() + "%"
        conditions = ["""(lower(i.name) LIKE ? OR lower(i.album) LIKE ? OR lower(i.path) LIKE ?
            OR i.id IN (SELECT o.image_id FROM gallery_ocr_fts
            JOIN gallery_ocr_index o ON o.image_id=gallery_ocr_fts.rowid
            JOIN images chk ON chk.id=o.image_id
            WHERE gallery_ocr_fts MATCH ? AND o.size=chk.size AND o.mtime=chk.mtime))"""]
        params: list[Any] = [needle,needle,needle,match]
        if album:
            conditions.append("i.album=?")
            params.append(album)
        # Keep date and sort handling aligned with the legacy metadata API.
        start = g["_parse_date"](args.get("date_from"))
        end = g["_parse_date"](args.get("date_to"))
        if start is not None:
            conditions.append("i.mtime>=?")
            params.append(start)
        if end is not None:
            conditions.append("i.mtime<=?")
            params.append(end)
        order = {"newest":"i.mtime DESC,i.id DESC","oldest":"i.mtime ASC,i.id ASC",
                 "name":"lower(i.name) ASC,i.id ASC","size":"i.size DESC,i.id DESC"}.get(args.get("sort","newest"))
        if order is None:
            raise ValueError("invalid sort")
        clause = " AND ".join(conditions)
        with db() as c:
            _ensure(c)
            total = c.execute(f"SELECT COUNT(*) FROM images i WHERE {clause}",params).fetchone()[0]
            rows = c.execute(f"SELECT i.* FROM images i WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                             [*params,limit,offset]).fetchall()
        return {"ok":True,"total":total,"count":len(rows),"offset":offset,
                "next_offset":offset+len(rows) if offset+len(rows)<total else None,
                "items":[g["_item"](r) for r in rows],"search_mode":"metadata+indexed-ocr",
                "note":"OCR matches only previously processed images; not full semantic image understanding"}

    g["gallery_ocr_status"] = status
    g["gallery_ocr_index"] = index
    g["gallery_ocr_text"] = text
    g["gallery_ocr_search"] = search
    g["gallery_search"] = combined
    g["gallery_status"] = status
    tools["gallery_status"] = (schema(),status)
    tools["gallery_search"] = (tools["gallery_search"][0],combined)
    tools["gallery_ocr_status"] = (schema(),status)
    tools["gallery_ocr_index"] = (schema({
        "ids":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"integer","minimum":1}},
        "album":{"type":"string"},
        "max_images":{"type":"integer","minimum":1,"maximum":8,"default":2},
        "max_seconds":{"type":"integer","minimum":3,"maximum":60,"default":24},
        "languages":{"type":"string","default":"rus+lav+eng"}
    }),index)
    tools["gallery_ocr_text"] = (schema({"id":{"type":"integer","minimum":1},
                                      "max_chars":{"type":"integer","minimum":1,"maximum":16000}},["id"]),text)
    tools["gallery_ocr_search"] = (schema({
        "query":{"type":"string"},
        "album":{"type":"string"},
        "limit":{"type":"integer","minimum":1,"maximum":100},
        "offset":{"type":"integer","minimum":0}
    },["query"]),search)
    descriptions.update({
        "gallery_ocr_status":"Show available local OCR languages and progress without revealing image text.",
        "gallery_ocr_index":"Index OCR text on local phone in a bounded batch, respecting low battery; never uploads originals.",
        "gallery_ocr_text":"Get cached OCR text for one image, never original pixels.",
        "gallery_ocr_search":"Search cached local Russian, Latvian and English OCR text with SQLite FTS5.",
        "gallery_search":"Search local image metadata and already-indexed OCR text, with date and album filters."
    })
    readonly.update({"gallery_ocr_status","gallery_ocr_text","gallery_ocr_search"})
