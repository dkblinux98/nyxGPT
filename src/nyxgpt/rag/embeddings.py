from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Iterable
from itertools import islice

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

from nyxgpt.config import get_default_model, get_ollama_base_url, load_config


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dimension: int
    timeout: int
    batch_size: int
    max_concurrent_batches: int  # New: control concurrent batch processing
    use_async: bool  # New: enable async processing


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

    # Performance optimization settings
    max_concurrent_batches = cfg.getint("rag", "embedding_max_concurrent_batches", fallback=4)
    use_async = cfg.getboolean("rag", "embedding_use_async", fallback=True) and HTTPX_AVAILABLE

    return EmbeddingConfig(
        base_url=base_url,
        model=model,
        dimension=int(dimension),
        timeout=int(timeout),
        batch_size=int(batch_size),
        max_concurrent_batches=int(max_concurrent_batches),
        use_async=use_async,
    )


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    """Synchronous HTTP POST with urllib (fallback when httpx not available)."""
    import urllib.error
    import urllib.request

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


async def _post_json_async(
    client: "httpx.AsyncClient", url: str, payload: dict, timeout: int
) -> dict:
    """Async HTTP POST with httpx for better performance."""
    try:
        resp = await client.post(
            url,
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except httpx.HTTPStatusError as e:
        raise EmbeddingError(
            f"HTTP error calling {url}: {e.response.status_code} {e.response.text}"
        )
    except httpx.RequestError as e:
        raise EmbeddingError(f"Failed to reach Ollama at {url}: {e}")


def _batched(iterable, size):
    """Split iterable into fixed-size batches."""
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


async def _embed_batch_async(
    client: "httpx.AsyncClient",
    url: str,
    model: str,
    batch: list[str],
    timeout: int,
    expected_dim: int,
) -> list[list[float]]:
    """Embed a single batch asynchronously.

    Args:
        client: Async HTTP client (reused connection pool)
        url: Ollama embed endpoint URL
        model: Embedding model name
        batch: List of texts to embed in this batch
        timeout: Request timeout in seconds
        expected_dim: Expected vector dimension for validation

    Returns:
        List of embedding vectors
    """
    data = await _post_json_async(
        client, url, {"model": model, "input": batch}, timeout
    )

    if "embeddings" in data:
        vectors = data["embeddings"]
    elif "embedding" in data:
        vectors = [data["embedding"]]
    else:
        raise EmbeddingError(
            f"Unexpected Ollama embed response keys: {list(data.keys())}"
        )

    result = []
    for i, v in enumerate(vectors):
        if not isinstance(v, list):
            raise EmbeddingError("Embedding is not a list")
        if len(v) != expected_dim:
            raise EmbeddingError(
                f"Embedding has dim {len(v)} but expected {expected_dim}. "
                f"Update collection dimension to match model output. "
                f"Use --collection flag to specify a different collection."
            )
        result.append([float(x) for x in v])

    return result


async def _embed_texts_async(
    texts_list: list[str],
    ecfg: EmbeddingConfig,
) -> list[list[float]]:
    """Embed texts asynchronously with concurrent batch processing.

    Args:
        texts_list: List of texts to embed
        ecfg: Embedding configuration

    Returns:
        List of embedding vectors (preserves input order)
    """
    url = f"{ecfg.base_url}/api/embed"

    # Create batches
    batches = list(_batched(texts_list, ecfg.batch_size))

    # Process batches concurrently with connection pooling
    # Use limits to control max connections and connection pool size
    limits = httpx.Limits(
        max_keepalive_connections=ecfg.max_concurrent_batches,
        max_connections=ecfg.max_concurrent_batches * 2,
        keepalive_expiry=30.0,  # Keep connections alive for 30 seconds
    )

    async with httpx.AsyncClient(limits=limits) as client:
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(ecfg.max_concurrent_batches)

        async def process_batch(batch: list[str]) -> list[list[float]]:
            async with semaphore:
                return await _embed_batch_async(
                    client, url, ecfg.model, batch, ecfg.timeout, ecfg.dimension
                )

        # Execute batches concurrently while preserving order
        tasks = [process_batch(batch) for batch in batches]
        batch_results = await asyncio.gather(*tasks)

        # Flatten results (preserve order)
        out: list[list[float]] = []
        for batch_vecs in batch_results:
            out.extend(batch_vecs)

        return out


def embed_texts(
    texts: Iterable[str],
    *,
    collect_metrics: bool = False,
    model: str | None = None,
    dimension: int | None = None,
) -> list[list[float]] | tuple[list[list[float]], EmbeddingDebugMetrics]:
    """Embed a batch of texts using Ollama.

    Uses the `/api/embed` endpoint with optimized async processing when available.

    Performance Optimizations:
      - Async I/O with httpx (when available, fallback to urllib)
      - Concurrent batch processing (configurable max_concurrent_batches)
      - Connection pooling with keep-alive
      - Configurable batch size for memory management

    Multi-model support:
      - Pass `model` to use a specific embedding model
      - Pass `dimension` to validate the expected output dimension
      - If not specified, uses config defaults

    Config:
      - `[ollama] base_url`
      - `[rag] embedding_model` (optional, can be overridden)
      - `[rag] embedding_dim` (can be overridden)
      - `[rag] embedding_batch_size` (default: 16)
      - `[rag] embedding_max_concurrent_batches` (default: 4)
      - `[rag] embedding_use_async` (default: true if httpx available)

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

    # Use async implementation if available and enabled
    if ecfg.use_async:
        # Check if we're already in an event loop
        try:
            _ = asyncio.get_running_loop()
            # We're in an async context, but embed_texts is sync
            # Use asyncio.create_task() won't work here
            # Fall back to sync implementation
            use_sync_fallback = True
        except RuntimeError:
            # No running loop, we can create one
            use_sync_fallback = False

        if not use_sync_fallback:
            out = asyncio.run(_embed_texts_async(texts_list, ecfg))
        else:
            # Fallback to synchronous implementation
            out = _embed_texts_sync(texts_list, ecfg)
    else:
        # Use synchronous implementation
        out = _embed_texts_sync(texts_list, ecfg)

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


def _embed_texts_sync(texts_list: list[str], ecfg: EmbeddingConfig) -> list[list[float]]:
    """Synchronous embedding implementation (fallback when async not available)."""
    url = f"{ecfg.base_url}/api/embed"
    out: list[list[float]] = []

    for batch in _batched(texts_list, ecfg.batch_size):
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
            out.append([float(x) for x in v])

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
