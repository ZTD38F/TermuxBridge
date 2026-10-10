#!/usr/bin/env python3
"""Local command-line entry for Ecosystem; never exposes an HTTP listener."""
from __future__ import annotations
import json
import sys
import urllib.request
from pathlib import Path
try:
    from . import ecosystem_core as core
except ImportError:
    import ecosystem_core as core


def live_adapters(root):
    """Fetch adapter status through the protected localhost MCP router only."""
    try:
        token=(root/"secrets/router_token").read_text().strip()
        if len(token)<32:
            raise ValueError("router token unavailable")
        request=urllib.request.Request(
            "http://127.0.0.1:8765/mcp",
            data=json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call",
                             "params":{"name":"ecosystem_adapter_status","arguments":{}}}).encode(),
            headers={"Content-Type":"application/json","X-Bridge-Token":token},
        )
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=4) as rsp:
            response=json.load(rsp)
        result=response["result"]
        if result.get("isError"):
            return {}
        return json.loads(result["content"][0]["text"])
    except (OSError,ValueError,KeyError,TypeError,TimeoutError):
        return {}


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if len(argv)<2:
        raise SystemExit("Usage: ecosystem_cli.py OP ROOT [BACKUP_NAME] [--yes]")
    cmd,root=argv[:2]
    root=Path(root).expanduser().resolve()
    if cmd=="dashboard":
        info=core.dashboard(root,adapters=live_adapters(root))
        print("TERMUXBRIDGE ECOSYSTEM — LOCAL")
        for key in ("bridge_version","health","fault","severity","tunnel_state",
                    "last_checked","update_phase","battery_mode","battery_percent",
                    "storage_free_mb","backups","remote_connectivity_verified"):
            print(f"{key.upper():32} {info.get(key)}")
        print("QUEUE",json.dumps(info["queue"],sort_keys=True))
        print("ADAPTERS",json.dumps(info["adapters"],sort_keys=True))
    elif cmd=="html":
        print(core.dashboard(root,adapters=live_adapters(root),html_file=True)["path"])
    elif cmd=="backup-create":
        print(json.dumps(core.backup_create(root),sort_keys=True))
    elif cmd=="backup-list":
        print(json.dumps(core.backup_list(root),sort_keys=True))
    elif cmd in {"backup-verify","backup-restore"}:
        if len(argv)<3:
            raise SystemExit("Backup name required")
        if cmd=="backup-verify":
            result=core.backup_verify(root,argv[2])
        else:
            if len(argv)>3 and argv[3]!="--yes":
                raise SystemExit("Unexpected restore argument; use --yes")
            result=core.backup_restore(root,argv[2],confirm=len(argv)>3)
        print(json.dumps(result,sort_keys=True))
    elif cmd=="auto-backup":
        print(json.dumps(core.auto_backup(root),sort_keys=True))
    elif cmd=="chaos-test":
        checks=core.chaos_selftest()
        print(json.dumps(checks,sort_keys=True))
        if not all([checks["adapter_isolated"],checks["healthy_adapter_available"],
                    checks["backup_verified"],checks["corrupted_archive_rejected"],
                    not checks["real_services_touched"]]):
            return 1
    else:
        raise SystemExit("Unknown ecosystem command")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
