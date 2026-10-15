from __future__ import annotations
import json,os,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from external_addons import ExternalAddonRegistry

class ExternalAddonTests(unittest.TestCase):
    def make_addon(self,root,name='demo-addon',namespace='demo_'):
        addon=Path(root)/name; release=addon/'releases'/'0.1.0';release.mkdir(parents=True)
        manifest={'name':name,'version':'0.1.0','abi':1,'bridge_major':1,'namespace':namespace,'entrypoint':'adapter.py'}
        (release/'addon.json').write_text(json.dumps(manifest));(release/'adapter.py').write_text("""TOOLS={'demo_echo':({'type':'object','properties':{},'required':[],'additionalProperties':False},lambda a:{'ok':True,'value':a.get('value')})}\nDESCRIPTIONS={'demo_echo':'Echo'}\nREAD_ONLY={'demo_echo'}\nVIRTUAL_SCHEMES={'demo':'virtual_read'}\ndef virtual_read(path,max_chars=100000): return {'path':path,'max_chars':max_chars}\n""")
        for p in (addon,addon/'releases',release,release/'addon.json',release/'adapter.py'):
            os.chmod(p,0o700 if p.is_dir() else 0o600)
        (addon/'current').symlink_to(release)
        return addon
    def test_discover_invoke_and_virtual(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_addon(tmp);r=ExternalAddonRegistry(tmp);records=r.discover()
            self.assertEqual([x['name'] for x in records],['demo_echo']);self.assertEqual(r.namespaces(),{'demo_'})
            self.assertEqual(r.invoke('demo_echo',{'value':7})['value'],7)
            self.assertTrue(r.can_virtual('demo://x'));self.assertEqual(r.virtual_read('demo://x',42)['max_chars'],42)
    def test_rejects_world_writable_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon=self.make_addon(tmp);entry=(addon/'current').resolve()/'adapter.py';os.chmod(entry,0o666)
            r=ExternalAddonRegistry(tmp);self.assertEqual(r.discover(),[]);self.assertEqual(r.status()['demo-addon']['status'],'FAILED_LOAD')
    def test_namespace_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon=self.make_addon(tmp,namespace='other_');r=ExternalAddonRegistry(tmp);self.assertEqual(r.discover(),[])
