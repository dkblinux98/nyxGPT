"""Caching utilities for embeddings and LLM responses.

This module provides caching backends (memory and disk) with TTL support
for improving performance by avoiding redundant computations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CacheBackend(ABC, Generic[T]):
    """Abstract base class for cache backends."""

    @abstractmethod
    def get(self, key: str) -> T | None:
        """Retrieve a value from the cache.

        Args:
            key: Cache key

        Returns:
            Cached value if found and not expired, None otherwise
        """
        pass

    @abstractmethod
    def set(self, key: str, value: T, ttl: int | None = None) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (None = no expiration)
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all cached values."""
        pass

    @abstractmethod
    def size(self) -> int:
        """Return the number of items in the cache."""
        pass


class MemoryCache(CacheBackend[T]):
    """In-memory LRU cache with TTL support.

    Uses an OrderedDict for LRU eviction when max_size is reached.
    Stores (value, expiry_time) tuples to support TTL.
    """

    def __init__(self, max_size: int = 1000, default_ttl: int | None = None):
        """Initialize memory cache.

        Args:
            max_size: Maximum number of items to store (LRU eviction)
            default_ttl: Default TTL in seconds (None = no expiration)
        """
        self._cache: OrderedDict[str, tuple[T, float | None]] = OrderedDict()
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> T | None:
        """Retrieve a value from the cache.

        Args:
            key: Cache key

        Returns:
            Cached value if found and not expired, None otherwise
        """
        if key not in self._cache:
            self._misses += 1
            return None

        value, expiry = self._cache[key]

        # Check expiration
        if expiry is not None and time.time() > expiry:
            # Expired - remove and return None
            del self._cache[key]
            self._misses += 1
            logger.debug(f"Cache miss (expired): {key[:16]}...")
            return None

        # Move to end (mark as recently used)
        self._cache.move_to_end(key)
        self._hits += 1
        logger.debug(f"Cache hit: {key[:16]}...")
        return value

    def set(self, key: str, value: T, ttl: int | None = None) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (None uses default_ttl)
        """
        # Determine expiry time
        effective_ttl = ttl if ttl is not None else self._default_ttl
        expiry = time.time() + effective_ttl if effective_ttl is not None else None

        # Add/update entry
        if key in self._cache:
            # Update existing entry and move to end
            self._cache[key] = (value, expiry)
            self._cache.move_to_end(key)
        else:
            # Check if we need to evict
            if len(self._cache) >= self._max_size:
                # Remove oldest (first) item
                evicted_key, _ = self._cache.popitem(last=False)
                logger.debug(f"Cache eviction (LRU): {evicted_key[:16]}...")

            self._cache[key] = (value, expiry)

        logger.debug(f"Cache set: {key[:16]}... (ttl={effective_ttl}s)")

    def clear(self) -> None:
        """Clear all cached values."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0
        logger.debug("Cache cleared")

    def size(self) -> int:
        """Return the number of items in the cache."""
        return len(self._cache)

    def stats(self) -> dict[str, int | float]:
        """Return cache statistics.

        Returns:
            Dict with hits, misses, and hit_rate
        """
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": hit_rate,
            "size": self.size(),
            "max_size": self._max_size,
        }


class DiskCache(CacheBackend[T]):
    """File-based cache with TTL support.

    Stores cache entries as pickle files in a directory.
    Each entry includes metadata (expiry time).
    """

    def __init__(self, cache_dir: str | Path, default_ttl: int | None = None):
        """Initialize disk cache.

        Args:
            cache_dir: Directory to store cache files
            default_ttl: Default TTL in seconds (None = no expiration)
        """
        self._cache_dir = Path(cache_dir).expanduser()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def _get_cache_path(self, key: str) -> Path:
        """Get the file path for a cache key."""
        return self._cache_dir / f"{key}.pkl"

    def get(self, key: str) -> T | None:
        """Retrieve a value from the cache.

        Args:
            key: Cache key

        Returns:
            Cached value if found and not expired, None otherwise
        """
        cache_file = self._get_cache_path(key)

        if not cache_file.exists():
            self._misses += 1
            return None

        try:
            with open(cache_file, "rb") as f:
                data = pickle.load(f)

            value: T = data["value"]  # Type: ignore - pickle.load returns Any
            expiry = data.get("expiry")

            # Check expiration
            if expiry is not None and time.time() > expiry:
                # Expired - remove file and return None
                cache_file.unlink(missing_ok=True)
                self._misses += 1
                logger.debug(f"Cache miss (expired): {key[:16]}...")
                return None

            self._hits += 1
            logger.debug(f"Cache hit: {key[:16]}...")
            return value

        except Exception as e:
            logger.warning(f"Failed to read cache file {cache_file}: {e}")
            cache_file.unlink(missing_ok=True)
            self._misses += 1
            return None

    def set(self, key: str, value: T, ttl: int | None = None) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (None uses default_ttl)
        """
        cache_file = self._get_cache_path(key)

        # Determine expiry time
        effective_ttl = ttl if ttl is not None else self._default_ttl
        expiry = time.time() + effective_ttl if effective_ttl is not None else None

        data = {
            "value": value,
            "expiry": expiry,
            "created_at": time.time(),
        }

        try:
            with open(cache_file, "wb") as f:
                pickle.dump(data, f)
            logger.debug(f"Cache set: {key[:16]}... (ttl={effective_ttl}s)")
        except Exception as e:
            logger.warning(f"Failed to write cache file {cache_file}: {e}")

    def clear(self) -> None:
        """Clear all cached values."""
        for cache_file in self._cache_dir.glob("*.pkl"):
            cache_file.unlink(missing_ok=True)
        self._hits = 0
        self._misses = 0
        logger.debug("Cache cleared")

    def size(self) -> int:
        """Return the number of items in the cache."""
        return len(list(self._cache_dir.glob("*.pkl")))

    def stats(self) -> dict[str, int | float]:
        """Return cache statistics.

        Returns:
            Dict with hits, misses, and hit_rate
        """
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": hit_rate,
            "size": self.size(),
        }


class NoOpCache(CacheBackend[T]):
    """No-op cache backend (caching disabled).

    Always returns None on get() and does nothing on set().
    Useful for disabling caching without code changes.
    """

    def get(self, key: str) -> T | None:  # noqa: ARG002
        """Always returns None."""
        return None

    def set(self, key: str, value: T, ttl: int | None = None) -> None:  # noqa: ARG002
        """Does nothing."""
        pass

    def clear(self) -> None:
        """Does nothing."""
        pass

    def size(self) -> int:
        """Always returns 0."""
        return 0


def hash_text(text: str) -> str:
    """Create a stable hash of text for cache keys.

    Args:
        text: Text to hash

    Returns:
        SHA256 hex digest
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_list(items: list[str]) -> str:
    """Create a stable hash of a list of strings.

    Args:
        items: List of strings to hash

    Returns:
        SHA256 hex digest of JSON-serialized list
    """
    serialized = json.dumps(items, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def hash_dict(data: dict[str, Any]) -> str:
    """Create a stable hash of a dictionary.

    Args:
        data: Dictionary to hash

    Returns:
        SHA256 hex digest of JSON-serialized dict
    """
    serialized = json.dumps(data, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
