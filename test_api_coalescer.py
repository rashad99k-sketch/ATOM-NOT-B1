import threading
import time
import unittest

from ggp_api_coalescer import KeyedSingleFlight


class KeyedSingleFlightTests(unittest.TestCase):
    def test_concurrent_same_key_uses_one_fetch(self):
        coalescer = KeyedSingleFlight(stripes=8)
        cache = {}
        cache_lock = threading.Lock()
        fetch_count = [0]
        results = []
        start = threading.Barrier(8)

        def get_cached():
            with cache_lock:
                return cache.get("ohlcv:BTC:15m:120")

        def fetch():
            with cache_lock:
                fetch_count[0] += 1
            time.sleep(0.04)
            return ["frame"]

        def set_cached(value):
            with cache_lock:
                cache["ohlcv:BTC:15m:120"] = value

        def worker():
            start.wait()
            result = coalescer.get_or_fetch(
                "ohlcv:BTC:15m:120",
                get_cached=get_cached,
                fetch=fetch,
                is_valid=lambda value: isinstance(value, list) and len(value) >= 1,
                set_cached=set_cached,
            )
            results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(fetch_count[0], 1)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(source == "FRESH_REQUEST" for _, source in results), 1)
        self.assertEqual(sum(source == "CACHE_HIT" for _, source in results), 7)

    def test_invalid_result_is_not_cached(self):
        coalescer = KeyedSingleFlight()
        writes = []
        result, source = coalescer.get_or_fetch(
            "ohlcv:ETH:15m:120",
            get_cached=lambda: None,
            fetch=lambda: None,
            is_valid=lambda value: value is not None,
            set_cached=writes.append,
        )
        self.assertIsNone(result)
        self.assertEqual(source, "NONE")
        self.assertEqual(writes, [])

    def test_different_keys_can_fetch_independently(self):
        coalescer = KeyedSingleFlight(stripes=64)
        barrier = threading.Barrier(2)
        completed = []

        def worker(key):
            result = coalescer.get_or_fetch(
                key,
                get_cached=lambda: None,
                fetch=lambda: (barrier.wait(timeout=1), key)[1],
                is_valid=lambda value: value is not None,
                set_cached=lambda value: None,
            )
            completed.append(result[0])

        threads = [threading.Thread(target=worker, args=(key,)) for key in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(set(completed), {"A", "B"})


if __name__ == "__main__":
    unittest.main()
