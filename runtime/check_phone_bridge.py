from __future__ import annotations
import json
import urllib.request

def rpc(payload):
    req=urllib.request.Request(
        "http://127.0.0.1:8765/mcp",
        data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=10))

listing=rpc({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}})
names=[x["name"] for x in listing["result"]["tools"]]
health=rpc({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"phone_phone_health","arguments":{}}})
print(json.dumps({"tool_count":len(names),"phone_tools":[x for x in names if x.startswith("phone_")],"health":health["result"]},ensure_ascii=False,indent=2))
