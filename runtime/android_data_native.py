#!/usr/bin/env python3
"""Read-only Android/data capability diagnostic for ordinary Termux UID.

Unicode folding can expose directory metadata on some Android/FUSE builds.
It does not imply that individual app files can be opened. Never modify
storage, never claim successful content access without reading a file byte.

Author: @dvkrum
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import stat
from pathlib import Path

ROOT = Path("/storage/emulated/0/Android/da\u200bta")


def confined(root: Path, user_path: str) -> Path:
    """Reject traversal and symlink escapes, including the final component."""
    if not user_path or user_path.startswith("/") or "\x00" in user_path:
        raise ValueError("expected a non-empty relative path")
    pieces = user_path.split("/")
    if any(p in ("", ".", "..") for p in pieces):
        raise ValueError("unsafe path component")
    real_root = root.resolve(strict=True)
    candidate = root.joinpath(*pieces)
    # resolve() follows child symlinks: no path outside the selected root.
    real_target = candidate.resolve(strict=False)
    if not real_target.is_relative_to(real_root):
        raise ValueError("path escapes Android/data root")
    return candidate


def status(root: Path = ROOT) -> dict:
    """Enumerate only immediate metadata; never present metadata as file access."""
    counts = {"entries": 0, "directories": 0, "descendable": 0}
    try:
        with os.scandir(root) as items:
            for item in items:
                counts["entries"] += 1
                if item.is_dir(follow_symlinks=False):
                    counts["directories"] += 1
                    try:
                        with os.scandir(item.path) as children:
                            next(children, None)
                        counts["descendable"] += 1
                    except OSError:
                        pass
    except OSError as exc:
        return {"ok": False, "metadata_access": False,
                "file_bytes_verified": False, "errno": exc.errno}
    return {"ok": True, "metadata_access": True,
            "file_bytes_verified": False,
            "file_access_status": "UNVERIFIED",
            **counts}


def list_dir(root: Path, user_path: str = "", limit: int = 200) -> dict:
    output: list[dict] = []
    try:
        candidate = confined(root, user_path) if user_path else root
        with os.scandir(candidate) as entries:
            for i, entry in enumerate(entries):
                if i >= limit:
                    return {"ok": True, "truncated": True, "items": output}
                output.append({"name": entry.name, "directory":
                               entry.is_dir(follow_symlinks=False)})
        return {"ok": True, "truncated": False, "items": output}
    except ValueError:
        return {"ok": False, "reason": "INVALID_PATH", "items": []}
    except OSError as exc:
        return {"ok": False, "errno": exc.errno, "items": []}


def read_probe(root: Path, user_path: str) -> dict:
    """Read exactly one byte for proof; do not output or store its contents."""
    try:
        target = confined(root, user_path)
        # Ensure no modification, no symlink at leaf, no device traversal.
        fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {"ok": False, "file_bytes_verified": False,
                        "reason": "NOT_REGULAR_FILE"}
            was_read = bool(os.read(fd, 1))
        finally:
            os.close(fd)
        return {"ok": was_read, "file_bytes_verified": was_read,
                "reason": "READABLE" if was_read else "EMPTY_FILE"}
    except ValueError:
        return {"ok": False, "file_bytes_verified": False,
                "reason": "INVALID_PATH"}
    except OSError as exc:
        return {"ok": False, "file_bytes_verified": False,
                "reason": errno.errorcode.get(exc.errno, "OS_ERROR"),
                "errno": exc.errno}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", nargs="?", default="status",
                    choices=("status", "list", "probe-file"))
    ap.add_argument("relative_path", nargs="?", default="")
    args = ap.parse_args(argv)
    if args.command == "status":
        result = status()
    elif args.command == "list":
        try:
            result = list_dir(ROOT, args.relative_path)
        except (OSError, ValueError):
            result = {"ok": False, "reason": "INVALID_PATH", "items": []}
    else:
        result = read_probe(ROOT, args.relative_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
