import py_compile
from pathlib import Path
p=Path.home()/"termux-mcp-bridge"/"bridge_server.py"
py_compile.compile(str(p), doraise=True)
print("syntax ok")
