"""Tests for RAG query result caching."""

import time
from unittest.mock import patch

import pytest

from nyxgpt.rag.cache import RAGQueryCache, CacheStats


class TestRAGQueryCache:
    """Test suite for RAG query cache functionality."""

    def test_cache_initialization(self):
        """Test cache initialization with default parameters."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        assert cache.enabled is True
        assert cache._ttl_seconds == 300
        assert cache._max_size == 100
        assert len(cache._cache) == 0

    def test_cache_disabled(self):
        """Test that disabled cache doesn't store or retrieve results."""
        cache = RAGQueryCache(enabled=False, ttl_seconds=300, max_size=100)

        # Try to get from cache
        result = cache.get("test query", top_k=5)
        assert result is None

        # Try to put in cache
        cache.put("test query", top_k=5, results=[{"text": "result"}])

        # Should still be empty
        assert len(cache._cache) == 0

    def test_cache_put_and_get(self):
        """Test basic cache put and get operations."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        # Put results in cache
        results = [
            {"doc_id": "doc1", "chunk_id": 1, "text": "test content", "score": 0.9}
        ]
        cache.put("test query", top_k=5, results=results)

        # Get from cache
        cached = cache.get("test query", top_k=5)
        assert cached is not None
        cached_results, cached_debug = cached
        assert cached_results == results
        assert cached_debug is None

    def test_cache_key_fingerprinting(self):
        """Test that cache keys are correctly fingerprinted."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        results1 = [{"text": "result1"}]
        results2 = [{"text": "result2"}]

        # Same query, same params -> same cache entry
        cache.put("query1", top_k=5, results=results1, collection="default")
        cached = cache.get("query1", top_k=5, collection="default")
        assert cached is not None
        assert cached[0] == results1

        # Same query, different top_k -> different cache entry
        cache.put("query1", top_k=10, results=results2, collection="default")
        cached = cache.get("query1", top_k=10, collection="default")
        assert cached is not None
        assert cached[0] == results2

        # Original entry still exists
        cached = cache.get("query1", top_k=5, collection="default")
        assert cached is not None
        assert cached[0] == results1

    def test_cache_ttl_expiration(self):
        """Test that cache entries expire after TTL."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=1, max_size=100)

        results = [{"text": "test"}]
        cache.put("test query", top_k=5, results=results)

        # Should hit immediately
        cached = cache.get("test query", top_k=5)
        assert cached is not None

        # Wait for TTL to expire
        time.sleep(1.1)

        # Should miss after expiration
        cached = cache.get("test query", top_k=5)
        assert cached is None

    def test_cache_max_size_eviction(self):
        """Test that oldest entries are evicted when max size is reached."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=3)

        # Add 3 entries (at max capacity)
        for i in range(3):
            cache.put(f"query{i}", top_k=5, results=[{"text": f"result{i}"}])
            time.sleep(0.01)  # Small delay to ensure different timestamps

        # All 3 should be cached
        assert len(cache._cache) == 3

        # Add a 4th entry - should evict oldest (query0)
        cache.put("query3", top_k=5, results=[{"text": "result3"}])

        # Should have 3 entries (max size)
        assert len(cache._cache) == 3

        # query0 should be evicted
        assert cache.get("query0", top_k=5) is None

        # query1, query2, query3 should still be cached
        assert cache.get("query1", top_k=5) is not None
        assert cache.get("query2", top_k=5) is not None
        assert cache.get("query3", top_k=5) is not None

    def test_cache_invalidate_all(self):
        """Test invalidating entire cache."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        # Add multiple entries
        for i in range(5):
            cache.put(f"query{i}", top_k=5, results=[{"text": f"result{i}"}])

        assert len(cache._cache) == 5

        # Invalidate all
        count = cache.invalidate_all()
        assert count == 5
        assert len(cache._cache) == 0

        # All queries should miss
        for i in range(5):
            assert cache.get(f"query{i}", top_k=5) is None

    def test_cache_invalidate_collection(self):
        """Test invalidating cache entries for a specific collection."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        # Add entries for different collections
        cache.put("query1", top_k=5, results=[{"text": "r1"}], collection="col1")
        cache.put("query2", top_k=5, results=[{"text": "r2"}], collection="col2")
        cache.put("query3", top_k=5, results=[{"text": "r3"}], collection="col1")

        assert len(cache._cache) == 3

        # Invalidate collection (current implementation invalidates all)
        # TODO: Implement selective invalidation by collection
        count = cache.invalidate_collection("col1")
        assert count == 3  # Currently invalidates all
        assert len(cache._cache) == 0

    def test_cache_hit_rate_statistics(self):
        """Test cache statistics tracking."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        # Initial stats
        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["hit_rate"] == 0.0

        # Add entry and hit it
        cache.put("query1", top_k=5, results=[{"text": "r1"}])
        cache.get("query1", top_k=5)  # Hit

        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 0
        assert stats["hit_rate"] == 100.0

        # Miss on different query
        cache.get("query2", top_k=5)  # Miss

        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 50.0

    def test_cache_eviction_statistics(self):
        """Test eviction statistics tracking."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=1, max_size=2)

        # Add entries
        cache.put("query1", top_k=5, results=[{"text": "r1"}])
        cache.put("query2", top_k=5, results=[{"text": "r2"}])

        stats = cache.get_stats()
        assert stats["evictions"] == 0

        # Add 3rd entry - should evict oldest
        cache.put("query3", top_k=5, results=[{"text": "r3"}])

        stats = cache.get_stats()
        assert stats["evictions"] == 1

        # Wait for TTL and access - should count as eviction
        time.sleep(1.1)
        cache.get("query2", top_k=5)  # Expired, counts as eviction

        stats = cache.get_stats()
        assert stats["evictions"] == 2

    def test_cache_invalidation_statistics(self):
        """Test invalidation statistics tracking."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        # Add entries
        for i in range(5):
            cache.put(f"query{i}", top_k=5, results=[{"text": f"r{i}"}])

        stats = cache.get_stats()
        assert stats["invalidations"] == 0

        # Invalidate all
        cache.invalidate_all()

        stats = cache.get_stats()
        assert stats["invalidations"] == 5

    def test_cache_reset_stats(self):
        """Test resetting cache statistics."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        # Generate some activity
        cache.put("query1", top_k=5, results=[{"text": "r1"}])
        cache.get("query1", top_k=5)  # Hit
        cache.get("query2", top_k=5)  # Miss

        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1

        # Reset stats
        cache.reset_stats()

        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["hit_rate"] == 0.0

        # Cache entry should still exist
        assert cache.get("query1", top_k=5) is not None

    def test_cache_with_metadata_filter(self):
        """Test cache key generation with metadata filters."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        results1 = [{"text": "r1"}]
        results2 = [{"text": "r2"}]

        # Different metadata filters should create different cache entries
        filter1 = {"doc_ids": ["doc1"]}
        filter2 = {"doc_ids": ["doc2"]}

        cache.put("query", top_k=5, results=results1, metadata_filter=filter1)
        cache.put("query", top_k=5, results=results2, metadata_filter=filter2)

        # Should get different results for different filters
        cached1 = cache.get("query", top_k=5, metadata_filter=filter1)
        cached2 = cache.get("query", top_k=5, metadata_filter=filter2)

        assert cached1 is not None
        assert cached2 is not None
        assert cached1[0] == results1
        assert cached2[0] == results2

    def test_cache_with_debug_info(self):
        """Test caching with debug information."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        results = [{"text": "r1"}]
        debug_info = {"total_time_ms": 123.45, "query": "test"}

        # Put with debug info
        cache.put("query", top_k=5, results=results, debug_info=debug_info)

        # Get should return both results and debug info
        cached = cache.get("query", top_k=5)
        assert cached is not None
        cached_results, cached_debug = cached
        assert cached_results == results
        assert cached_debug == debug_info

    def test_cache_query_normalization(self):
        """Test that queries are normalized for cache keys."""
        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=100)

        results = [{"text": "r1"}]

        # Put with specific case and whitespace
        cache.put("  Test Query  ", top_k=5, results=results)

        # Should hit with different case/whitespace
        cached = cache.get("test query", top_k=5)
        assert cached is not None
        assert cached[0] == results

    def test_cache_thread_safety(self):
        """Test that cache operations are thread-safe."""
        import threading

        cache = RAGQueryCache(enabled=True, ttl_seconds=300, max_size=1000)

        def worker(thread_id):
            for i in range(10):
                query = f"query{thread_id}_{i}"
                results = [{"text": f"result{thread_id}_{i}"}]
                cache.put(query, top_k=5, results=results)
                cached = cache.get(query, top_k=5)
                assert cached is not None

        # Run 10 threads concurrently
        threads = []
        for tid in range(10):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Should have 100 entries (10 threads * 10 queries each)
        assert len(cache._cache) == 100

    def test_get_cache_global_instance(self):
        """Test global cache instance creation from config."""
        from nyxgpt.rag.cache import get_cache

        # Mock config
        with patch("nyxgpt.rag.cache.load_config") as mock_cfg:
            mock_config = {
                "rag": {
                    "cache_enabled": "true",
                    "cache_ttl_seconds": "600",
                    "cache_max_size": "500",
                }
            }

            mock_cfg.return_value.getboolean.return_value = True
            mock_cfg.return_value.getint.side_effect = [600, 500]

            # Reset global cache
            import nyxgpt.rag.cache

            nyxgpt.rag.cache._global_cache = None

            cache = get_cache()
            assert cache is not None
            assert cache.enabled is True
