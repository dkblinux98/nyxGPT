from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable
from itertools import islice

from nyxgpt.config import get_default_model, get_ollama_base_url, load_config


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dimension: int
    timeout: int
    batch_size: int


@dataclass
class EmbeddingDebugMetrics:
    """Debug metrics for embedding operations."""

    embedding_model: str
    embedding_dim: int
    num_texts_embedded: int
    batch_size: int
    embedding_time_ms: float


class EmbeddingError(RuntimeError):
    pass


@dataclass
class CacheEntry:
    """Cache entry with TTL support."""

    embedding: list[float]
    timestamp: float


class EmbeddingCache:
    """Thread-safe LRU cache for embeddings with TTL support."""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 3600):
        """Initialize embedding cache.

        Args:
            max_size: Maximum number of cached embeddings (LRU eviction)
            ttl_seconds: Time-to-live for cache entries in seconds
        """
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, CacheEntry] = {}
        self._access_order: list[str] = []  # For LRU tracking

    def _compute_hash(self, text: str, model: str, dimension: int) -> str:
        """Compute cache key hash from text and model parameters.

        Args:
            text: Text to embed
            model: Embedding model name
            dimension: Embedding dimension

        Returns:
            SHA256 hash of the cache key
        """
        key = f"{text}::{model}::{dimension}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def get(self, text: str, model: str, dimension: int) -> list[float] | None:
        """Get cached embedding if available and not expired.

        Args:
            text: Text to look up
            model: Model used for embedding
            dimension: Expected dimension

        Returns:
            Cached embedding vector or None if not found/expired
        """
        cache_key = self._compute_hash(text, model, dimension)
        entry = self._cache.get(cache_key)

        if entry is None:
            return None

        # Check TTL
        if self.ttl_seconds > 0:
            age = time.time() - entry.timestamp
            if age > self.ttl_seconds:
                # Expired, remove from cache
                del self._cache[cache_key]
                if cache_key in self._access_order:
                    self._access_order.remove(cache_key)
                return None

        # Update access order for LRU
        if cache_key in self._access_order:
            self._access_order.remove(cache_key)
        self._access_order.append(cache_key)

        return entry.embedding

    def put(self, text: str, model: str, dimension: int, embedding: list[float]) -> None:
        """Store embedding in cache.

        Args:
            text: Text that was embedded
            model: Model used for embedding
            dimension: Embedding dimension
            embedding: Embedding vector to cache
        """
        cache_key = self._compute_hash(text, model, dimension)

        # Evict oldest entry if cache is full
        if len(self._cache) >= self.max_size and cache_key not in self._cache:
            if self._access_order:
                oldest_key = self._access_order.pop(0)
                del self._cache[oldest_key]

        # Store new entry
        self._cache[cache_key] = CacheEntry(
            embedding=embedding, timestamp=time.time()
        )

        # Update access order
        if cache_key in self._access_order:
            self._access_order.remove(cache_key)
        self._access_order.append(cache_key)

    def clear(self) -> None:
        """Clear all cached embeddings."""
        self._cache.clear()
        self._access_order.clear()

    def size(self) -> int:
        """Get current cache size."""
        return len(self._cache)


# Global embedding cache instance
_embedding_cache: EmbeddingCache | None = None


def _get_embedding_cache() -> EmbeddingCache | None:
    """Get or create the global embedding cache instance.

    Returns None if caching is disabled in config.
    """
    global _embedding_cache

    cfg = load_config(None)

    # Check if embedding cache is enabled
    try:
        cache_enabled = cfg.getboolean("cache", "embedding_cache_enabled", fallback=True)
    except Exception:
        cache_enabled = True

    if not cache_enabled:
        return None

    # Create cache if it doesn't exist
    if _embedding_cache is None:
        try:
            max_size = cfg.getint("cache", "embedding_cache_max_size", fallback=1000)
            ttl_seconds = cfg.getint("cache", "embedding_cache_ttl_seconds", fallback=3600)
        except Exception:
            max_size = 1000
            ttl_seconds = 3600

        _embedding_cache = EmbeddingCache(max_size=max_size, ttl_seconds=ttl_seconds)

    return _embedding_cache


def _embedding_cfg(
    model: str | None = None, dimension: int | None = None
) -> EmbeddingConfig:
    """Get embedding configuration.

    Args:
        model: Override embedding model (default: from config)
        dimension: Override embedding dimension (default: from config)

    Returns:
        EmbeddingConfig with model, dimension, and connection settings
    """
    cfg = load_config(None)
    base_url = get_ollama_base_url(cfg).rstrip("/")

    # Allow dedicated embedding model override; otherwise fall back to default_model.
    # [rag] embedding_model = ...
    if model is None:
        model = cfg.get(
            "rag", "embedding_model", fallback=""
        ).strip() or get_default_model(cfg)

    # Default dimension must match Cassandra schema; override in config if needed.
    if dimension is None:
        dimension = cfg.getint("rag", "embedding_dim", fallback=768)

    timeout = cfg.getint("rag", "embedding_timeout_seconds", fallback=120)
    batch_size = cfg.getint("rag", "embedding_batch_size", fallback=16)

    return EmbeddingConfig(
        base_url=base_url,
        model=model,
        dimension=int(dimension),
        timeout=int(timeout),
        batch_size=int(batch_size),
    )


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = (
            e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        )
        raise EmbeddingError(f"HTTP error calling {url}: {e.code} {msg}")
    except urllib.error.URLError as e:
        raise EmbeddingError(f"Failed to reach Ollama at {url}: {e}")


def _batched(iterable, size):
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def embed_texts(
    texts: Iterable[str],
    *,
    collect_metrics: bool = False,
    model: str | None = None,
    dimension: int | None = None,
) -> list[list[float]] | tuple[list[list[float]], EmbeddingDebugMetrics]:
    """Embed a batch of texts using Ollama with caching support.

    Uses the `/api/embed` endpoint with automatic caching of results.
    Cache is keyed by (text, model, dimension) and respects TTL settings.

    Multi-model support:
      - Pass `model` to use a specific embedding model
      - Pass `dimension` to validate the expected output dimension
      - If not specified, uses config defaults

    Config:
      - `[ollama] base_url`
      - `[rag] embedding_model` (optional, can be overridden)
      - `[rag] embedding_dim` (can be overridden)
      - `[cache] embedding_cache_enabled` (default: true)
      - `[cache] embedding_cache_max_size` (default: 1000)
      - `[cache] embedding_cache_ttl_seconds` (default: 3600)

    Args:
        texts: Iterable of texts to embed
        collect_metrics: If True, return tuple of (embeddings, metrics)
        model: Override embedding model (default: from config)
        dimension: Override expected dimension (default: from config)

    Returns:
        list of float vectors, one per input text.
        If collect_metrics=True, returns tuple of (embeddings, EmbeddingDebugMetrics).
    """

    texts_list = [t if isinstance(t, str) else str(t) for t in texts]
    if not texts_list:
        if collect_metrics:
            ecfg = _embedding_cfg(model=model, dimension=dimension)
            metrics = EmbeddingDebugMetrics(
                embedding_model=ecfg.model,
                embedding_dim=ecfg.dimension,
                num_texts_embedded=0,
                batch_size=ecfg.batch_size,
                embedding_time_ms=0.0,
            )
            return [], metrics
        return []

    start_time = time.perf_counter()
    ecfg = _embedding_cfg(model=model, dimension=dimension)
    cache = _get_embedding_cache()

    # Try to retrieve from cache
    out: list[list[float]] = []
    texts_to_embed: list[str] = []
    cache_indices: dict[int, int] = {}  # Maps output index to texts_to_embed index

    for i, text in enumerate(texts_list):
        if cache is not None:
            cached_embedding = cache.get(text, ecfg.model, ecfg.dimension)
            if cached_embedding is not None:
                out.append(cached_embedding)
                continue

        # Cache miss - need to embed this text
        cache_indices[i] = len(texts_to_embed)
        texts_to_embed.append(text)
        out.append([])  # Placeholder

    # Embed uncached texts
    if texts_to_embed:
        url = f"{ecfg.base_url}/api/embed"
        newly_embedded: list[list[float]] = []

        for batch in _batched(texts_to_embed, ecfg.batch_size):
            data = _post_json(
                url,
                {"model": ecfg.model, "input": batch},
                timeout=ecfg.timeout,
            )

            if "embeddings" in data:
                vectors = data["embeddings"]
            elif "embedding" in data:
                vectors = [data["embedding"]]
            else:
                raise EmbeddingError(
                    f"Unexpected Ollama embed response keys: {list(data.keys())}"
                )

            for i, v in enumerate(vectors):
                if not isinstance(v, list):
                    raise EmbeddingError("Embedding is not a list")
                if len(v) != ecfg.dimension:
                    raise EmbeddingError(
                        f"Embedding has dim {len(v)} but expected {ecfg.dimension}. "
                        f"Update collection dimension to match model output. "
                        f"Use --collection flag to specify a different collection."
                    )
                newly_embedded.append([float(x) for x in v])

        # Store newly embedded results in output and cache
        for orig_idx, embed_idx in cache_indices.items():
            embedding = newly_embedded[embed_idx]
            out[orig_idx] = embedding
            if cache is not None:
                cache.put(texts_list[orig_idx], ecfg.model, ecfg.dimension, embedding)

    if collect_metrics:
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        metrics = EmbeddingDebugMetrics(
            embedding_model=ecfg.model,
            embedding_dim=ecfg.dimension,
            num_texts_embedded=len(texts_list),
            batch_size=ecfg.batch_size,
            embedding_time_ms=elapsed_ms,
        )
        return out, metrics

    return out


def embed_text(
    text: str, *, model: str | None = None, dimension: int | None = None
) -> list[float]:
    """Convenience wrapper for a single string.

    Args:
        text: Text to embed
        model: Override embedding model (default: from config)
        dimension: Override expected dimension (default: from config)

    Returns:
        Embedding vector
    """
    result = embed_texts([text], model=model, dimension=dimension)
    # Handle both return types: list[list[float]] or tuple with metrics
    if isinstance(result, tuple):
        vecs, _ = result
    else:
        vecs = result
    return vecs[0] if vecs else []
