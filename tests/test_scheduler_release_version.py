"""The JobScheduler fix must ship as a new immutable release.
Author: @dvkrum
"""
import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
class SchedulerReleaseVersionTests(unittest.TestCase):
    def test_new_release_version_and_wrapper_are_bundled(self):
        tree=ast.parse((ROOT/"runtime/bridge_server.py").read_text())
        version=next(ast.literal_eval(s.value) for s in tree.body
                     if isinstance(s,ast.Assign)
                     and any(isinstance(x,ast.Name) and x.id=="BRIDGE_VERSION"
                             for x in s.targets))
        self.assertGreaterEqual(tuple(map(int, version.split("."))), (1, 2, 34))
        updater=(ROOT/"update_termux.sh").read_text()
        self.assertIn("scheduled_maintain_bridge.sh",updater)
        self.assertTrue((ROOT/"runtime/scheduled_maintain_bridge.sh").is_file())
        self.assertIn("cp -a runtime", (ROOT/"build_release_bundle.sh").read_text())

if __name__=="__main__":
    unittest.main()
