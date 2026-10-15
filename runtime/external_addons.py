"""Private local addon loader for TermuxBridge.

Loads versioned addons from ~/.termux-mcp-bridge/addons/<name>/current.
Addons are isolated by namespace and cannot replace core tools.
"""
from __future__ import annotations
import importlib.util,json,os,re,threading,time
from pathlib import Path
SEMVER=re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
NAME=re.compile(r"^[a-z][a-z0-9-]{1,63}$")
NAMESPACE=re.compile(r"^[a-z][a-z0-9_]*_$")
SCHEME=re.compile(r"^[a-z][a-z0-9+.-]{1,31}$")
ABI=1; BRIDGE_MAJOR=1; MAX_TOOLS=250; FAILURE_THRESHOLD=3; RETRY_SECONDS=60

class ExternalAddonRegistry:
    def __init__(self,root=None):
        self.root=Path(root or os.environ.get("TERMUX_BRIDGE_ADDONS_ROOT", str(Path.home()/".termux-mcp-bridge/addons"))).expanduser()
        self.lock=threading.RLock();self.modules={};self.handlers={};self.tool_to_addon={};self.manifests={};self.info={};self.virtual={};self.blocked_until={}
    def _private(self,p:Path):
        st=p.stat()
        if st.st_uid!=os.getuid():raise ValueError("addon path owner mismatch")
        if st.st_mode & 0o022:raise ValueError("addon release is group/world writable")
    def _read_manifest(self,addon_dir:Path,current:Path):
        manifest=current/"addon.json"
        if not manifest.is_file() or manifest.is_symlink():raise ValueError("addon manifest missing")
        self._private(manifest)
        data=json.loads(manifest.read_text(encoding="utf-8"))
        name=data.get("name");version=data.get("version");namespace=data.get("namespace")
        if not isinstance(name,str) or not NAME.fullmatch(name) or name!=addon_dir.name:raise ValueError("invalid addon name")
        if not isinstance(version,str) or not SEMVER.fullmatch(version):raise ValueError("invalid addon version")
        if data.get("abi")!=ABI or data.get("bridge_major")!=BRIDGE_MAJOR:raise ValueError("incompatible addon ABI")
        if not isinstance(namespace,str) or not NAMESPACE.fullmatch(namespace):raise ValueError("invalid addon namespace")
        entry=data.get("entrypoint","adapter.py")
        if not isinstance(entry,str) or Path(entry).name!=entry:raise ValueError("invalid addon entrypoint")
        return data,current/entry
    def _load_one(self,addon_dir:Path):
        current_link=addon_dir/"current"
        if not current_link.exists():return None
        current=current_link.resolve()
        if not current.is_dir() or not current.is_relative_to(addon_dir.resolve()):raise ValueError("addon current escapes addon root")
        self._private(current)
        manifest,entry=self._read_manifest(addon_dir,current)
        if not entry.is_file() or entry.is_symlink():raise ValueError("addon entrypoint missing")
        self._private(entry)
        spec=importlib.util.spec_from_file_location("termuxbridge_addon_"+manifest["name"].replace("-","_"),entry)
        if not spec or not spec.loader:raise ImportError("addon loader unavailable")
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        tools=getattr(mod,"TOOLS",None);descriptions=getattr(mod,"DESCRIPTIONS",{});read_only=set(getattr(mod,"READ_ONLY",set()));destructive=set(getattr(mod,"DESTRUCTIVE",set()));open_world=set(getattr(mod,"OPEN_WORLD",set()))
        if not isinstance(tools,dict) or len(tools)>MAX_TOOLS:raise ValueError("invalid addon tools")
        namespace=manifest["namespace"]
        records=[];handlers={}
        for name,value in tools.items():
            if not isinstance(name,str) or not name.startswith(namespace):raise ValueError("addon tool outside namespace")
            if name in self.tool_to_addon:raise ValueError("duplicate addon tool")
            if not isinstance(value,(tuple,list)) or len(value)!=2 or not isinstance(value[0],dict) or not callable(value[1]):raise ValueError("invalid addon tool contract")
            handlers[name]=value[1]
            records.append({"name":name,"title":name.replace("_"," ").title(),"description":str(descriptions.get(name,name))[:2000],"inputSchema":value[0],"annotations":{"readOnlyHint":name in read_only,"destructiveHint":name in destructive,"openWorldHint":name in open_world}})
        virtual=getattr(mod,"VIRTUAL_SCHEMES",{})
        if not isinstance(virtual,dict):raise ValueError("invalid virtual schemes")
        for scheme,method_name in virtual.items():
            if not isinstance(scheme,str) or not SCHEME.fullmatch(scheme) or not isinstance(method_name,str):raise ValueError("invalid virtual scheme")
            if scheme in self.virtual:raise ValueError("duplicate virtual scheme")
            method=getattr(mod,method_name,None)
            if not callable(method):raise ValueError("virtual handler missing")
            self.virtual[scheme]=(manifest["name"],method)
        name=manifest["name"]
        self.modules[name]=mod;self.handlers[name]=handlers;self.manifests[name]=manifest
        self.info[name]={"status":"READY","version":manifest["version"],"namespace":namespace,"tools":len(records),"failures":0,"path":str(current)}
        for tool in handlers:self.tool_to_addon[tool]=name
        return records
    def discover(self):
        records=[]
        if not self.root.is_dir():return records
        for addon_dir in sorted(self.root.iterdir()):
            if not addon_dir.is_dir() or not NAME.fullmatch(addon_dir.name):continue
            try:
                rec=self._load_one(addon_dir)
                if rec:records.extend(rec)
            except Exception:
                self.info[addon_dir.name]={"status":"FAILED_LOAD","version":None,"namespace":None,"tools":0,"failures":1}
        return records
    def namespaces(self):return {m["namespace"] for m in self.manifests.values()}
    def invoke(self,tool,args):
        addon=self.tool_to_addon.get(tool)
        if addon is None:raise ValueError("unknown addon tool")
        now=time.monotonic()
        if self.blocked_until.get(addon,0)>now:raise RuntimeError("addon temporarily isolated: "+addon)
        handler=self.handlers[addon][tool]
        try:value=handler(args)
        except (ValueError,TypeError,PermissionError):raise
        except Exception:
            info=self.info[addon];info["failures"]+=1;info["status"]="DEGRADED"
            if info["failures"]>=FAILURE_THRESHOLD:self.blocked_until[addon]=time.monotonic()+RETRY_SECONDS
            raise RuntimeError("addon operation failed: "+addon) from None
        self.info[addon]["failures"]=0;self.info[addon]["status"]="READY";self.blocked_until.pop(addon,None);return value
    def can_virtual(self,path):
        try:scheme=str(path).split(":",1)[0].lower();return scheme in self.virtual
        except Exception:return False
    def virtual_read(self,path,max_chars=100000):
        scheme=str(path).split(":",1)[0].lower();entry=self.virtual.get(scheme)
        if not entry:raise ValueError("unknown addon virtual scheme")
        addon,handler=entry
        try:return handler(path,max_chars)
        except Exception as exc:
            if isinstance(exc,(ValueError,TypeError,PermissionError)):raise
            raise RuntimeError("addon virtual read failed: "+addon) from None
    def status(self):
        return {name:{**info,"isolated":self.blocked_until.get(name,0)>time.monotonic()} for name,info in self.info.items()}
