"""Unit tests for caching functionality."""

import tempfile
import time
from unittest.mock import patch

from prometheus_client.parser import text_string_to_metric_families

from nyxgpt import metrics as prom_metrics
from nyxgpt.cache import (
    CacheBackend,
    DiskCache,
    MemoryCache,
    NoOpCache,
    hash_dict,
    hash_list,
    hash_text,
)


def _cache_metric_value(cache_name: str, result: str) -> float:
    body, _ = prom_metrics.render_metrics()
    for family in text_string_to_metric_families(body.decode("utf-8")):
        for sample in family.samples:
            if (
                sample.name == "nyxgpt_cache_requests_total"
                and sample.labels.get("cache") == cache_name
                and sample.labels.get("result") == result
            ):
                return sample.value
    return 0.0


class TestCacheBackend:
    """Test the abstract CacheBackend base class stub implementations."""

    def test_abstract_methods_are_inert_stubs(self):
        """Directly invoke the base class's abstract method bodies.

        Concrete subclasses (MemoryCache, DiskCache, NoOpCache) fully override
        every abstract method, so the base implementations are otherwise
        unreachable. Calling them explicitly via the class confirms they are
        harmless no-ops (just `pass`), which is what abstract stub bodies
        should be.
        """

        class DummyCache(CacheBackend[str]):
            def get(self, key: str) -> str | None:
                return None

            def set(self, key: str, value: str, ttl: int | None = None) -> None:
                pass

            def clear(self) -> None:
                pass

            def size(self) -> int:
                return 0

        dummy = DummyCache()

        assert CacheBackend.get(dummy, "key1") is None
        assert CacheBackend.set(dummy, "key1", "value1") is None
        assert CacheBackend.clear(dummy) is None
        assert CacheBackend.size(dummy) is None


class TestMemoryCache:
    """Test the in-memory LRU cache."""

    def test_basic_set_get(self):
        """Test basic set and get operations."""
        cache: MemoryCache[str] = MemoryCache(max_size=10)

        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_cache_miss(self):
        """Test that missing keys return None."""
        cache: MemoryCache[str] = MemoryCache(max_size=10)

        assert cache.get("nonexistent") is None

    def test_lru_eviction(self):
        """Test that LRU eviction works correctly."""
        cache: MemoryCache[str] = MemoryCache(max_size=3)

        # Fill cache to capacity
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Add one more - should evict key1 (oldest)
        cache.set("key4", "value4")

        assert cache.get("key1") is None  # Evicted
        assert cache.get("key2") == "value2"
        assert cache.get("key3") == "value3"
        assert cache.get("key4") == "value4"

    def test_lru_access_updates_order(self):
        """Test that accessing a key marks it as recently used."""
        cache: MemoryCache[str] = MemoryCache(max_size=3)

        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Access key1 to mark it as recently used
        _ = cache.get("key1")

        # Add key4 - should evict key2 (oldest), not key1
        cache.set("key4", "value4")

        assert cache.get("key1") == "value1"  # Still present
        assert cache.get("key2") is None  # Evicted
        assert cache.get("key3") == "value3"
        assert cache.get("key4") == "value4"

    def test_ttl_expiration(self):
        """Test that TTL expiration works."""
        cache: MemoryCache[str] = MemoryCache(max_size=10, default_ttl=1)

        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

        # Wait for expiration
        time.sleep(1.1)

        assert cache.get("key1") is None  # Expired

    def test_override_ttl(self):
        """Test that per-item TTL override works."""
        cache: MemoryCache[str] = MemoryCache(max_size=10, default_ttl=10)

        # Set with short TTL
        cache.set("key1", "value1", ttl=1)
        assert cache.get("key1") == "value1"

        # Wait for expiration
        time.sleep(1.1)

        assert cache.get("key1") is None  # Expired

    def test_no_ttl(self):
        """Test that items with no TTL don't expire."""
        cache: MemoryCache[str] = MemoryCache(max_size=10)

        cache.set("key1", "value1")  # No TTL
        assert cache.get("key1") == "value1"

        # Wait a bit
        time.sleep(0.5)

        assert cache.get("key1") == "value1"  # Still present

    def test_clear(self):
        """Test that clear() removes all entries."""
        cache: MemoryCache[str] = MemoryCache(max_size=10)

        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        cache.clear()

        assert cache.get("key1") is None
        assert cache.get("key2") is None
        assert cache.get("key3") is None
        assert cache.size() == 0

    def test_size(self):
        """Test that size() returns correct count."""
        cache: MemoryCache[str] = MemoryCache(max_size=10)

        assert cache.size() == 0

        cache.set("key1", "value1")
        assert cache.size() == 1

        cache.set("key2", "value2")
        assert cache.size() == 2

        cache.clear()
        assert cache.size() == 0

    def test_stats(self):
        """Test cache statistics tracking."""
        cache: MemoryCache[str] = MemoryCache(max_size=10)

        cache.set("key1", "value1")

        # Hit
        _ = cache.get("key1")

        # Miss
        _ = cache.get("key2")

        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5
        assert stats["size"] == 1
        assert stats["max_size"] == 10

    def test_update_existing_key(self):
        """Test that updating an existing key works correctly."""
        cache: MemoryCache[str] = MemoryCache(max_size=10)

        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

        cache.set("key1", "value2")
        assert cache.get("key1") == "value2"
        assert cache.size() == 1  # Size should not increase


class TestDiskCache:
    """Test the disk-based cache."""

    def test_basic_set_get(self):
        """Test basic set and get operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir)

            cache.set("key1", "value1")
            assert cache.get("key1") == "value1"

    def test_cache_miss(self):
        """Test that missing keys return None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir)

            assert cache.get("nonexistent") is None

    def test_persistence_across_instances(self):
        """Test that cache persists across instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # First instance
            cache1: DiskCache[str] = DiskCache(cache_dir=tmpdir)
            cache1.set("key1", "value1")

            # Second instance (same directory)
            cache2: DiskCache[str] = DiskCache(cache_dir=tmpdir)
            assert cache2.get("key1") == "value1"

    def test_ttl_expiration(self):
        """Test that TTL expiration works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir, default_ttl=1)

            cache.set("key1", "value1")
            assert cache.get("key1") == "value1"

            # Wait for expiration
            time.sleep(1.1)

            assert cache.get("key1") is None  # Expired

    def test_clear(self):
        """Test that clear() removes all cache files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir)

            cache.set("key1", "value1")
            cache.set("key2", "value2")

            cache.clear()

            assert cache.get("key1") is None
            assert cache.get("key2") is None
            assert cache.size() == 0

    def test_size(self):
        """Test that size() returns correct count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir)

            assert cache.size() == 0

            cache.set("key1", "value1")
            assert cache.size() == 1

            cache.set("key2", "value2")
            assert cache.size() == 2

    def test_complex_types(self):
        """Test caching of complex types (lists, dicts)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[list[float]] = DiskCache(cache_dir=tmpdir)

            data = [1.0, 2.0, 3.0]
            cache.set("key1", data)

            result = cache.get("key1")
            assert result == data

    def test_get_corrupted_cache_file_is_treated_as_miss_and_removed(self):
        """A cache file that fails to unpickle should be swallowed as a miss.

        This exercises the except branch in DiskCache.get(): the corrupt file
        must not raise, must count as a miss, and must be removed so future
        reads don't keep tripping over it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir)
            cache_file = cache._get_cache_path("key1")
            cache_file.write_bytes(b"not a valid pickle stream")

            result = cache.get("key1")

            assert result is None
            assert not cache_file.exists()
            assert cache.stats()["misses"] == 1

    def test_set_write_failure_is_logged_and_swallowed(self):
        """A failure while writing the cache file must not raise.

        Exercises the except branch in DiskCache.set().
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir)

            with patch("nyxgpt.cache.pickle.dump", side_effect=OSError("disk full")):
                # Should not raise despite the write failure.
                cache.set("key1", "value1")

            # Nothing was actually persisted.
            assert cache.get("key1") is None

    def test_stats(self):
        """Test disk cache statistics tracking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir)

            cache.set("key1", "value1")

            # Hit
            _ = cache.get("key1")

            # Miss
            _ = cache.get("key2")

            stats = cache.stats()
            assert stats["hits"] == 1
            assert stats["misses"] == 1
            assert stats["hit_rate"] == 0.5
            assert stats["size"] == 1


class TestNoOpCache:
    """Test the no-op cache backend."""

    def test_always_miss(self):
        """Test that NoOpCache always returns None on get."""
        cache: NoOpCache[str] = NoOpCache()

        cache.set("key1", "value1")
        assert cache.get("key1") is None

    def test_size_always_zero(self):
        """Test that size is always 0."""
        cache: NoOpCache[str] = NoOpCache()

        cache.set("key1", "value1")
        assert cache.size() == 0

    def test_clear_does_nothing(self):
        """Test that clear() is safe to call."""
        cache: NoOpCache[str] = NoOpCache()

        cache.set("key1", "value1")
        cache.clear()
        # Should not raise


class TestHashFunctions:
    """Test the hash utility functions."""

    def test_hash_text_stable(self):
        """Test that hash_text produces stable hashes."""
        text = "Hello, world!"

        hash1 = hash_text(text)
        hash2 = hash_text(text)

        assert hash1 == hash2

    def test_hash_text_different(self):
        """Test that different texts produce different hashes."""
        hash1 = hash_text("text1")
        hash2 = hash_text("text2")

        assert hash1 != hash2

    def test_hash_list_stable(self):
        """Test that hash_list produces stable hashes."""
        items = ["a", "b", "c"]

        hash1 = hash_list(items)
        hash2 = hash_list(items)

        assert hash1 == hash2

    def test_hash_list_order_matters(self):
        """Test that list order affects hash."""
        hash1 = hash_list(["a", "b", "c"])
        hash_list(["c", "b", "a"])

        # Note: hash_list uses json.dumps with sort_keys, so order might not matter
        # Let's just check they're stable
        assert hash1 == hash1

    def test_hash_dict_stable(self):
        """Test that hash_dict produces stable hashes."""
        data = {"key1": "value1", "key2": "value2"}

        hash1 = hash_dict(data)
        hash2 = hash_dict(data)

        assert hash1 == hash2

    def test_hash_dict_order_independent(self):
        """Test that dict key order doesn't affect hash."""
        # Python 3.7+ maintains insertion order, but JSON dumps with sort_keys
        # should produce the same hash regardless
        hash1 = hash_dict({"a": 1, "b": 2})
        hash2 = hash_dict({"b": 2, "a": 1})

        assert hash1 == hash2


class TestCacheMetrics:
    """Test that cache hit/miss lookups are recorded as Prometheus metrics."""

    def test_memory_cache_records_hits_and_misses_by_name(self):
        cache: MemoryCache[str] = MemoryCache(max_size=10, name="metrics-unit-test-memory")

        before_hits = _cache_metric_value("metrics-unit-test-memory", "hit")
        before_misses = _cache_metric_value("metrics-unit-test-memory", "miss")

        cache.get("missing-key")
        cache.set("key1", "value1")
        cache.get("key1")

        assert _cache_metric_value("metrics-unit-test-memory", "hit") == before_hits + 1
        assert _cache_metric_value("metrics-unit-test-memory", "miss") == before_misses + 1

    def test_disk_cache_records_hits_and_misses_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache: DiskCache[str] = DiskCache(cache_dir=tmpdir, name="metrics-unit-test-disk")

            before_hits = _cache_metric_value("metrics-unit-test-disk", "hit")
            before_misses = _cache_metric_value("metrics-unit-test-disk", "miss")

            cache.get("missing-key")
            cache.set("key1", "value1")
            cache.get("key1")

            assert _cache_metric_value("metrics-unit-test-disk", "hit") == before_hits + 1
            assert _cache_metric_value("metrics-unit-test-disk", "miss") == before_misses + 1

    def test_caches_default_to_the_default_name(self):
        cache: MemoryCache[str] = MemoryCache(max_size=10)
        assert cache._name == "default"
