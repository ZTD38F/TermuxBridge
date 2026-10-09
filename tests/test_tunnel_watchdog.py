from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from runtime.tunnel_watchdog import classify_error, retry_delay, update_status


class TunnelWatchdogTests(unittest.TestCase):
    def test_error_categories(self):
        cases = {
            "2026 ERROR rejected: HTTP 401 unauthorized": "AUTHORIZATION",
            "2026 ERROR x509: certificate signed by unknown authority": "TLS",
            "2026 WARN proxyconnect tcp refused": "PROXY",
            "2026 ERROR DNS lookup: no such host": "DNS",
            "2026 WARN timeout while dialing": "NETWORK",
            "2026 INFO tls trust summary Certificates are configured": "TLS",
            "": "UNKNOWN",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_error(message), expected)

    def test_bounded_retry(self):
        self.assertEqual(retry_delay(1, "UNKNOWN"), 2)
        self.assertEqual(retry_delay(4, "NETWORK"), 16)
        self.assertEqual(retry_delay(200, "NETWORK"), 256)
        self.assertEqual(retry_delay(2, "AUTHORIZATION"), 300)

    def test_status_is_machine_readable_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            update_status(root, state="RETRYING", reason="NETWORK", attempts=2, retry_seconds=4)
            path = root / "state" / "tunnel_status.json"
            data = json.loads(path.read_text())
            self.assertEqual(data["state"], "RETRYING")
            self.assertEqual(data["retry_seconds"], 4)
            self.assertNotIn("token", data)
            self.assertNotIn("credential", data)


class ManagedLauncherContracts(unittest.TestCase):
    def test_healthy_local_stack_is_not_restarted_due_to_tunnel_failure(self):
        text = Path("runtime/start_bridge.sh").read_text()
        self.assertIn("start_tunnel_watchdog", text)
        block = text[text.index("# A tunnel failure MUST NOT"):text.index("# Startup/reboot recovery")]
        self.assertIn("backend_health", block)
        self.assertIn("router_health", block)
        self.assertNotIn("stop_bridge.sh", block)

    def test_force_update_repairs_launcher_without_same_generation_activation(self):
        text = Path("update_termux.sh").read_text()
        self.assertIn('if [[ "$CURRENT" == "$TARGET" && "${1:-}" != "--force"', text)
        block = text[text.index("# A previous partial update"):text.index("CANDIDATE=")]
        self.assertIn('install_management_from_stage "$TMP/source"', block)
        self.assertIn("exit 0", block)

    def test_stop_order_prevents_watchdog_restart_race(self):
        text = Path("runtime/stop_bridge.sh").read_text()
        self.assertLess(text.index('stop_pidfile "$ROOT/watchdog.pid" watchdog'), 
                        text.index('stop_pidfile "$ROOT/bridge.pid" tunnel'))


if __name__ == "__main__":
    unittest.main()
