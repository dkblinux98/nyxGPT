"""RAG query result caching module.

Provides in-memory caching for RAG query results with:
- Query fingerprinting (SHA-256 hash)
- TTL-based expiration
- Cache invalidation on document changes
- Hit rate monitoring and statistics
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Single cache entry with results and metadata."""

    results: list[dict]
    timestamp: float
    ttl: int
    hit_count: int = 0
    debug_info: Any | None = None  # Optional RAGDebugInfo if cached with debug


@dataclass
class CacheStats:
    """Cache performance statistics."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0
    total_entries: int = 0
    total_size_bytes: int = 0

    @property
    def total_queries(self) -> int:
        """Total number of cache queries (hits + misses)."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Cache hit rate as percentage (0-100)."""
        if self.total_queries == 0:
            return 0.0
        return (self.hits / self.total_queries) * 100


class RAGQueryCache:
    """Thread-safe in-memory cache for RAG query results.

    Features:
    - Query fingerprinting using SHA-256
    - TTL-based automatic expiration
    - LRU-style eviction when max size reached
    - Invalidation on document changes
    - Hit rate monitoring
    """

    def __init__(
        self,
        enabled: bool = True,
        ttl_seconds: int = 300,
        max_size: int = 1000,
    ):
        """Initialize RAG query cache.

        Args:
            enabled: Whether caching is enabled
            ttl_seconds: Time-to-live for cache entries in seconds
            max_size: Maximum number of entries before eviction
        """
        self._enabled = enabled
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._cache: dict[str, CacheEntry] = {}
        self._stats = CacheStats()
        self._lock = threading.RLock()

        log.info(
            f"RAG cache initialized: enabled={enabled}, ttl={ttl_seconds}s, "
            f"max_size={max_size}"
        )

    @property
    def enabled(self) -> bool:
        """Check if caching is enabled."""
        return self._enabled

    def _generate_key(
        self,
        query: str,
        top_k: int | None,
        collection: str,
        embedding_model: str | None,
        embedding_dim: int | None,
        metadata_filter: dict[str, Any] | None,
    ) -> str:
        """Generate cache key fingerprint from query parameters.

        Uses SHA-256 hash of JSON-serialized parameters for consistent
        fingerprinting across queries with identical parameters.

        Args:
            query: Search query text
            top_k: Number of results to retrieve
            collection: Collection name
            embedding_model: Embedding model name
            embedding_dim: Embedding dimension
            metadata_filter: Metadata filter dict

        Returns:
            SHA-256 hash string (hex digest)
        """
        # Normalize parameters to ensure consistent hashing
        params = {
            "query": query.strip().lower(),  # Normalize query
            "top_k": top_k,
            "collection": collection,
            "embedding_model": embedding_model,
            "embedding_dim": embedding_dim,
            "metadata_filter": metadata_filter,
        }

        # JSON serialize with sorted keys for consistency
        param_str = json.dumps(params, sort_keys=True)

        # Generate SHA-256 hash
        return hashlib.sha256(param_str.encode("utf-8")).hexdigest()

    def get(
        self,
        query: str,
        top_k: int | None,
        collection: str = "default",
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> tuple[list[dict], Any | None] | None:
        """Retrieve cached results if available and not expired.

        Args:
            query: Search query text
            top_k: Number of results to retrieve
            collection: Collection name
            embedding_model: Embedding model name
            embedding_dim: Embedding dimension
            metadata_filter: Metadata filter dict

        Returns:
            Tuple of (results, debug_info) if cache hit, None if miss
        """
        if not self._enabled:
            return None

        key = self._generate_key(
            query, top_k, collection, embedding_model, embedding_dim, metadata_filter
        )

        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._stats.misses += 1
                log.debug(f"Cache miss for key: {key[:16]}...")
                return None

            # Check if entry has expired
            age = time.time() - entry.timestamp
            if age > entry.ttl:
                # Expired - remove and count as miss
                del self._cache[key]
                self._stats.misses += 1
                self._stats.evictions += 1
                log.debug(
                    f"Cache entry expired (age={age:.1f}s > ttl={entry.ttl}s): "
                    f"{key[:16]}..."
                )
                return None

            # Cache hit - increment counters
            entry.hit_count += 1
            self._stats.hits += 1
            log.debug(
                f"Cache hit for key: {key[:16]}... (hit_count={entry.hit_count}, "
                f"age={age:.1f}s)"
            )

            return (entry.results, entry.debug_info)

    def put(
        self,
        query: str,
        top_k: int | None,
        results: list[dict],
        collection: str = "default",
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
        debug_info: Any | None = None,
    ) -> None:
        """Store query results in cache.

        Args:
            query: Search query text
            top_k: Number of results to retrieve
            results: Query results to cache
            collection: Collection name
            embedding_model: Embedding model name
            embedding_dim: Embedding dimension
            metadata_filter: Metadata filter dict
            debug_info: Optional debug information to cache
        """
        if not self._enabled:
            return

        key = self._generate_key(
            query, top_k, collection, embedding_model, embedding_dim, metadata_filter
        )

        with self._lock:
            # Evict oldest entry if at max size
            if len(self._cache) >= self._max_size and key not in self._cache:
                self._evict_oldest()

            # Create and store cache entry
            entry = CacheEntry(
                results=results,
                timestamp=time.time(),
                ttl=self._ttl_seconds,
                hit_count=0,
                debug_info=debug_info,
            )

            self._cache[key] = entry
            log.debug(
                f"Cached results for key: {key[:16]}... "
                f"({len(results)} results, ttl={self._ttl_seconds}s)"
            )

    def _evict_oldest(self) -> None:
        """Evict the oldest cache entry (LRU-style).

        Called when cache is full and a new entry needs to be added.
        Must be called with lock held.
        """
        if not self._cache:
            return

        # Find oldest entry by timestamp
        oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].timestamp)
        del self._cache[oldest_key]
        self._stats.evictions += 1
        log.debug(f"Evicted oldest entry: {oldest_key[:16]}...")

    def invalidate_all(self) -> int:
        """Clear entire cache (e.g., after document ingestion).

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._stats.invalidations += count
            log.info(f"Cache invalidated: {count} entries cleared")
            return count

    def invalidate_collection(self, collection: str) -> int:
        """Invalidate all cache entries for a specific collection.

        Args:
            collection: Collection name to invalidate

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            # Need to check each key to see if it matches the collection
            # This is inefficient but acceptable for occasional invalidation
            keys_to_remove = []

            for key, entry in self._cache.items():
                # We can't easily decode the hash, so we track collection in a separate index
                # For now, invalidate all to be safe
                # TODO: Consider storing collection as part of cache key for selective invalidation
                keys_to_remove.append(key)

            for key in keys_to_remove:
                del self._cache[key]

            count = len(keys_to_remove)
            self._stats.invalidations += count
            log.info(f"Cache invalidated for collection '{collection}': {count} entries cleared")
            return count

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics including hit rate
        """
        with self._lock:
            # Update current entry count
            self._stats.total_entries = len(self._cache)

            # Estimate total size (rough approximation)
            # Each entry has results list + metadata
            self._stats.total_size_bytes = sum(
                len(json.dumps(entry.results)) for entry in self._cache.values()
            )

            return {
                "enabled": self._enabled,
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "total_queries": self._stats.total_queries,
                "hit_rate": self._stats.hit_rate,
                "evictions": self._stats.evictions,
                "invalidations": self._stats.invalidations,
                "total_entries": self._stats.total_entries,
                "total_size_bytes": self._stats.total_size_bytes,
                "max_size": self._max_size,
                "ttl_seconds": self._ttl_seconds,
            }

    def reset_stats(self) -> None:
        """Reset cache statistics (but keep cached entries)."""
        with self._lock:
            self._stats = CacheStats()
            log.info("Cache statistics reset")


# Global cache instance
_global_cache: RAGQueryCache | None = None


def get_cache() -> RAGQueryCache:
    """Get the global RAG query cache instance.

    Initializes cache on first call using config settings.

    Returns:
        Global RAGQueryCache instance
    """
    global _global_cache

    if _global_cache is None:
        # Import here to avoid circular dependency
        from nyxgpt.config import load_config

        cfg = load_config()
        enabled = cfg.getboolean("rag", "cache_enabled", fallback=True)
        ttl = cfg.getint("rag", "cache_ttl_seconds", fallback=300)
        max_size = cfg.getint("rag", "cache_max_size", fallback=1000)

        _global_cache = RAGQueryCache(
            enabled=enabled,
            ttl_seconds=ttl,
            max_size=max_size,
        )

    return _global_cache
