#!/usr/bin/env python3
"""Read-only local Android gallery tools for TermuxBridge.

The photo library itself is never modified. The only write is a private SQLite
metadata index under ~/.termux-mcp-bridge/.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
    PIL_AVAILABLE = True
except ImportError:
    Image = ImageDraw = ImageFont = ImageOps = None
    UnidentifiedImageError = OSError
    PIL_AVAILABLE = False

SHARED_ROOT = Path("/storage/emulated/0").resolve()
STATE_DIR = Path.home() / ".termux-mcp-bridge"
DB_PATH = STATE_DIR / "gallery.sqlite3"

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif",
}
SKIP_DIR_NAMES = {
    "data", "obb", "cache", "caches", "code_cache", "tmp", "temp",
    ".thumbnails", ".thumbnail", ".trash", ".globaltrash", "lost+found",
}
MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif",
}


def _schema(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            album TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            seen INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            mime TEXT
        );
        CREATE INDEX IF NOT EXISTS images_mtime_idx ON images(mtime DESC);
        CREATE INDEX IF NOT EXISTS images_album_idx ON images(album);
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return conn


def _visible_dir(name: str, parent_rel: str) -> bool:
    lower = name.lower()
    if name.startswith("."):
        return False
    if lower in SKIP_DIR_NAMES:
        return False
    if parent_rel.lower() == "android" and lower in {"data", "obb"}:
        return False
    return True


def _iter_images():
    if not SHARED_ROOT.is_dir():
        return
    for root, dirs, files in os.walk(SHARED_ROOT, topdown=True, followlinks=False):
        root_path = Path(root)
        try:
            rel_root = str(root_path.relative_to(SHARED_ROOT))
        except ValueError:
            continue
        dirs[:] = [d for d in dirs if _visible_dir(d, rel_root)]
        for name in files:
            path = root_path / name
            if path.suffix.lower() not in IMAGE_EXTS:
                continue
            try:
                stat = path.stat()
            except (OSError, PermissionError):
                continue
            if stat.st_size <= 0:
                continue
            yield path, stat


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(SHARED_ROOT))


def _album_for(path: Path) -> str:
    rel_parent = path.resolve().parent.relative_to(SHARED_ROOT)
    value = str(rel_parent)
    return value if value != "." else "Shared storage"


def _ensure_index() -> None:
    with _db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    if count == 0:
        gallery_scan({})


def gallery_scan(_: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    scan_id = time.time_ns()
    scanned = 0
    skipped = 0
    with _db() as conn:
        for path, stat in _iter_images() or []:
            try:
                rel = _relative(path)
                album = _album_for(path)
                ext = path.suffix.lower()
                conn.execute(
                    """
                    INSERT INTO images(path,name,album,size,mtime,seen,mime)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        name=excluded.name,
                        album=excluded.album,
                        size=excluded.size,
                        mtime=excluded.mtime,
                        seen=excluded.seen,
                        mime=excluded.mime
                    """,
                    (rel, path.name, album, int(stat.st_size), float(stat.st_mtime), scan_id, MIME_BY_EXT.get(ext)),
                )
                scanned += 1
                if scanned % 1000 == 0:
                    conn.commit()
            except (OSError, ValueError, sqlite3.Error):
                skipped += 1
        removed = conn.execute("SELECT COUNT(*) FROM images WHERE seen<>?", (scan_id,)).fetchone()[0]
        conn.execute("DELETE FROM images WHERE seen<>?", (scan_id,))
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(time.time()),),
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    return {
        "ok": True,
        "indexed": total,
        "scanned": scanned,
        "removed_missing": removed,
        "skipped": skipped,
        "seconds": round(time.time() - started, 2),
        "source": str(SHARED_ROOT),
        "photos_modified": False,
    }


def gallery_status(_: dict[str, Any]) -> dict[str, Any]:
    with _db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        albums = conn.execute("SELECT COUNT(DISTINCT album) FROM images").fetchone()[0]
        row = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
        newest = conn.execute("SELECT MAX(mtime) FROM images").fetchone()[0]
    return {
        "ok": True,
        "indexed": count,
        "albums": albums,
        "last_scan": float(row[0]) if row else None,
        "newest_mtime": newest,
        "source": str(SHARED_ROOT),
        "database": str(DB_PATH),
        "read_only_photos": True,
        "pillow_available": PIL_AVAILABLE,
    }


def _parse_date(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit():
        return float(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("date filters must be ISO-8601 or Unix seconds") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _query_rows(arguments: dict[str, Any], *, max_limit: int = 200) -> tuple[list[sqlite3.Row], int]:
    _ensure_index()
    clauses = ["1=1"]
    params: list[Any] = []
    query = str(arguments.get("query", "")).strip().lower()
    if query:
        clauses.append("(LOWER(name) LIKE ? OR LOWER(album) LIKE ? OR LOWER(path) LIKE ?)")
        needle = f"%{query}%"
        params.extend([needle, needle, needle])
    album = str(arguments.get("album", "")).strip()
    if album:
        clauses.append("album = ?")
        params.append(album)
    date_from = _parse_date(arguments.get("date_from"))
    date_to = _parse_date(arguments.get("date_to"))
    if date_from is not None:
        clauses.append("mtime >= ?")
        params.append(date_from)
    if date_to is not None:
        clauses.append("mtime <= ?")
        params.append(date_to)
    sort = str(arguments.get("sort", "newest"))
    order = {
        "newest": "mtime DESC, id DESC",
        "oldest": "mtime ASC, id ASC",
        "name": "LOWER(name) ASC, id ASC",
        "size": "size DESC, id DESC",
    }.get(sort)
    if order is None:
        raise ValueError("sort must be newest, oldest, name, or size")
    limit = max(1, min(int(arguments.get("limit", 50)), max_limit))
    offset = max(0, int(arguments.get("offset", 0)))
    where = " AND ".join(clauses)
    with _db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM images WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM images WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return rows, total


def _item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "album": row["album"],
        "path": row["path"],
        "size": row["size"],
        "mtime": row["mtime"],
        "width": row["width"],
        "height": row["height"],
        "mime": row["mime"],
    }


def gallery_list(arguments: dict[str, Any]) -> dict[str, Any]:
    rows, total = _query_rows(arguments, max_limit=200)
    offset = max(0, int(arguments.get("offset", 0)))
    return {
        "ok": True,
        "total": total,
        "offset": offset,
        "count": len(rows),
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        "items": [_item(row) for row in rows],
    }


def gallery_albums(arguments: dict[str, Any]) -> dict[str, Any]:
    _ensure_index()
    query = str(arguments.get("query", "")).strip().lower()
    limit = max(1, min(int(arguments.get("limit", 100)), 500))
    with _db() as conn:
        if query:
            rows = conn.execute(
                """
                SELECT album,COUNT(*) AS count,MAX(mtime) AS newest
                FROM images WHERE LOWER(album) LIKE ?
                GROUP BY album ORDER BY count DESC, album ASC LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT album,COUNT(*) AS count,MAX(mtime) AS newest
                FROM images GROUP BY album ORDER BY count DESC, album ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return {
        "ok": True,
        "albums": [{"album": row["album"], "count": row["count"], "newest_mtime": row["newest"]} for row in rows],
    }


def _row_by_id(image_id: int) -> sqlite3.Row:
    _ensure_index()
    with _db() as conn:
        row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown gallery image id: {image_id}")
    path = (SHARED_ROOT / row["path"]).resolve()
    if not path.is_file() or SHARED_ROOT not in path.parents:
        raise ValueError("indexed image is no longer available; run gallery_scan")
    return row


def _require_pillow() -> None:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is required for gallery image rendering; metadata browsing still works")


def _render_jpeg(path: Path, max_edge: int, quality: int) -> tuple[bytes, int, int]:
    _require_pillow()
    try:
        with Image.open(path) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            if image.mode not in ("RGB", "L"):
                if "A" in image.getbands():
                    background = Image.new("RGB", image.size, "white")
                    rgba = image.convert("RGBA")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
            elif image.mode == "L":
                image = image.convert("RGB")
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            width, height = image.size
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue(), width, height
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"cannot decode image: {path.name}") from exc


def _update_dimensions(image_id: int, width: int, height: int) -> None:
    with _db() as conn:
        conn.execute("UPDATE images SET width=?,height=? WHERE id=?", (width, height, image_id))
        conn.commit()


def _image_result(row: sqlite3.Row, *, max_edge: int, quality: int) -> dict[str, Any]:
    path = (SHARED_ROOT / row["path"]).resolve()
    data, width, height = _render_jpeg(path, max_edge, quality)
    _update_dimensions(int(row["id"]), width, height)
    metadata = _item(row)
    metadata.update({"rendered_width": width, "rendered_height": height, "rendered_mime": "image/jpeg"})
    return {
        "__mcp_content__": [
            {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/jpeg"},
            {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)},
        ]
    }


def gallery_thumbnail(arguments: dict[str, Any]) -> dict[str, Any]:
    image_id = int(arguments["id"])
    max_edge = max(128, min(int(arguments.get("max_edge", 768)), 1280))
    return _image_result(_row_by_id(image_id), max_edge=max_edge, quality=82)


def gallery_get_image(arguments: dict[str, Any]) -> dict[str, Any]:
    image_id = int(arguments["id"])
    max_edge = max(512, min(int(arguments.get("max_edge", 3072)), 4096))
    quality = max(70, min(int(arguments.get("quality", 90)), 95))
    return _image_result(_row_by_id(image_id), max_edge=max_edge, quality=quality)


def gallery_get_images(arguments: dict[str, Any]) -> dict[str, Any]:
    ids = arguments.get("ids", [])
    if not isinstance(ids, list) or not 1 <= len(ids) <= 12:
        raise ValueError("ids must contain 1-12 gallery image ids")
    max_edge = max(384, min(int(arguments.get("max_edge", 1600)), 2048))
    quality = max(65, min(int(arguments.get("quality", 84)), 92))
    content: list[dict[str, Any]] = []
    metadata = []
    for raw_id in ids:
        row = _row_by_id(int(raw_id))
        path = (SHARED_ROOT / row["path"]).resolve()
        data, width, height = _render_jpeg(path, max_edge, quality)
        _update_dimensions(int(row["id"]), width, height)
        content.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/jpeg"})
        metadata.append({**_item(row), "rendered_width": width, "rendered_height": height})
    content.append({"type": "text", "text": json.dumps({"items": metadata}, ensure_ascii=False)})
    return {"__mcp_content__": content}


def gallery_contact_sheet(arguments: dict[str, Any]) -> dict[str, Any]:
    _require_pillow()
    args = dict(arguments)
    args["limit"] = max(1, min(int(arguments.get("limit", 36)), 64))
    rows, total = _query_rows(args, max_limit=64)
    if not rows:
        return {"ok": True, "total": total, "count": 0, "message": "No matching images"}
    cell_w = 240
    image_h = 180
    label_h = 30
    cols = max(1, min(int(arguments.get("columns", 6)), 8))
    cols = min(cols, len(rows))
    grid_rows = math.ceil(len(rows) / cols)
    sheet = Image.new("RGB", (cols * cell_w, grid_rows * (image_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=18)
    mapped = []
    for index, row in enumerate(rows):
        x0 = (index % cols) * cell_w
        y0 = (index // cols) * (image_h + label_h)
        path = (SHARED_ROOT / row["path"]).resolve()
        try:
            with Image.open(path) as source:
                source.seek(0)
                thumb = ImageOps.exif_transpose(source).convert("RGB")
                thumb.thumbnail((cell_w, image_h), Image.Resampling.LANCZOS)
                px = x0 + (cell_w - thumb.width) // 2
                py = y0 + (image_h - thumb.height) // 2
                sheet.paste(thumb, (px, py))
        except Exception:
            draw.rectangle((x0, y0, x0 + cell_w - 1, y0 + image_h - 1), outline="black")
            draw.text((x0 + 8, y0 + 8), "unreadable", fill="black", font=font)
        label = f"#{row['id']}"
        draw.rectangle((x0, y0 + image_h, x0 + cell_w - 1, y0 + image_h + label_h - 1), fill="white")
        draw.text((x0 + 6, y0 + image_h + 5), label, fill="black", font=font)
        mapped.append({"id": row["id"], "name": row["name"], "album": row["album"], "mtime": row["mtime"]})
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=82, optimize=True)
    offset = max(0, int(arguments.get("offset", 0)))
    metadata = {
        "total": total,
        "offset": offset,
        "count": len(rows),
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        "items": mapped,
    }
    return {
        "__mcp_content__": [
            {"type": "image", "data": base64.b64encode(out.getvalue()).decode("ascii"), "mimeType": "image/jpeg"},
            {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)},
        ]
    }


FILTER_PROPS = {
    "query": {"type": "string", "description": "Metadata substring matched against filename, album, and relative path."},
    "album": {"type": "string", "description": "Exact album/folder value returned by gallery_albums."},
    "date_from": {"type": "string", "description": "ISO-8601 date/time or Unix seconds, based on file modification time."},
    "date_to": {"type": "string", "description": "ISO-8601 date/time or Unix seconds, based on file modification time."},
    "sort": {"type": "string", "enum": ["newest", "oldest", "name", "size"], "default": "newest"},
    "offset": {"type": "integer", "minimum": 0, "default": 0},
}

TOOLS: dict[str, tuple[dict[str, Any], Any]] = {
    "gallery_status": (_schema(), gallery_status),
    "gallery_scan": (_schema(), gallery_scan),
    "gallery_albums": (_schema({
        "query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
    }), gallery_albums),
    "gallery_list": (_schema({
        **FILTER_PROPS,
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
    }), gallery_list),
    "gallery_thumbnail": (_schema({
        "id": {"type": "integer", "minimum": 1},
        "max_edge": {"type": "integer", "minimum": 128, "maximum": 1280, "default": 768},
    }, ["id"]), gallery_thumbnail),
    "gallery_get_image": (_schema({
        "id": {"type": "integer", "minimum": 1},
        "max_edge": {"type": "integer", "minimum": 512, "maximum": 4096, "default": 3072},
        "quality": {"type": "integer", "minimum": 70, "maximum": 95, "default": 90},
    }, ["id"]), gallery_get_image),
    "gallery_get_images": (_schema({
        "ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 12},
        "max_edge": {"type": "integer", "minimum": 384, "maximum": 2048, "default": 1600},
        "quality": {"type": "integer", "minimum": 65, "maximum": 92, "default": 84},
    }, ["ids"]), gallery_get_images),
    "gallery_contact_sheet": (_schema({
        **FILTER_PROPS,
        "limit": {"type": "integer", "minimum": 1, "maximum": 64, "default": 36},
        "columns": {"type": "integer", "minimum": 1, "maximum": 8, "default": 6},
    }), gallery_contact_sheet),
}

DESCRIPTIONS = {
    "gallery_status": "Read the local photo-index status. The source photos are never modified.",
    "gallery_scan": "Refresh a private metadata index of images visible in Android shared storage. This never modifies, deletes, or uploads photos.",
    "gallery_albums": "List indexed Android gallery folders/albums with image counts.",
    "gallery_list": "List indexed local photos with pagination and metadata filters. Query is metadata-only, not semantic image search.",
    "gallery_thumbnail": "Return one local gallery photo as a small image for visual inspection.",
    "gallery_get_image": "Return one local gallery photo as a high-quality rendered image for visual inspection, bounded to avoid oversized responses.",
    "gallery_get_images": "Return 1-12 selected local gallery photos as image content in one call.",
    "gallery_contact_sheet": "Return a labeled contact sheet for up to 64 local photos so ChatGPT can visually scan a large gallery page efficiently.",
}

READ_ONLY = {
    "gallery_status", "gallery_albums", "gallery_list", "gallery_thumbnail",
    "gallery_get_image", "gallery_get_images", "gallery_contact_sheet",
}

# Sonoryx Photo Intelligence v2 — Dāvids Krūmiņš.
from gallery_intelligence import install as _install_sonoryx_gallery
_install_sonoryx_gallery(globals())

# On-device OCR and full-text search; author Dāvids Krūmiņš.
from gallery_ocr import install as _install_sonoryx_ocr
_install_sonoryx_ocr(globals())
