import py_compile
from pathlib import Path

root = Path.home() / "termux-mcp-bridge"
for name in ("bridge_server.py", "external_addons.py", "addon_services.py"):
    p = root / name
    if p.exists():
        py_compile.compile(str(p), doraise=True)
print("syntax ok")
