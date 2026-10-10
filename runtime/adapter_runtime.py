"""Versioned, fault-isolated built-in adapter registry for TermuxBridge.

Allowlisted modules only: never import adapter paths from network requests or
writable manifests. Failures in one optional adapter must not block the core.
"""
from __future__ import annotations
import importlib.util
from pathlib import Path
import re
import threading
import time

ADAPTER_ABI = 1
BRIDGE_MAJOR = 1
RETRY_AFTER_SECONDS = 60
FAILURE_THRESHOLD = 3
# Immutable implementation contracts. Versions describe bundled adapter APIs,
# not the versions of third-party Android apps.
BUILTINS = {
    "phone": {"version":"1.0.0","abi":1,"bridge_major":1,"namespace":"phone_"},
    "gallery":{"version":"1.0.0","abi":1,"bridge_major":1,"namespace":"gallery_"},
    "google":{"version":"1.0.0","abi":1,"bridge_major":1,"namespace":"google_"},
}
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class AdapterRegistry:
    def __init__(self):
        self.lock=threading.RLock()
        self.modules={}
        self.handlers={}
        self.info={name:{"status":"NOT_INSTALLED","version":spec["version"],
                         "abi":spec["abi"],"tools":0,"failures":0}
                   for name,spec in BUILTINS.items()}
        self.blocked_until={}

    @staticmethod
    def validate(name,version,abi,bridge_major):
        if name not in BUILTINS:
            raise ValueError("adapter is not on the built-in allowlist")
        expected=BUILTINS[name]
        if not VERSION_RE.fullmatch(str(version)) or str(version).split(".")[0]!="1":
            raise ValueError("incompatible adapter version")
        if not isinstance(abi,int) or abi!=ADAPTER_ABI or abi!=expected["abi"]:
            raise ValueError("unsupported adapter ABI")
        if bridge_major!=BRIDGE_MAJOR or bridge_major!=expected["bridge_major"]:
            raise ValueError("incompatible bridge major version")
        return True

    def register(self,name,module,tools,*,version=None,abi=None,bridge_major=1):
        cfg=BUILTINS[name]
        self.validate(name,version or cfg["version"],cfg["abi"] if abi is None else abi,bridge_major)
        if not isinstance(tools,dict):
            raise ValueError("adapter tools must be a mapping")
        if len(tools)>200:
            raise ValueError("adapter exceeds tool count")
        reserved=cfg["namespace"]
        handlers={}
        for key,value in tools.items():
            if not isinstance(key,str) or not key.startswith(reserved):
                # Phone source exports short names; prefix during discovery.
                if not (name=="phone" and isinstance(key,str) and re.fullmatch("[a-z][a-z0-9_]*",key)):
                    raise ValueError("adapter tool outside namespace")
            if not isinstance(value,(tuple,list)) or len(value)!=2 or not isinstance(value[0],dict) or not callable(value[1]):
                raise ValueError("adapter tool contract invalid")
            public=key if key.startswith(reserved) else reserved+key
            if public in handlers:
                raise ValueError("duplicate adapter tool")
            handlers[public]=value[1]
        with self.lock:
            self.modules[name]=module
            self.handlers[name]=handlers
            self.info[name]={"status":"READY","version":version or cfg["version"],
                             "abi":cfg["abi"],"tools":len(handlers),"failures":0}
        return tools

    def load(self,name,path,*,version=None,abi=None):
        if name not in BUILTINS:
            raise ValueError("not allowlisted")
        path=Path(path)
        if not path.is_file() or path.is_symlink():
            return None
        try:
            spec=importlib.util.spec_from_file_location("termuxbridge_"+name+"_adapter",path)
            if not spec or not spec.loader:
                raise ImportError("adapter loader absent")
            mod=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.register(name,mod,getattr(mod,"TOOLS"),
                          version=version,abi=abi)
            return mod
        except Exception:
            # No exception text: third-party import exceptions may contain secrets.
            with self.lock:
                self.modules.pop(name,None)
                self.handlers.pop(name,None)
                self.info[name]={**self.info[name],"status":"FAILED_LOAD","tools":0}
            return None

    def invoke(self,name,public_tool,arguments):
        with self.lock:
            adapter=self.info.get(name)
            if not adapter or adapter["status"] not in {"READY","DEGRADED"}:
                raise RuntimeError("adapter unavailable: "+name)
            now=time.monotonic()
            if self.blocked_until.get(name,0)>now:
                raise RuntimeError("adapter temporarily isolated: "+name)
            handler=self.handlers.get(name,{}).get(public_tool)
        if handler is None:
            raise ValueError("unknown adapter tool")
        try:
            result=handler(arguments)
        except (ValueError,TypeError,PermissionError):
            # Ordinary argument validation and Android approvals are not crashes.
            raise
        except Exception as exc:
            with self.lock:
                info=self.info[name]
                info["failures"]+=1
                info["status"]="DEGRADED"
                if info["failures"]>=FAILURE_THRESHOLD:
                    self.blocked_until[name]=time.monotonic()+RETRY_AFTER_SECONDS
            raise RuntimeError("adapter operation failed: "+name) from None
        with self.lock:
            self.info[name]["failures"]=0
            self.info[name]["status"]="READY"
            self.blocked_until.pop(name,None)
        return result

    def status(self):
        with self.lock:
            now=time.monotonic()
            return {name:{**self.info[name],
                          "isolated":self.blocked_until.get(name,0)>now}
                    for name in BUILTINS}
