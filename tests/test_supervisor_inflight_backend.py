"""Generation-compatible backend-specific drain counters.
Author: @dvkrum.
"""
import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"runtime"))
import supervisor as s

class BackendInflightTests(unittest.TestCase):
    def setUp(self):
        with s._guard:
            s._inflight.clear()
            s._inflight_by_backend.clear()

    def test_same_generation_different_backend(self):
        a="a"*40
        s._inflight_add(a,18771,1)
        s._inflight_add(a,18772,1)
        s._inflight_add(a,18771,-1)
        self.assertEqual(s._inflight[a],1)
        self.assertEqual(s._inflight_by_backend[s.backend_key(a,18771)],0)
        self.assertEqual(s._inflight_by_backend[s.backend_key(a,18772)],1)

    def test_snapshot_remains_backward_compatible(self):
        gen="b"*40
        s._inflight_add(gen,18771,2)
        with patch.object(s,"_read_route",return_value={"generation":gen,"port":18771}):
            data=s._snapshot()
        self.assertEqual(data["inflight"][gen],2)
        self.assertEqual(data["inflight_by_backend"][s.backend_key(gen,18771)],2)
        self.assertEqual(data["backend_key"],s.backend_key(gen,18771))

    def test_counts_never_negative(self):
        s._inflight_add("x"*40,18771,-1)
        self.assertEqual(s._inflight["x"*40],0)
        self.assertEqual(s._inflight_by_backend[s.backend_key("x"*40,18771)],0)

if __name__=="__main__":
    unittest.main()
