from __future__ import annotations
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class BridgeExternalAddonIntegration(unittest.TestCase):
    def _addon(self,base,name,namespace,tool):
        addon=Path(base)/name;rel=addon/'releases'/'0.1.0';rel.mkdir(parents=True)
        (rel/'addon.json').write_text(json.dumps({'name':name,'version':'0.1.0','abi':1,'bridge_major':1,'namespace':namespace,'entrypoint':'adapter.py'}))
        (rel/'adapter.py').write_text(f"TOOLS={{'{tool}':({{'type':'object','properties':{{'value':{{'type':'integer'}}}},'required':[],'additionalProperties':False}},lambda a:{{'ok':True,'value':a.get('value')}})}}\nDESCRIPTIONS={{'{tool}':'test'}}\nREAD_ONLY={{'{tool}'}}\n")
        for p in (addon,addon/'releases',rel,rel/'addon.json',rel/'adapter.py'):os.chmod(p,0o700 if p.is_dir() else 0o600)
        (addon/'current').symlink_to(rel)
    def _run(self,addons,code):
        env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'runtime');env['TERMUX_BRIDGE_ADDONS_ROOT']=str(addons)
        with tempfile.TemporaryDirectory() as bridge:
            token=Path(bridge)/'token';token.write_text('x'*64);env['TERMUX_BRIDGE_BACKEND_TOKEN_FILE']=str(token);env['TERMUX_BRIDGE_ROOT']=bridge
            return subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=20,check=True).stdout.strip()
    def test_external_tool_reaches_router(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._addon(tmp,'demo-addon','demo_','demo_echo')
            out=self._run(tmp,"import bridge_server as b,json; print(json.dumps({'listed':any(x['name']=='demo_echo' for x in b.TOOLS),'value':b.call('demo_echo',{'value':9}),'addons':b.call('addon_status',{})}))")
            data=json.loads(out);self.assertTrue(data['listed']);self.assertEqual(data['value']['value'],9);self.assertIn('demo-addon',data['addons']['addons'])
    def test_arbitrary_external_namespace_needs_no_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._addon(tmp,'media-addon','media_','media_probe')
            out=self._run(tmp,"import bridge_server as b,json; print(json.dumps({'legacy_symbol':hasattr(b,'MEDIA_MODULE'),'listed':any(x['name']=='media_probe' for x in b.TOOLS),'value':b.call('media_probe',{'value':4})}))")
            data=json.loads(out);self.assertFalse(data['legacy_symbol']);self.assertTrue(data['listed']);self.assertEqual(data['value']['value'],4)
