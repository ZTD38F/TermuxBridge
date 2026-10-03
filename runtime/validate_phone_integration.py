from __future__ import annotations
import importlib.util
from pathlib import Path

path = Path.home() / "termux-mcp-bridge" / "bridge_server.py"
spec = importlib.util.spec_from_file_location("bridge_validation", path)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load bridge")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
names = [tool["name"] for tool in module.TOOLS]
phone = [name for name in names if name.startswith("phone_")]
print({"total_tools": len(names), "phone_tools": len(phone), "names": phone})
