"""Regression checks for safe startup lock and staged transaction recovery."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartupLockTests(unittest.TestCase):
    def test_all_long_lived_children_close_lock_descriptor(self):
        data = (ROOT / "runtime/start_bridge.sh").read_text()
        self.assertIn('LOCKFILE="$BRIDGE_DIR/.start.v2.lock"', data)
        for line in data.splitlines():
            if "nohup " in line and not line.lstrip().startswith("#"):
                self.assertIn("9>&- &", line, msg=f"Daemon inherited startup flock: {line}")

    def test_stale_stage_can_be_verified_and_reused_without_delete(self):
        data = (ROOT / "update_termux.sh").read_text()
        self.assertIn('cmp -s "$TMP/source/runtime/$file" "$CANDIDATE/$file"', data)
        self.assertIn("reusing checksum-verified existing candidate", data)
        self.assertIn("existing candidate differs from verified release", data)
        self.assertIn("candidate generation is active, linked, or invalid", data)

    def test_pid_zero_never_signalled(self):
        updater = (ROOT / "update_termux.sh").read_text()
        start = (ROOT / "runtime/start_bridge.sh").read_text()
        stop = (ROOT / "runtime/stop_bridge.sh").read_text()
        self.assertIn('"${1:-0}" -gt 1', updater)
        self.assertIn('"$pid" -gt 1', start)
        self.assertIn('"$pid" -gt 1', stop)

    def test_concurrent_update_is_reported(self):
        data = (ROOT / "update_termux.sh").read_text()
        self.assertIn('flock -n 9 || { echo "Bridge update is already in progress', data)
        self.assertIn('exit 75;', data)

    def test_force_repair_marks_transaction_committed(self):
        data = (ROOT / "update_termux.sh").read_text()
        subsection = data[data.index("# A previous partial update"):data.index('CANDIDATE="$RELEASES/$TARGET"')]
        self.assertIn('install_management_from_stage "$TMP/source"', subsection)
        self.assertIn('journal COMMITTED', subsection)


if __name__ == "__main__":
    unittest.main()

class CandidateRollbackContracts(unittest.TestCase):
    def test_failed_pre_switch_candidate_is_retired_and_recorded(self):
        text = Path("update_termux.sh").read_text()
        self.assertIn('if candidate_owned "$CANDIDATE_PID" "$TARGET" "$CANDIDATE_PORT"; then', text)
        self.assertIn('kill -TERM "$CANDIDATE_PID"', text)
        self.assertIn('d.get("phase") == "CANDIDATE_STARTING"', text)
        self.assertIn('journal CANDIDATE_STARTING\nfor _ in {1..40}', text)
        self.assertIn('candidate port is already occupied', text)

    def test_interrupted_candidate_recovery_checks_process_identity(self):
        text = Path("update_termux.sh").read_text()
        self.assertIn('candidate_owned "$cand_pid" "$cand_gen" "$cand_port"', text)
        self.assertIn('if path.stat().st_uid!=os.getuid()', text)
        self.assertIn('args.index("--http")', text)
