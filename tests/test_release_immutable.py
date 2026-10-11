"""Source releases must not reuse or silently replace a previous tag.
Author: @dvkrum
"""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]

class ReleaseGuardTests(unittest.TestCase):
    def test_exact_commit_immutable_release(self):
        source=(ROOT/".github/workflows/release.yml").read_text()
        self.assertIn("termuxbridge-stable-release",source)
        self.assertIn("cancel-in-progress: false",source)
        self.assertIn("git ls-remote --tags --refs origin",source)
        self.assertIn("bump BRIDGE_VERSION",source)
        self.assertIn('--target "$GITHUB_SHA"',source)
        self.assertIn("scheduled_maintain_bridge.sh update_termux.sh",source)
        self.assertIn("Run complete regression suite (required before publishing)",source)
        self.assertLess(source.index("Run complete regression suite (required before publishing)"),source.index("Publish stable release"))
        self.assertNotIn("gh release upload",source)

if __name__=="__main__":
    unittest.main()
