"""No-network fail-closed tests for same-generation addon hot reload.
Author: @dvkrum.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import addon_reload as a

CORE = {"name":"core_status","inputSchema":{"type":"object"}}
OLD = {"name":"demo_old","inputSchema":{"type":"object"}}
NEW = {"name":"demo_new","inputSchema":{"type":"object"}}
INFO_OLD = {"demo-addon":{"version":"0.1.0","namespace":"demo_","status":"READY"}}
INFO_NEW = {"demo-addon":{"version":"0.2.0","namespace":"demo_","status":"READY"}}

class AddonReloadTests(unittest.TestCase):
    def test_core_contract_preserved_and_addon_can_change(self):
        changed = a.verify_contract([CORE,OLD],[CORE,NEW],INFO_OLD,INFO_NEW)
        self.assertEqual(changed,["demo-addon"])

    def test_reject_core_mutation(self):
        invalid=dict(CORE,inputSchema={"type":"string"})
        with self.assertRaisesRegex(RuntimeError,"core"):
            a.verify_contract([CORE,OLD],[invalid,NEW],INFO_OLD,INFO_NEW)

    def test_reject_addon_removed_or_invalid(self):
        with self.assertRaisesRegex(RuntimeError,"removed"):
            a.verify_contract([CORE,OLD],[CORE],INFO_OLD,{})
        bad = {"demo-addon":dict(INFO_NEW["demo-addon"],status="FAILED_LOAD")}
        with self.assertRaisesRegex(RuntimeError,"not ready"):
            a.verify_contract([CORE,OLD],[CORE,NEW],INFO_OLD,bad)

    def test_atomic_state_file_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"state.json"
            a.atomic(path,{"phase":"COMMITTED","pid":123})
            self.assertEqual(json.loads(path.read_text())["phase"],"COMMITTED")
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_interrupted_switch_restores_route_without_killing_old(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); state=root/"state";state.mkdir()
            gen="a"*40
            (state/"route.json").write_text(json.dumps({"generation":gen,"port":18772}))
            (state/"addon_reload.json").write_text(json.dumps({
                "phase":"SWITCHED","generation":gen,"old_port":18771,"new_port":18772,"old_pid":123}))
            with patch.multiple(a,ROOT=root,STATE=state,ROUTE=state/"route.json",
                                JOURNAL=state/"addon_reload.json"), \
                 patch.object(a,"identity",return_value=100):
                a.recover_interrupted()
            self.assertEqual(json.loads((state/"route.json").read_text())["port"],18771)
            self.assertEqual(json.loads((state/"addon_reload.json").read_text())["phase"],"FAILED_ROLLED_BACK")

    def test_interrupted_not_recovered_without_old_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);state=root/"state";state.mkdir()
            gen="b"*40;route={"generation":gen,"port":18772}
            (state/"route.json").write_text(json.dumps(route))
            (state/"addon_reload.json").write_text(json.dumps({
                "phase":"SWITCHED","generation":gen,"old_port":18771,"new_port":18772,"old_pid":123}))
            with patch.multiple(a,ROOT=root,STATE=state,ROUTE=state/"route.json",
                                JOURNAL=state/"addon_reload.json"), \
                 patch.object(a,"identity",side_effect=RuntimeError("missing backend")):
                with self.assertRaisesRegex(RuntimeError,"missing backend"):
                    a.recover_interrupted()
            self.assertEqual(json.loads((state/"route.json").read_text()),route)

    def test_cli_and_release(self):
        root=Path(__file__).resolve().parents[1]
        cli=(root/"termuxbridgectl").read_text()
        self.assertIn("addon-reload)",cli)
        self.assertIn("addon-reload-status)",cli)
        self.assertIn('BRIDGE_VERSION = "1.2.37"',(root/"runtime/bridge_server.py").read_text())

    def test_old_generation_inflight_compat(self):
        import supervisor
        with supervisor._guard:
            supervisor._inflight.clear()
            supervisor._inflight_by_backend.clear()
        gen="c"*40
        supervisor._inflight_add(gen,18771,1)
        supervisor._inflight_add(gen,18772,1)
        supervisor._inflight_add(gen,18771,-1)
        self.assertEqual(supervisor._inflight[gen],1)
        self.assertEqual(supervisor._inflight_by_backend[f"{gen}:18771"],0)
        self.assertEqual(supervisor._inflight_by_backend[f"{gen}:18772"],1)

if __name__ == "__main__":
    unittest.main()
