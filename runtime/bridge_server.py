#!/usr/bin/env python3
"""Small dependency-free MCP stdio server for a private Termux bridge."""
from __future__ import annotations

BRIDGE_VERSION = "1.2.6"

import hashlib
import hmac
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(os.environ.get("TERMUX_BRIDGE_ROOT", Path.home())).resolve()
SHARED_ROOT = Path("/storage/emulated/0").resolve()
ALLOWED_ROOTS = (ROOT, SHARED_ROOT)
JOBS = Path(os.environ.get("TERMUX_BRIDGE_JOBS", ROOT / ".termux-mcp-bridge" / "jobs")).resolve()
MAX_READ = 512_000
MAX_OUTPUT = 256_000
COMMAND_MODE = os.environ.get("TERMUX_BRIDGE_COMMAND_MODE", "open").lower()
BACKEND_TOKEN_FILE = Path(os.environ.get("TERMUX_BRIDGE_BACKEND_TOKEN_FILE", Path.home() / "termux-mcp-bridge" / "secrets" / "backend_token")).resolve()


def _backend_token() -> str:
    value = BACKEND_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError("invalid backend authentication secret")
    return value


def _backend_authorized(headers) -> bool:
    try:
        return hmac.compare_digest(_backend_token(), headers.get("X-Bridge-Backend-Token", ""))
    except Exception:
        return False



def inside(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if not any(p == allowed or allowed in p.parents for allowed in ALLOWED_ROOTS):
        roots = ", ".join(str(allowed) for allowed in ALLOWED_ROOTS)
        raise ValueError(f"Path is outside allowed roots: {roots}")
    return p


def text_result(value, *, error=False):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def schema(props=None, required=None):
    return {"type": "object", "properties": props or {}, "required": required or [], "additionalProperties": False}


TOOLS = [
    {"name": "termux_status", "title": "Termux bridge status", "description": "Use this to verify the phone bridge, allowed root, Python, and active jobs.", "inputSchema": schema(), "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "list_files", "title": "List phone files", "description": "List files inside the configured Termux root. Does not read file contents.", "inputSchema": schema({"path": {"type": "string", "default": "."}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}}), "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "read_text", "title": "Read a text file", "description": "Read a bounded UTF-8 text file inside the configured Termux root.", "inputSchema": schema({"path": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_READ, "default": 100000}}, ["path"]), "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "search_text", "title": "Search text files", "description": "Search for a literal string in bounded text files inside the Termux root.", "inputSchema": schema({"path": {"type": "string", "default": "."}, "query": {"type": "string", "minLength": 1, "maxLength": 500}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}}, ["query"]), "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "write_text", "title": "Write a text file safely", "description": "Create or replace one text file inside the allowed root. Existing files are backed up; expected_sha256 prevents stale overwrites.", "inputSchema": schema({"path": {"type": "string"}, "content": {"type": "string"}, "expected_sha256": {"type": "string"}}, ["path", "content"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "run_command", "title": "Run a Termux command", "description": "Run any executable available to the Termux user. For shell syntax, pass bash -lc as argv. Android/Termux OS permissions still apply; no root is assumed.", "inputSchema": schema({"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 200}, "cwd": {"type": "string", "default": "."}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800, "default": 30}}, ["argv"]), "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    {"name": "start_job", "title": "Start a Termux background job", "description": "Start any executable available to the Termux user as a detached background job. For shell syntax, pass bash -lc as argv.", "inputSchema": schema({"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 200}, "cwd": {"type": "string", "default": "."}}, ["argv"]), "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    {"name": "job_status", "title": "Check background job", "description": "Check whether a bridge job is still running.", "inputSchema": schema({"job_id": {"type": "string", "pattern": "^[0-9]{8}T[0-9]{6}Z-[0-9]+$"}}, ["job_id"]), "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "read_job_log", "title": "Read background job log", "description": "Read the tail of a background job log without exposing bridge secrets.", "inputSchema": schema({"job_id": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT, "default": 20000}}, ["job_id"]), "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "spotify_migrator", "title": "Run Spotify safe migrator", "description": "Run snapshot, resolve, plan, apply, verify, or status using spotify_v31_safe.py. Apply is a write action and ChatGPT should ask for confirmation.", "inputSchema": schema({"action": {"type": "string", "enum": ["snapshot", "resolve", "plan", "apply", "verify", "status"]}, "project_dir": {"type": "string", "default": "storage/downloads/Spotify_V3.1/work_v31/package"}, "background": {"type": "boolean", "default": True}}, ["action"]), "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}},
]


# Load the typed no-root Android controller. It deliberately keeps Android
# security boundaries and action-bound confirmations for sensitive operations.
PHONE_SERVER = ROOT / "ai-phone-control" / "mcp-server" / "server.py"
PHONE_MODULE = None
PHONE_TOOLS = {}
if PHONE_SERVER.is_file():
    _phone_spec = importlib.util.spec_from_file_location("phone_control_bridge", PHONE_SERVER)
    if _phone_spec and _phone_spec.loader:
        PHONE_MODULE = importlib.util.module_from_spec(_phone_spec)
        _phone_spec.loader.exec_module(PHONE_MODULE)
        PHONE_TOOLS = PHONE_MODULE.TOOLS
        _phone_read_only = {
            "phone_health", "get_device_state", "get_battery", "get_network_state",
            "list_apps", "get_setting", "get_hotspot_state", "audit_tail",
            "organizer_summary",
        }
        _phone_open_world = {"open_app", "open_settings_page", "import_chatgpt_export"}
        for _name, (_input_schema, _handler) in PHONE_TOOLS.items():
            TOOLS.append({
                "name": f"phone_{_name}",
                "title": f"Phone: {_name.replace('_', ' ')}",
                "description": PHONE_MODULE.DESCRIPTIONS[_name],
                "inputSchema": _input_schema,
                "annotations": {
                    "readOnlyHint": _name in _phone_read_only,
                    "destructiveHint": _name in {"set_setting", "stop_app", "tap", "swipe", "type_text"},
                    "openWorldHint": _name in _phone_open_world,
                },
            })


GOOGLE_SERVER = Path(__file__).resolve().with_name("google_bridge_tools.py")
GOOGLE_MODULE = None
GOOGLE_TOOLS = {}
if GOOGLE_SERVER.is_file():
    _google_spec = importlib.util.spec_from_file_location("google_bridge_tools", GOOGLE_SERVER)
    if _google_spec and _google_spec.loader:
        GOOGLE_MODULE = importlib.util.module_from_spec(_google_spec)
        _google_spec.loader.exec_module(GOOGLE_MODULE)
        GOOGLE_TOOLS = GOOGLE_MODULE.TOOLS
        for _name, (_input_schema, _handler) in GOOGLE_TOOLS.items():
            TOOLS.append({
                "name": _name,
                "title": _name.replace("_", " ").title(),
                "description": GOOGLE_MODULE.DESCRIPTIONS[_name],
                "inputSchema": _input_schema,
                "annotations": {
                    "readOnlyHint": _name in {"google_tasks_list_tasklists", "google_tasks_list_tasks", "google_keep_list_notes", "google_keep_search_notes", "google_maps_search"},
                    "destructiveHint": _name == "google_tasks_delete_task",
                    "openWorldHint": True,
                },
            })



GALLERY_SERVER = Path(__file__).resolve().with_name("gallery_bridge_tools.py")
GALLERY_MODULE = None
GALLERY_TOOLS = {}
if GALLERY_SERVER.is_file():
    _gallery_spec = importlib.util.spec_from_file_location("gallery_bridge_tools", GALLERY_SERVER)
    if _gallery_spec and _gallery_spec.loader:
        GALLERY_MODULE = importlib.util.module_from_spec(_gallery_spec)
        _gallery_spec.loader.exec_module(GALLERY_MODULE)
        GALLERY_TOOLS = GALLERY_MODULE.TOOLS
        for _name, (_input_schema, _handler) in GALLERY_TOOLS.items():
            TOOLS.append({
                "name": _name,
                "title": _name.replace("_", " ").title(),
                "description": GALLERY_MODULE.DESCRIPTIONS[_name],
                "inputSchema": _input_schema,
                "annotations": {
                    "readOnlyHint": _name in GALLERY_MODULE.READ_ONLY,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
            })


def gallery_virtual_read(path: str, max_chars: int = 100000):
    """Backward-compatible image access through the long-lived read_text tool.

    Existing ChatGPT sessions can keep using the already-discovered read_text
    tool even when the MCP client has not refreshed the newly added gallery_*
    tool list yet.
    """
    if GALLERY_MODULE is None:
        raise ValueError("Gallery bridge is unavailable")
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(path)
    if parsed.scheme != "gallery":
        raise ValueError("Not a gallery virtual path")
    route = (parsed.netloc + parsed.path).strip("/")
    query = parse_qs(parsed.query, keep_blank_values=False)

    def one(name, default=None):
        values = query.get(name)
        return values[-1] if values else default

    if route.startswith("image/"):
        image_id = int(route.split("/", 1)[1])
        edge = int(one("max_edge", "3072"))
        quality = int(one("quality", "90"))
        return GALLERY_MODULE.gallery_get_image({"id": image_id, "max_edge": edge, "quality": quality})

    if route.startswith("thumbnail/"):
        image_id = int(route.split("/", 1)[1])
        edge = int(one("max_edge", "768"))
        return GALLERY_MODULE.gallery_thumbnail({"id": image_id, "max_edge": edge})

    if route in {"contact-sheet", "contact_sheet"}:
        args = {
            "limit": int(one("limit", "36")),
            "offset": int(one("offset", "0")),
            "columns": int(one("columns", "6")),
            "sort": one("sort", "newest"),
        }
        for key in ("query", "album", "date_from", "date_to"):
            value = one(key)
            if value not in (None, ""):
                args[key] = value
        return GALLERY_MODULE.gallery_contact_sheet(args)

    if route == "status":
        return GALLERY_MODULE.gallery_status({})

    if route == "albums":
        args = {"limit": int(one("limit", "100"))}
        value = one("query")
        if value:
            args["query"] = value
        return GALLERY_MODULE.gallery_albums(args)

    if route == "list":
        args = {
            "limit": int(one("limit", "50")),
            "offset": int(one("offset", "0")),
            "sort": one("sort", "newest"),
        }
        for key in ("query", "album", "date_from", "date_to"):
            value = one(key)
            if value not in (None, ""):
                args[key] = value
        return GALLERY_MODULE.gallery_list(args)

    raise ValueError(
        "Unknown gallery virtual path; use gallery://image/ID, "
        "gallery://thumbnail/ID, gallery://contact-sheet, gallery://status, "
        "gallery://albums, or gallery://list"
    )


def checked_argv(argv):
    """Open command mode: validate transport shape only, not executable policy.

    The subprocess still runs as the ordinary Termux app UID, so Android's
    sandbox/permissions remain the hard security boundary.  Shell syntax is
    intentionally available through e.g. ["bash", "-lc", "..."] .
    """
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or "\x00" in x for x in argv):
        raise ValueError("argv must be a non-empty string array without NUL bytes")
    if len(argv) > 200:
        raise ValueError("argv may contain at most 200 items")
    return argv


def execution_cwd(cwd):
    p = Path(cwd).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"cwd is not a directory: {p}")
    return p

def run(argv, cwd, timeout=30):
    proc = subprocess.run(checked_argv(argv), cwd=execution_cwd(cwd), text=True, capture_output=True, timeout=timeout, env=os.environ.copy())
    out = (proc.stdout + proc.stderr)[-MAX_OUTPUT:]
    return {"exit_code": proc.returncode, "output": out, "truncated": len(proc.stdout) + len(proc.stderr) > MAX_OUTPUT}


def start(argv, cwd):
    JOBS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log = JOBS / f"pending-{stamp}.log"
    fh = log.open("ab", buffering=0)
    proc = subprocess.Popen(checked_argv(argv), cwd=execution_cwd(cwd), stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
    job_id = f"{stamp}-{proc.pid}"
    final = JOBS / f"{job_id}.log"
    log.rename(final)
    (JOBS / f"{job_id}.json").write_text(json.dumps({"pid": proc.pid, "argv": argv, "cwd": str(execution_cwd(cwd)), "log": str(final)}), encoding="utf-8")
    return {"job_id": job_id, "pid": proc.pid, "log": str(final)}


def call(name, a):
    if name == "termux_status":
        active = 0
        JOBS.mkdir(parents=True, exist_ok=True)
        for f in JOBS.glob("*.json"):
            try:
                pid = json.loads(f.read_text())["pid"]
                os.kill(pid, 0); active += 1
            except Exception: pass
        return {"ok": True, "root": str(ROOT), "python": sys.version.split()[0], "active_jobs": active, "command_mode": COMMAND_MODE, "execution": "unrestricted-as-Termux-user"}
    if name == "list_files":
        p = inside(a.get("path", ".")); limit = a.get("limit", 100)
        return [{"name": x.name, "type": "dir" if x.is_dir() else "file", "size": x.stat().st_size if x.is_file() else None} for x in sorted(p.iterdir())[:limit]]
    if name == "read_text":
        if isinstance(a.get("path"), str) and a["path"].startswith("gallery://"):
            return gallery_virtual_read(a["path"], a.get("max_chars", 100000))
        p = inside(a["path"]); n = a.get("max_chars", 100000)
        data = p.read_bytes()[:n + 1]
        return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "text": data[:n].decode("utf-8", "replace"), "truncated": len(data) > n}
    if name == "search_text":
        base = inside(a.get("path", ".")); query = a["query"]; limit = a.get("limit", 100); hits = []
        files = [base] if base.is_file() else base.rglob("*")
        for p in files:
            if len(hits) >= limit: break
            if not p.is_file() or p.stat().st_size > MAX_READ: continue
            try:
                for number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if query in line:
                        hits.append({"path": str(p.relative_to(ROOT)), "line": number, "text": line[:500]})
                        if len(hits) >= limit: break
            except (UnicodeDecodeError, OSError): pass
        return {"query": query, "hits": hits, "limited": len(hits) >= limit}
    if name == "write_text":
        p = inside(a["path"]); p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            current = hashlib.sha256(p.read_bytes()).hexdigest()
            if a.get("expected_sha256") and a["expected_sha256"] != current: raise ValueError("File changed; SHA-256 mismatch")
            backup = p.with_suffix(p.suffix + ".bridge-backup"); backup.write_bytes(p.read_bytes())
        tmp = p.with_suffix(p.suffix + ".bridge-tmp"); tmp.write_text(a["content"], encoding="utf-8"); tmp.replace(p)
        return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "backup_created": p.with_suffix(p.suffix + ".bridge-backup").exists()}
    if name == "run_command": return run(a["argv"], a.get("cwd", "."), a.get("timeout_seconds", 30))
    if name == "start_job": return start(a["argv"], a.get("cwd", "."))
    if name in {"job_status", "read_job_log"}:
        job_id = a["job_id"]
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in job_id): raise ValueError("Invalid job ID")
        meta = json.loads((JOBS / f"{job_id}.json").read_text())
        if name == "job_status":
            try: os.kill(meta["pid"], 0); running = True
            except OSError: running = False
            return {"job_id": job_id, "running": running, "pid": meta["pid"]}
        n = a.get("max_chars", 20000); data = Path(meta["log"]).read_bytes(); return {"job_id": job_id, "text": data[-n:].decode("utf-8", "replace"), "truncated": len(data) > n}
    if name == "spotify_migrator":
        d = inside(a.get("project_dir", "storage/downloads/Spotify_V3.1/work_v31/package")); script = d / "spotify_v31_safe.py"
        if not script.is_file(): raise ValueError(f"Migrator not found: {script}")
        argv = ["python", str(script), a["action"]]
        return start(argv, str(d)) if a.get("background", True) else run(argv, str(d), 120)
    if name.startswith("phone_"):
        phone_name = name.removeprefix("phone_")
        if PHONE_MODULE is None or phone_name not in PHONE_TOOLS:
            raise ValueError(f"Unknown phone tool: {phone_name}")
        try:
            value = PHONE_TOOLS[phone_name][1](a)
            ok = not (isinstance(value, dict) and value.get("ok") is False)
            PHONE_MODULE._audit(phone_name, ok, "completed" if ok else "capability unavailable or command failed")
            if not ok:
                raise RuntimeError(json.dumps(value, ensure_ascii=False))
            return value
        except (ValueError, PermissionError, TypeError):
            PHONE_MODULE._audit(phone_name, False, "validation or approval failed")
            raise
    if name.startswith("gallery_"):
        if GALLERY_MODULE is None or name not in GALLERY_TOOLS:
            raise ValueError(f"Unknown gallery tool: {name}")
        return GALLERY_TOOLS[name][1](a)
    if name.startswith("google_"):
        if GOOGLE_MODULE is None or name not in GOOGLE_TOOLS:
            raise ValueError(f"Unknown Google tool: {name}")
        return GOOGLE_TOOLS[name][1](a)
    raise ValueError(f"Unknown tool: {name}")


def respond(msg):
    mid = msg.get("id")
    if mid is None: return None
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "TermuxBridge", "version": BRIDGE_VERSION}, "instructions": "Command execution is open and runs with the ordinary Termux app UID (no root assumed). File helper tools remain scoped to Termux/shared storage. Never request or expose secrets. Confirm consequential writes or destructive actions."}
    elif method == "ping": result = {}
    elif method == "tools/list": result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            value = call(msg["params"]["name"], msg["params"].get("arguments", {}))
            if isinstance(value, dict) and "__mcp_content__" in value:
                result = {"content": value["__mcp_content__"], "isError": False}
            else:
                result = text_result(value)
        except Exception as e: result = text_result({"error": type(e).__name__, "message": str(e)}, error=True)
    else: return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


class Handler(BaseHTTPRequestHandler):
    server_version = f"TermuxSafeBridge/{BRIDGE_VERSION}"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(data)

    def _auth_or_reject(self):
        if _backend_authorized(self.headers):
            return True
        self.send_json(403, {"error": "forbidden"})
        return False

    def do_GET(self):
        if not self._auth_or_reject(): return
        if self.path == "/healthz": self.send_json(200, {"ok": True, "pid": os.getpid()})
        else: self.send_json(405, {"error": "POST JSON-RPC to /mcp"})

    def do_POST(self):
        if not self._auth_or_reject(): return
        if self.path != "/mcp": self.send_json(404, {"error": "not found"}); return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 2_000_000: raise ValueError("invalid request size")
            msg = json.loads(self.rfile.read(size))
            ans = respond(msg)
            if ans is None: self.send_response(202); self.end_headers()
            else: self.send_json(200, ans)
        except Exception as e:
            self.send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})


def main():
    if "--http" in sys.argv:
        index = sys.argv.index("--http")
        port = int(sys.argv[index + 1]) if len(sys.argv) > index + 1 else 8765
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    else:
        for line in sys.stdin:
            try:
                msg = json.loads(line); ans = respond(msg)
                if ans is not None: print(json.dumps(ans, ensure_ascii=False), flush=True)
            except Exception as e:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}), flush=True)


if __name__ == "__main__": main()
