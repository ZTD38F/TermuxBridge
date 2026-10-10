import importlib.util
from pathlib import Path
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"runtime"))
SPEC = importlib.util.spec_from_file_location("bridge_server_phone_names", ROOT/"runtime"/"bridge_server.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class PhoneToolNameTests(unittest.TestCase):
    def test_public_name_does_not_double_prefix(self):
        self.assertEqual(bridge._phone_public_name("phone_health"), "phone_health")
        self.assertEqual(bridge._phone_public_name("get_battery"), "phone_get_battery")

    def test_source_name_accepts_canonical_and_legacy_alias(self):
        source_tools = {"phone_health": object(), "get_battery": object()}
        self.assertEqual(bridge._phone_source_name("phone_health", source_tools), "phone_health")
        self.assertEqual(bridge._phone_source_name("phone_phone_health", source_tools), "phone_health")
        self.assertEqual(bridge._phone_source_name("phone_get_battery", source_tools), "get_battery")


if __name__ == "__main__":
    unittest.main()
