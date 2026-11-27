"""MCP Apps addon registration tests; entirely synthetic fixtures."""
import json,tempfile,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"runtime"))
from external_addons import ExternalAddonRegistry
URI="ui://test-app/gallery.html"
class UIResources(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def fixture(self,name="test-app",uri=URI,mime="text/html;profile=mcp-app",declared=URI,legacy=False):
  d=self.root/name;d.mkdir();p=d/"current";p.mkdir()
  ns=name.replace("-","_")+"_";tool=ns+"render"
  (p/"addon.json").write_text(json.dumps({"name":name,"version":"1.0.0","namespace":ns,"abi":1,"bridge_major":1,"entrypoint":"adapter.py"}))
  src="TOOLS={%r:({'type':'object'},lambda a:{'content':[{'type':'text','text':'safe'}],'structuredContent':{'ok':True},'_meta':{'fixture':'private'}})}\n"%tool
  if not legacy:
   src+="UI_RESOURCES="+repr({uri:{"uri":uri,"mimeType":mime,"text":"<p>fixture</p>"}})+"\n"
   src+="TOOL_META="+repr({tool:{"ui":{"resourceUri":declared,"visibility":["model","app"]}}})+"\n"
  (p/"adapter.py").write_text(src)
  return tool
 def test_registration(self):
  n=self.fixture();r=ExternalAddonRegistry(self.root);ts=r.discover();self.assertEqual(ts[0]["_meta"]["ui"]["resourceUri"],URI)
  self.assertEqual(r.list_resources()[0]["uri"],URI)
  self.assertEqual(r.read_resource(URI)["contents"][0]["text"],"<p>fixture</p>")
  x=r.app_result(n,r.invoke(n,{}));self.assertEqual(x["_meta"],{"fixture":"private"});self.assertNotIn("private",x["content"][0]["text"])
 def test_legacy(self):
  n=self.fixture(legacy=True);r=ExternalAddonRegistry(self.root);ts=r.discover()
  self.assertNotIn("_meta",ts[0]);self.assertEqual(r.list_resources(),[]);self.assertIsNone(r.app_result(n,{}))
 def test_invalid_uri(self):
  self.fixture(uri="file:///tmp/private");r=ExternalAddonRegistry(self.root);self.assertEqual(r.discover(),[]);self.assertEqual(r.list_resources(),[])
 def test_invalid_mime(self):
  self.fixture(mime="text/plain");r=ExternalAddonRegistry(self.root);self.assertEqual(r.discover(),[])
 def test_undeclared(self):
  self.fixture(declared="ui://test-app/other.html");r=ExternalAddonRegistry(self.root);self.assertEqual(r.discover(),[]);self.assertEqual(r.list_resources(),[])
 def test_duplicate(self):
  self.fixture();self.fixture(name="z-test");r=ExternalAddonRegistry(self.root);self.assertEqual(len(r.discover()),1);self.assertEqual(len(r.list_resources()),1)
 def test_result_validation(self):
  n=self.fixture();r=ExternalAddonRegistry(self.root);r.discover()
  for x in (None,{},{"content":"not a list"},{"content":[],"_meta":"bad"},{"content":[],"structuredContent":"bad"},{"content":[],"unexpected":1}):
   with self.assertRaises(ValueError):r.app_result(n,x)
  with self.assertRaises(ValueError):r.read_resource("ui://test-app/missing.html")
if __name__=="__main__":unittest.main()
