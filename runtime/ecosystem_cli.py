#!/usr/bin/env python3
"""Local command-line entry for Ecosystem; never exposes an HTTP listener."""
from __future__ import annotations
import json
import sys
from pathlib import Path
try:
    from . import ecosystem_core as core
except ImportError:
    import ecosystem_core as core


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if len(argv)<2:
        raise SystemExit("Usage: ecosystem_cli.py OP ROOT [BACKUP_NAME] [--yes]")
    cmd,root=argv[:2]
    root=Path(root).expanduser().resolve()
    if cmd=="dashboard":
        info=core.dashboard(root)
        print("TERMUXBRIDGE ECOSYSTEM — LOCAL")
        for key in ("bridge_version","health","fault","severity","tunnel_state",
                    "last_checked","update_phase","battery_mode","battery_percent",
                    "storage_free_mb","backups","remote_connectivity_verified"):
            print(f"{key.upper():32} {info.get(key)}")
        print("QUEUE",json.dumps(info["queue"],sort_keys=True))
        print("ADAPTERS: use MCP ecosystem_adapter_status for live state")
    elif cmd=="html":
        print(core.dashboard(root,html_file=True)["path"])
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
