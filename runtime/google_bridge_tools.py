from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path.home()
HTTP_FILE = ROOT / ".config" / "google-bridge" / "google_http.py"
KEEP_SCRIPT = ROOT / "chatgpt-browser-control" / "keep_automation.mjs"
MAPS_SCRIPT = ROOT / "chatgpt-browser-control" / "maps_automation.mjs"
MAPS_SAVE_SCRIPT = ROOT / "chatgpt-browser-control" / "maps_save_to_list.mjs"
BROWSER_HEADLESS = ROOT / "chatgpt-browser-headless"

_spec = importlib.util.spec_from_file_location("google_http_client", HTTP_FILE)
if not _spec or not _spec.loader:
    raise RuntimeError("Cannot load Google HTTP client")
_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_client)

def schema(props=None, required=None):
    return {"type":"object","properties":props or {},"required":required or [],"additionalProperties":False}

def _cdp_ready():
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False

def _ensure_browser():
    if _cdp_ready():
        return
    p = subprocess.run([str(BROWSER_HEADLESS)], cwd=ROOT, text=True, capture_output=True, timeout=45)
    if p.returncode != 0:
        raise RuntimeError("Could not start background Chromium: " + (p.stdout+p.stderr)[-1000:])

def _browser(script: Path, op: str, a: dict, timeout=180):
    if not script.is_file():
        raise RuntimeError(f"Browser automation is not installed: {script}")
    _ensure_browser()
    p = subprocess.run(
        ["node", str(script), op, json.dumps(a, ensure_ascii=False)],
        cwd=script.parent, text=True, capture_output=True, timeout=timeout
    )
    raw = (p.stdout or p.stderr).strip()
    if not raw:
        raise RuntimeError(f"{script.name} returned no result")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(raw[-4000:])
    if p.returncode != 0 or not result.get("ok"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result

def _keep(op, a):
    return _browser(KEEP_SCRIPT, op, a, 180)

def _maps(op, a):
    return _browser(MAPS_SCRIPT, op, a, 240)

def _maps_save_legacy(a):
    list_name = a["list_name"].strip()
    places = [x.strip() for x in a["places"] if isinstance(x, str) and x.strip()]
    if not list_name:
        raise ValueError("list_name must not be empty")
    if not places or len(places) > 100:
        raise ValueError("places must contain 1..100 non-empty strings")
    if a.get("dry_run", False):
        return {
            "ok": True,
            "operation": "dry_run",
            "result": [
                _client.dispatch({"operation":"maps_search","query":q,"language_code":"ru","region_code":"LV","max_results":5})
                for q in places
            ],
        }
    results = []
    for q in places:
        results.append(_maps("add_place", {"name":list_name, "query":q})["result"])
    return {"ok":True,"operation":"save_to_list","result":results}

TOOLS = {
    # Google Tasks: task-list CRUD
    "google_tasks_list_tasklists": (
        schema({"max_results":{"type":"integer","minimum":1,"maximum":100,"default":100}}),
        lambda a:_client.dispatch({"operation":"list_tasklists",**a})
    ),
    "google_tasks_get_tasklist": (
        schema({"tasklist_id":{"type":"string","minLength":1}},["tasklist_id"]),
        lambda a:_client.dispatch({"operation":"get_tasklist",**a})
    ),
    "google_tasks_create_tasklist": (
        schema({"title":{"type":"string","minLength":1,"maxLength":1024}},["title"]),
        lambda a:_client.dispatch({"operation":"create_tasklist",**a})
    ),
    "google_tasks_update_tasklist": (
        schema({"tasklist_id":{"type":"string","minLength":1},"title":{"type":"string","minLength":1,"maxLength":1024}},["tasklist_id","title"]),
        lambda a:_client.dispatch({"operation":"update_tasklist",**a})
    ),
    "google_tasks_delete_tasklist": (
        schema({"tasklist_id":{"type":"string","minLength":1}},["tasklist_id"]),
        lambda a:_client.dispatch({"operation":"delete_tasklist",**a})
    ),

    # Google Tasks: task CRUD
    "google_tasks_list_tasks": (
        schema({
            "tasklist_id":{"type":"string","minLength":1},
            "max_results":{"type":"integer","minimum":1,"maximum":100,"default":100},
            "show_completed":{"type":"boolean","default":True},
            "show_hidden":{"type":"boolean","default":False}
        },["tasklist_id"]),
        lambda a:_client.dispatch({"operation":"list_tasks",**a})
    ),
    "google_tasks_get_task": (
        schema({"tasklist_id":{"type":"string","minLength":1},"task_id":{"type":"string","minLength":1}},["tasklist_id","task_id"]),
        lambda a:_client.dispatch({"operation":"get_task",**a})
    ),
    "google_tasks_create_task": (
        schema({"tasklist_id":{"type":"string","minLength":1},"title":{"type":"string","minLength":1},"notes":{"type":"string"},"due":{"type":"string"}},["tasklist_id","title"]),
        lambda a:_client.dispatch({"operation":"create_task",**a})
    ),
    "google_tasks_update_task": (
        schema({
            "tasklist_id":{"type":"string","minLength":1},
            "task_id":{"type":"string","minLength":1},
            "title":{"type":"string"},
            "notes":{"type":"string"},
            "due":{"type":"string"},
            "status":{"type":"string","enum":["needsAction","completed"]}
        },["tasklist_id","task_id"]),
        lambda a:_client.dispatch({"operation":"update_task",**a})
    ),
    "google_tasks_delete_task": (
        schema({"tasklist_id":{"type":"string","minLength":1},"task_id":{"type":"string","minLength":1}},["tasklist_id","task_id"]),
        lambda a:_client.dispatch({"operation":"delete_task",**a})
    ),

    # Google Keep CRUD
    "google_keep_list_notes": (
        schema({"limit":{"type":"integer","minimum":1,"maximum":200,"default":100}}),
        lambda a:_keep("list",a)
    ),
    "google_keep_get_note": (
        schema({"title":{"type":"string","minLength":1}},["title"]),
        lambda a:_keep("get",a)
    ),
    "google_keep_search_notes": (
        schema({"query":{"type":"string","minLength":1}},["query"]),
        lambda a:_keep("search",a)
    ),
    "google_keep_create_note": (
        schema({"title":{"type":"string","minLength":1,"maxLength":1000},"body":{"type":"string","maxLength":20000}},["title"]),
        lambda a:_keep("create",a)
    ),
    "google_keep_update_note": (
        schema({"title":{"type":"string","minLength":1},"new_title":{"type":"string","minLength":1},"body":{"type":"string","maxLength":20000}},["title"]),
        lambda a:_keep("update",a)
    ),
    "google_keep_trash_note": (
        schema({"title":{"type":"string","minLength":1}},["title"]),
        lambda a:_keep("trash",a)
    ),
    "google_keep_restore_note": (
        schema({"title":{"type":"string","minLength":1}},["title"]),
        lambda a:_keep("restore",a)
    ),
    "google_keep_delete_note_forever": (
        schema({"title":{"type":"string","minLength":1}},["title"]),
        lambda a:_keep("delete_forever",a)
    ),
    "google_keep_list_trash": (
        schema({"limit":{"type":"integer","minimum":1,"maximum":200,"default":100}}),
        lambda a:_keep("list_trash",a)
    ),
    "google_keep_list_archive": (
        schema({"limit":{"type":"integer","minimum":1,"maximum":200,"default":100}}),
        lambda a:_keep("list_archive",a)
    ),

    # Google Maps/Places
    "google_maps_search": (
        schema({
            "query":{"type":"string","minLength":1},
            "language_code":{"type":"string","default":"ru"},
            "region_code":{"type":"string","default":"LV"},
            "max_results":{"type":"integer","minimum":1,"maximum":20,"default":10}
        },["query"]),
        lambda a:_client.dispatch({"operation":"maps_search",**a})
    ),
    "google_maps_list_lists": (
        schema(),
        lambda a:_maps("list_lists",a)
    ),
    "google_maps_create_list": (
        schema({"name":{"type":"string","minLength":1,"maxLength":200},"description":{"type":"string","maxLength":4000}},["name"]),
        lambda a:_maps("create_list",a)
    ),
    "google_maps_get_list": (
        schema({"name":{"type":"string","minLength":1}},["name"]),
        lambda a:_maps("get_list",a)
    ),
    "google_maps_update_list": (
        schema({"name":{"type":"string","minLength":1},"new_name":{"type":"string","minLength":1,"maxLength":200},"description":{"type":"string","maxLength":4000}},["name"]),
        lambda a:_maps("update_list",a)
    ),
    "google_maps_delete_list": (
        schema({"name":{"type":"string","minLength":1}},["name"]),
        lambda a:_maps("delete_list",a)
    ),
    "google_maps_list_places": (
        schema({"name":{"type":"string","minLength":1}},["name"]),
        lambda a:_maps("list_places",a)
    ),
    "google_maps_add_place": (
        schema({"name":{"type":"string","minLength":1},"query":{"type":"string","minLength":1,"maxLength":500}},["name","query"]),
        lambda a:_maps("add_place",a)
    ),
    "google_maps_remove_place": (
        schema({"name":{"type":"string","minLength":1},"match":{"type":"string","minLength":1}},["name","match"]),
        lambda a:_maps("remove_place",a)
    ),
    "google_maps_update_place_note": (
        schema({"name":{"type":"string","minLength":1},"match":{"type":"string","minLength":1},"note":{"type":"string","maxLength":4000}},["name","match","note"]),
        lambda a:_maps("update_place_note",a)
    ),
    "google_maps_save_to_list": (
        schema({
            "list_name":{"type":"string","minLength":1,"maxLength":200},
            "places":{"type":"array","items":{"type":"string","minLength":1,"maxLength":500},"minItems":1,"maxItems":100},
            "dry_run":{"type":"boolean","default":False}
        },["list_name","places"]),
        _maps_save_legacy
    ),
}

DESCRIPTIONS = {
    "google_tasks_list_tasklists":"List Google Tasks task lists through the user's OAuth grant.",
    "google_tasks_get_tasklist":"Get one Google Tasks task list by ID.",
    "google_tasks_create_tasklist":"Create a Google Tasks task list.",
    "google_tasks_update_tasklist":"Rename a Google Tasks task list.",
    "google_tasks_delete_tasklist":"Delete a Google Tasks task list and its tasks.",
    "google_tasks_list_tasks":"List tasks in a Google Tasks list.",
    "google_tasks_get_task":"Get one Google Task by ID.",
    "google_tasks_create_task":"Create a Google Task.",
    "google_tasks_update_task":"Update or complete a Google Task.",
    "google_tasks_delete_task":"Delete a Google Task.",

    "google_keep_list_notes":"List visible Google Keep notes from the authenticated browser profile.",
    "google_keep_get_note":"Read one Google Keep note selected by an exact unique title.",
    "google_keep_search_notes":"Search visible Google Keep notes by title or body.",
    "google_keep_create_note":"Create a Google Keep note.",
    "google_keep_update_note":"Update title and/or body of one Google Keep note selected by exact unique title.",
    "google_keep_trash_note":"Move one Google Keep note to Trash.",
    "google_keep_restore_note":"Restore one Google Keep note from Trash.",
    "google_keep_delete_note_forever":"Permanently delete one Google Keep note from Trash.",
    "google_keep_list_trash":"List Google Keep notes currently in Trash.",
    "google_keep_list_archive":"List Google Keep notes currently in Archive.",

    "google_maps_search":"Search Google Places by text through the configured Places API key.",
    "google_maps_list_lists":"List personal Google Maps saved lists visible in the authenticated account.",
    "google_maps_create_list":"Create a personal Google Maps saved list.",
    "google_maps_get_list":"Read one Google Maps saved list, including description and current places.",
    "google_maps_update_list":"Rename a Google Maps saved list and/or update its description.",
    "google_maps_delete_list":"Delete one Google Maps saved list.",
    "google_maps_list_places":"List the places currently stored in one Google Maps saved list.",
    "google_maps_add_place":"Search for and add one place to a Google Maps saved list.",
    "google_maps_remove_place":"Remove one uniquely matching place from a Google Maps saved list.",
    "google_maps_update_place_note":"Update the note attached to one uniquely matching place in a Google Maps saved list.",
    "google_maps_save_to_list":"Save multiple place queries into an existing Google Maps list; dry_run only resolves places.",
}
