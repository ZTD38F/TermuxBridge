#!/usr/bin/env python3
"""Shizuku-backed read-only source for Android/data images.

Author: Dāvids Krūmiņš.
Uses official rish. Source files in Android/data are never modified.
A hidden shared-storage exchange file is created only for the duration of a read
and removed immediately afterwards.
"""
from __future__ import annotations
from contextlib import contextmanager
import os, secrets, shlex, subprocess, time
from pathlib import Path
from typing import Iterable

PREFIX = "shizuku://"
ANDROID_DATA = "/storage/emulated/0/Android/data"
RISH = Path.home() / "bin" / "rish"
EXCHANGE_DIR = Path("/storage/emulated/0/.sonoryx-shizuku")
EXCHANGE_PREFIX = "exchange-"

def is_virtual(value: str) -> bool:
    return str(value).startswith(PREFIX)

def to_virtual(path: str) -> str:
    if not path.startswith(ANDROID_DATA + "/"):
        raise ValueError("Shizuku source outside Android/data")
    return PREFIX + path

def to_absolute(value: str) -> str:
    if not is_virtual(value):
        raise ValueError("not a Shizuku source")
    path = value[len(PREFIX):]
    if not path.startswith(ANDROID_DATA + "/"):
        raise ValueError("invalid Shizuku source")
    return path

def _run(command: str, *, timeout: float = 30) -> subprocess.CompletedProcess:
    if not RISH.is_file():
        raise RuntimeError("rish unavailable")
    return subprocess.run([str(RISH), "-c", command],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout, check=False)

def _prepare_exchange() -> None:
    EXCHANGE_DIR.mkdir(parents=True, exist_ok=True)
    # Best-effort cleanup of stale exchange files from interrupted reads.
    cutoff = time.time() - 3600
    try:
        for p in EXCHANGE_DIR.iterdir():
            if p.name.startswith(EXCHANGE_PREFIX):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
    except OSError:
        pass

def _exchange_path() -> Path:
    _prepare_exchange()
    return EXCHANGE_DIR / f"{EXCHANGE_PREFIX}{secrets.token_hex(12)}.tmp"

def _remote_output(command: str, *, timeout: float = 60, attempts: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        target = _exchange_path()
        try:
            wrapped = f"( {command} ) > {shlex.quote(str(target))}"
            p = _run(wrapped, timeout=timeout)
            if p.returncode == 0 and target.is_file():
                return target.read_bytes()
            last = RuntimeError(f"rish exit {p.returncode}")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            last = exc
        finally:
            try:
                target.unlink()
            except OSError:
                pass
        if attempt + 1 < attempts:
            time.sleep(0.35)
    raise RuntimeError("Shizuku exchange failed") from last

def available() -> bool:
    try:
        p = _run(f"test -d {shlex.quote(ANDROID_DATA)}", timeout=8)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False

def enumerate_images(exts: Iterable[str], *, timeout: float = 120) -> list[tuple[str,int,float]]:
    exts = sorted({x.lower().lstrip(".") for x in exts if x})
    if not exts:
        return []
    tests = " -o ".join(f'-iname {shlex.quote("*."+ext)}' for ext in exts)
    manifest = _remote_output(
        f"find {shlex.quote(ANDROID_DATA)} -type f \\( {tests} \\) "
        r"-printf '%s\t%T@\t%p\0' 2>/dev/null",
        timeout=timeout, attempts=3)
    out: list[tuple[str,int,float]] = []
    for record in manifest.split(b"\0"):
        if not record:
            continue
        try:
            raw_size, raw_mtime, raw_path = record.split(b"\t", 2)
            path = raw_path.decode("utf-8", errors="surrogateescape")
            size = int(raw_size)
            mtime = float(raw_mtime)
        except (ValueError, OverflowError):
            continue
        if size > 0 and path.startswith(ANDROID_DATA + "/"):
            out.append((path,size,mtime))
    return out

def stat_virtual(value: str, *, timeout: float = 15) -> tuple[int,float]:
    path = to_absolute(value)
    raw = _remote_output(f"stat -c '%s %Y' -- {shlex.quote(path)}",
                         timeout=timeout, attempts=3)
    try:
        size_s, mtime_s = raw.decode().strip().split(None,1)
        return int(size_s), float(mtime_s)
    except Exception as exc:
        raise ValueError("invalid Shizuku stat") from exc

def validate(value: str, expected_size: int, expected_mtime: float) -> None:
    size, mtime = stat_virtual(value)
    if size != int(expected_size) or abs(mtime - float(expected_mtime)) > 1.1:
        raise ValueError("Shizuku source changed; rescan gallery")

@contextmanager
def materialize(value: str, expected_size: int, expected_mtime: float, state_dir: Path):
    del state_dir  # kept in signature for the generic gallery source abstraction
    validate(value, expected_size, expected_mtime)
    absolute = to_absolute(value)
    last: Exception | None = None
    target: Path | None = None
    try:
        for attempt in range(2):
            target = _exchange_path()
            try:
                p = _run(f"cp -- {shlex.quote(absolute)} {shlex.quote(str(target))}", timeout=180)
                if p.returncode == 0 and target.is_file() and target.stat().st_size == int(expected_size):
                    yield target
                    return
                last = ValueError("Shizuku read size mismatch")
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                last = exc
            finally:
                if target is not None and target.exists():
                    try:
                        target.unlink()
                    except OSError:
                        pass
            if attempt == 0:
                time.sleep(0.35)
        raise ValueError("Shizuku read failed") from last
    finally:
        if target is not None:
            try:
                target.unlink()
            except OSError:
                pass
