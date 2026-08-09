"""Text embedding generation via Ollama, with caching, batching, and GPU-aware sizing.

This module turns text into fixed-dimension embedding vectors by calling an
Ollama embeddings endpoint. It provides:

- A disk/memory cache keyed on model + text so repeated ingestion/queries
  avoid redundant embedding calls.
- Synchronous and async batch embedding, with automatic batching of large
  text lists into model-sized chunks.
- Optional GPU detection to adapt batch size to available accelerator memory.

The resulting vectors are consumed by `nyxgpt.rag.vectorstore_cassandra` for
upsert/query against the vector store, so the `dimension` configured here
must match the vector column dimension used by that store.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice

from nyxgpt.cache import CacheBackend, DiskCache, MemoryCache, NoOpCache, hash_text
from nyxgpt.config import get_default_model, get_ollama_base_url, load_config
from nyxgpt.tracing import traced_span

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingConfig:
    """Resolved configuration for talking to the embedding backend.

    Attributes:
        base_url: Base URL of the Ollama server exposing the embeddings API.
        model: Name of the embedding model to invoke.
        dimension: Expected length of each embedding vector; must match the
            vector store's configured dimension.
        timeout: Per-request timeout in seconds.
        batch_size: Default number of texts sent per embedding request.
        enable_async: Whether async (concurrent) embedding is allowed.
        max_workers: Maximum thread/task concurrency for parallel embedding.
        enable_gpu: Whether to detect and account for GPU memory when sizing
            batches.
        adaptive_batching: Whether to dynamically adjust batch size based on
            detected GPU memory instead of using a fixed `batch_size`.
    """

    base_url: str
    model: str
    dimension: int
    timeout: int
    batch_size: int
    enable_async: bool = False
    max_workers: int = 4
    enable_gpu: bool = False
    adaptive_batching: bool = False


@dataclass
class EmbeddingDebugMetrics:
    """Debug metrics for embedding operations."""

    embedding_model: str
    embedding_dim: int
    num_texts_embedded: int
    batch_size: int
    embedding_time_ms: float
    cache_hits: int = 0
    cache_misses: int = 0
    gpu_available: bool = False
    batches_processed: int = 0
    parallel_workers: int = 1


@dataclass
class GPUInfo:
    """GPU availability and utilization information."""

    available: bool = False
    device_count: int = 0
    memory_total: int = 0
    memory_used: int = 0
    utilization: float = 0.0


class EmbeddingError(RuntimeError):
    """Raised when embedding generation fails (transport, HTTP, or response-shape errors)."""


# Global embedding cache instance (initialized lazily)
_embedding_cache: CacheBackend[list[list[float]]] | None = None

# Global thread pool for async operations (initialized lazily)
_thread_pool: concurrent.futures.ThreadPoolExecutor | None = None

# Global GPU info cache (updated periodically). `_gpu_info_lock` guards the
# check-then-act read/recompute/write sequence in `_detect_gpu()` so concurrent
# callers (e.g. parallel embedding requests) can't race on these globals.
_gpu_info: GPUInfo | None = None
_gpu_info_updated: float = 0.0
_gpu_info_lock = threading.Lock()
_GPU_INFO_TTL = 60.0  # Cache GPU info for 60 seconds


def _cleanup_thread_pool() -> None:
    """Clean up global thread pool on shutdown."""
    global _thread_pool
    if _thread_pool is not None:
        _thread_pool.shutdown(wait=True)
        _thread_pool = None


# Register cleanup handler to ensure thread pool is properly shut down
atexit.register(_cleanup_thread_pool)


def _embedding_cfg(model: str | None = None, dimension: int | None = None) -> EmbeddingConfig:
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
        model = cfg.get("rag", "embedding_model", fallback="").strip() or get_default_model(cfg)

    # Default dimension must match Cassandra schema; override in config if needed.
    if dimension is None:
        dimension = cfg.getint("rag", "embedding_dim", fallback=768)

    timeout = cfg.getint("rag", "embedding_timeout_seconds", fallback=120)
    batch_size = cfg.getint("rag", "embedding_batch_size", fallback=16)

    # Performance optimizations
    enable_async = cfg.getboolean("rag", "embedding_async_enabled", fallback=False)
    max_workers = cfg.getint("rag", "embedding_max_workers", fallback=4)
    enable_gpu = cfg.getboolean("rag", "embedding_gpu_enabled", fallback=False)
    adaptive_batching = cfg.getboolean("rag", "embedding_adaptive_batching", fallback=False)

    return EmbeddingConfig(
        base_url=base_url,
        model=model,
        dimension=int(dimension),
        timeout=int(timeout),
        batch_size=int(batch_size),
        enable_async=enable_async,
        max_workers=max_workers,
        enable_gpu=enable_gpu,
        adaptive_batching=adaptive_batching,
    )


def _get_embedding_cache() -> CacheBackend[list[list[float]]]:
    """Get or initialize the global embedding cache.

    Returns:
        Initialized cache backend based on config settings
    """
    global _embedding_cache

    if _embedding_cache is not None:
        return _embedding_cache

    # Load config to determine cache settings
    cfg = load_config(None)

    # Check if embedding cache is enabled
    cache_enabled = cfg.getboolean("cache", "embedding_cache_enabled", fallback=False)

    if not cache_enabled:
        logger.debug("Embedding cache disabled")
        _embedding_cache = NoOpCache()
        return _embedding_cache

    # Get cache backend type
    cache_backend = cfg.get("cache", "embedding_cache_backend", fallback="memory").lower()

    if cache_backend == "memory":
        max_size = cfg.getint("cache", "embedding_cache_max_size", fallback=1000)
        ttl = cfg.getint("cache", "embedding_cache_ttl_seconds", fallback=3600)
        _embedding_cache = MemoryCache(max_size=max_size, default_ttl=ttl, name="embedding")
        logger.debug(f"Embedding cache initialized: memory (max_size={max_size}, ttl={ttl}s)")

    elif cache_backend == "disk":
        cache_dir = cfg.get("cache", "embedding_cache_dir", fallback="~/.nyxGPT/cache/embeddings")
        ttl = cfg.getint("cache", "embedding_cache_ttl_seconds", fallback=86400)
        _embedding_cache = DiskCache(cache_dir=cache_dir, default_ttl=ttl, name="embedding")
        logger.debug(f"Embedding cache initialized: disk (dir={cache_dir}, ttl={ttl}s)")

    else:
        logger.warning(f"Unknown cache backend '{cache_backend}', disabling cache")
        _embedding_cache = NoOpCache()

    return _embedding_cache


def clear_embedding_cache() -> None:
    """Clear the global embedding cache.

    This is useful for testing or when you want to force fresh embeddings.
    """
    global _embedding_cache
    if _embedding_cache is not None:
        _embedding_cache.clear()
        logger.info("Embedding cache cleared")


def _detect_gpu() -> GPUInfo:
    """Detect GPU availability and utilization.

    Returns:
        GPUInfo with GPU status and metrics
    """
    global _gpu_info, _gpu_info_updated

    # Hold the lock across the whole check-then-act sequence: without it, two
    # concurrent callers can both observe a cold/expired cache, both shell out
    # to nvidia-smi, and race writing `_gpu_info`/`_gpu_info_updated`.
    with _gpu_info_lock:
        # Return cached info if still valid
        now = time.time()
        if _gpu_info is not None and (now - _gpu_info_updated) < _GPU_INFO_TTL:
            return _gpu_info

        # Try to detect NVIDIA GPU via nvidia-smi
        try:
            import subprocess

            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total,memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                shell=False,
            )

            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                gpu_count = len(lines)

                # Parse first GPU metrics
                if gpu_count > 0:
                    parts = lines[0].split(",")
                    if len(parts) >= 3:
                        memory_total = int(float(parts[0].strip()))
                        memory_used = int(float(parts[1].strip()))
                        utilization = float(parts[2].strip())

                        _gpu_info = GPUInfo(
                            available=True,
                            device_count=gpu_count,
                            memory_total=memory_total,
                            memory_used=memory_used,
                            utilization=utilization,
                        )
                        _gpu_info_updated = now
                        logger.debug(
                            f"GPU detected: {gpu_count} device(s), {utilization}% utilization"
                        )
                        return _gpu_info

        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, Exception) as e:
            logger.debug(f"GPU detection failed: {e}")

        # No GPU or detection failed
        _gpu_info = GPUInfo(available=False)
        _gpu_info_updated = now
        return _gpu_info


def _get_thread_pool(max_workers: int) -> concurrent.futures.ThreadPoolExecutor:
    """Get or initialize the global thread pool.

    Args:
        max_workers: Maximum number of worker threads

    Returns:
        Initialized ThreadPoolExecutor
    """
    global _thread_pool

    if _thread_pool is None:
        _thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        logger.debug(f"Thread pool initialized with {max_workers} workers")

    return _thread_pool


def _estimate_memory_usage(batch_size: int, dimension: int) -> int:
    """Estimate memory usage in bytes for a batch of embeddings.

    Args:
        batch_size: Number of texts in the batch
        dimension: Embedding dimension

    Returns:
        Estimated memory usage in bytes
    """
    # Each float is 8 bytes, plus overhead for lists and objects
    embedding_size = dimension * 8
    total_embeddings = batch_size * embedding_size
    overhead = batch_size * 100  # Rough estimate for Python object overhead
    return total_embeddings + overhead


def _get_optimal_batch_size(num_texts: int, config: EmbeddingConfig) -> int:  # noqa: ARG001
    """Determine optimal batch size based on available resources.

    Args:
        num_texts: Total number of texts to embed
        config: Embedding configuration

    Returns:
        Optimal batch size
    """
    if not config.adaptive_batching:
        return config.batch_size

    # Get available memory
    try:
        import psutil

        available_memory = psutil.virtual_memory().available
    except ImportError:
        logger.debug("psutil not available, using default batch size")
        return config.batch_size

    # Estimate memory per batch
    memory_per_batch = _estimate_memory_usage(config.batch_size, config.dimension)

    # Reserve 20% of available memory for other operations
    usable_memory = int(available_memory * 0.8)

    # Calculate max batch size that fits in memory
    max_safe_batch = max(1, usable_memory // memory_per_batch)

    # Use smaller of configured batch size and memory-safe batch size
    optimal_batch = min(config.batch_size, max_safe_batch)

    # Adjust based on GPU availability
    if config.enable_gpu:
        gpu_info = _detect_gpu()
        if gpu_info.available and gpu_info.memory_total > 0:
            # If GPU is available, we can use larger batches
            # Scale up based on GPU memory
            gpu_memory_mb = gpu_info.memory_total - gpu_info.memory_used
            if gpu_memory_mb > 4096:  # > 4GB free
                optimal_batch = min(optimal_batch * 2, 128)
            elif gpu_memory_mb > 2048:  # > 2GB free
                optimal_batch = int(min(optimal_batch * 1.5, 64))

    logger.debug(f"Adaptive batching: {optimal_batch} (from {config.batch_size})")
    return int(optimal_batch)


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    """POST a JSON payload to the Ollama embeddings endpoint and parse the response.

    Args:
        url: Full URL of the Ollama embeddings endpoint.
        payload: JSON-serializable request body (e.g. model name and input texts).
        timeout: Request timeout in seconds.

    Returns:
        The decoded JSON response body, or an empty dict if the response was empty.

    Raises:
        EmbeddingError: If the HTTP request fails or the server is unreachable.
    """
    with traced_span("ollama.embeddings", url=url):
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
            msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            raise EmbeddingError(f"HTTP error calling {url}: {e.code} {msg}") from e
        except urllib.error.URLError as e:
            raise EmbeddingError(f"Failed to reach Ollama at {url}: {e}") from e


def _batched(iterable, size):
    """Yield successive lists of up to `size` items from `iterable`.

    Args:
        iterable: Source iterable to split into batches.
        size: Maximum number of items per yielded batch.

    Yields:
        Lists of items, each of length `size` except possibly the last.
    """
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def _embed_batch_sync(
    batch: list[str], url: str, model: str, timeout: int, dimension: int
) -> list[list[float]]:
    """Synchronously embed a single batch of texts.

    Args:
        batch: List of texts to embed
        url: Ollama API endpoint URL
        model: Embedding model name
        timeout: Request timeout in seconds
        dimension: Expected embedding dimension

    Returns:
        List of embedding vectors

    Raises:
        EmbeddingError: If embedding fails
    """
    data = _post_json(url, {"model": model, "input": batch}, timeout=timeout)

    if "embeddings" in data:
        vectors = data["embeddings"]
    elif "embedding" in data:
        vectors = [data["embedding"]]
    else:
        raise EmbeddingError(f"Unexpected Ollama embed response keys: {list(data.keys())}")

    result = []
    for v in vectors:
        if not isinstance(v, list):
            raise EmbeddingError("Embedding is not a list")
        if len(v) != dimension:
            raise EmbeddingError(
                f"Embedding has dim {len(v)} but expected {dimension}. "
                f"Update collection dimension to match model output. "
                f"Use --collection flag to specify a different collection."
            )
        result.append([float(x) for x in v])

    return result


async def _embed_batch_async(
    batch: list[str],
    url: str,
    model: str,
    timeout: int,
    dimension: int,
    executor: concurrent.futures.ThreadPoolExecutor,
) -> list[list[float]]:
    """Asynchronously embed a single batch of texts.

    Args:
        batch: List of texts to embed
        url: Ollama API endpoint URL
        model: Embedding model name
        timeout: Request timeout in seconds
        dimension: Expected embedding dimension
        executor: Thread pool executor for async operations

    Returns:
        List of embedding vectors
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return await loop.run_in_executor(
        executor, _embed_batch_sync, batch, url, model, timeout, dimension
    )


async def _embed_texts_parallel(
    batches: list[list[str]],
    url: str,
    model: str,
    timeout: int,
    dimension: int,
    executor: concurrent.futures.ThreadPoolExecutor,
) -> list[list[float]]:
    """Embed multiple batches in parallel using async execution.

    Args:
        batches: List of text batches to embed
        url: Ollama API endpoint URL
        model: Embedding model name
        timeout: Request timeout in seconds
        dimension: Expected embedding dimension
        executor: Thread pool executor for async operations

    Returns:
        Flattened list of all embedding vectors
    """
    tasks = [
        _embed_batch_async(batch, url, model, timeout, dimension, executor) for batch in batches
    ]

    batch_results = await asyncio.gather(*tasks)

    # Flatten results
    result = []
    for batch_vecs in batch_results:
        result.extend(batch_vecs)

    return result


def embed_texts(
    texts: Iterable[str],
    *,
    collect_metrics: bool = False,
    model: str | None = None,
    dimension: int | None = None,
) -> list[list[float]] | tuple[list[list[float]], EmbeddingDebugMetrics]:
    """Embed a batch of texts using Ollama with caching and performance optimizations.

    Uses the `/api/embed` endpoint with automatic caching of embeddings.
    Cache is keyed by (text, model, dimension) to ensure correctness.

    Performance optimizations:
      - Async/parallel processing for improved throughput
      - GPU utilization detection and optimization
      - Adaptive batch sizing based on available memory
      - Connection pooling for reduced latency

    Multi-model support:
      - Pass `model` to use a specific embedding model
      - Pass `dimension` to validate the expected output dimension
      - If not specified, uses config defaults

    Config:
      - `[ollama] base_url`
      - `[rag] embedding_model` (optional, can be overridden)
      - `[rag] embedding_dim` (can be overridden)
      - `[rag] embedding_async_enabled` (enable async processing)
      - `[rag] embedding_max_workers` (parallel worker threads)
      - `[rag] embedding_gpu_enabled` (enable GPU optimization)
      - `[rag] embedding_adaptive_batching` (enable adaptive batch sizing)
      - `[cache] embedding_cache_enabled` (enable/disable caching)
      - `[cache] embedding_cache_backend` (memory or disk)
      - `[cache] embedding_cache_ttl_seconds` (cache expiration time)

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
                cache_hits=0,
                cache_misses=0,
            )
            return [], metrics
        return []

    start_time = time.perf_counter()
    ecfg = _embedding_cfg(model=model, dimension=dimension)
    cache = _get_embedding_cache()

    # Detect GPU if enabled
    gpu_info = _detect_gpu() if ecfg.enable_gpu else GPUInfo(available=False)

    # Try to retrieve from cache first
    # Create cache key from texts + model + dimension
    cache_key_data = {
        "texts": texts_list,
        "model": ecfg.model,
        "dimension": ecfg.dimension,
    }
    cache_key = hash_text(json.dumps(cache_key_data, sort_keys=True))

    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.debug(f"Embedding cache hit for {len(texts_list)} texts")
        if collect_metrics:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            metrics = EmbeddingDebugMetrics(
                embedding_model=ecfg.model,
                embedding_dim=ecfg.dimension,
                num_texts_embedded=len(texts_list),
                batch_size=ecfg.batch_size,
                embedding_time_ms=elapsed_ms,
                cache_hits=1,
                cache_misses=0,
                gpu_available=gpu_info.available,
            )
            return cached_result, metrics
        return cached_result

    # Cache miss - compute embeddings
    logger.debug(f"Embedding cache miss for {len(texts_list)} texts, computing...")
    url = f"{ecfg.base_url}/api/embed"

    # Determine optimal batch size
    optimal_batch_size = _get_optimal_batch_size(len(texts_list), ecfg)

    # Create batches
    batches = list(_batched(texts_list, optimal_batch_size))
    num_batches = len(batches)

    # Process batches (async or sync)
    if ecfg.enable_async and num_batches > 1:
        # Async processing for multiple batches
        executor = _get_thread_pool(ecfg.max_workers)
        try:
            # Run async processing
            out = asyncio.run(
                _embed_texts_parallel(
                    batches, url, ecfg.model, ecfg.timeout, ecfg.dimension, executor
                )
            )
        except RuntimeError as e:
            # If event loop is already running, fall back to sync
            logger.debug(f"Async processing failed, falling back to sync: {e}")
            out = []
            for batch in batches:
                batch_vecs = _embed_batch_sync(batch, url, ecfg.model, ecfg.timeout, ecfg.dimension)
                out.extend(batch_vecs)
    else:
        # Synchronous processing
        out = []
        for batch in batches:
            batch_vecs = _embed_batch_sync(batch, url, ecfg.model, ecfg.timeout, ecfg.dimension)
            out.extend(batch_vecs)

    # Store in cache
    cache.set(cache_key, out)

    if collect_metrics:
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        metrics = EmbeddingDebugMetrics(
            embedding_model=ecfg.model,
            embedding_dim=ecfg.dimension,
            num_texts_embedded=len(texts_list),
            batch_size=optimal_batch_size,
            embedding_time_ms=elapsed_ms,
            cache_hits=0,
            cache_misses=1,
            gpu_available=gpu_info.available,
            batches_processed=num_batches,
            parallel_workers=ecfg.max_workers if ecfg.enable_async else 1,
        )
        return out, metrics

    return out


def embed_text(text: str, *, model: str | None = None, dimension: int | None = None) -> list[float]:
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
