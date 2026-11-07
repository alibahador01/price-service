import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shared_cache  # noqa: E402


class TestSharedCache(unittest.TestCase):
    def setUp(self):
        shared_cache.clear_all()

    def test_second_call_within_ttl_reuses_cached_value(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return "value-{}".format(calls["n"])

        first = shared_cache.cached_call("k1", 60, fetch)
        second = shared_cache.cached_call("k1", 60, fetch)
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1)

    def test_call_after_ttl_expiry_refetches(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return calls["n"]

        shared_cache.cached_call("k2", 0.05, fetch)
        time.sleep(0.1)
        shared_cache.cached_call("k2", 0.05, fetch)
        self.assertEqual(calls["n"], 2)

    def test_different_keys_are_independent(self):
        shared_cache.cached_call("a", 60, lambda: "A")
        shared_cache.cached_call("b", 60, lambda: "B")
        self.assertEqual(shared_cache.cached_call("a", 60, lambda: "changed"), "A")
        self.assertEqual(shared_cache.cached_call("b", 60, lambda: "changed"), "B")

    def test_invalidate_forces_refetch(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return calls["n"]

        shared_cache.cached_call("k3", 60, fetch)
        shared_cache.invalidate("k3")
        shared_cache.cached_call("k3", 60, fetch)
        self.assertEqual(calls["n"], 2)

    def test_clear_all_removes_every_key(self):
        shared_cache.cached_call("x", 60, lambda: 1)
        shared_cache.cached_call("y", 60, lambda: 2)
        shared_cache.clear_all()
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return calls["n"]

        shared_cache.cached_call("x", 60, fetch)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
