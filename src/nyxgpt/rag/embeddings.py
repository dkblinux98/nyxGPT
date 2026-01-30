from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable
from itertools import islice

from nyxgpt.config import get_default_model, get_ollama_base_url, load_config
from nyxgpt.cache import get_cached_embedding, cache_embedding

logger = logging.getLogger(__name__)


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
    """Embed a batch of texts using Ollama with caching.

    Uses the `/api/embed` endpoint. Caches embeddings by (text, model, dimension)
    to avoid recomputing identical embeddings.

    Multi-model support:
      - Pass `model` to use a specific embedding model
      - Pass `dimension` to validate the expected output dimension
      - If not specified, uses config defaults

    Config:
      - `[ollama] base_url`
      - `[rag] embedding_model` (optional, can be overridden)
      - `[rag] embedding_dim` (can be overridden)
      - `[cache] embedding_cache_enabled` (default: true)
      - `[cache] embedding_cache_ttl_seconds` (default: 0 = no expiration)

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
    url = f"{ecfg.base_url}/api/embed"

    # Try to get cached embeddings first
    out: list[list[float]] = []
    uncached_texts: list[str] = []
    uncached_indices: list[int] = []

    for i, text in enumerate(texts_list):
        cached = get_cached_embedding(text, ecfg.model, ecfg.dimension)
        if cached is not None:
            out.append(cached)
        else:
            uncached_texts.append(text)
            uncached_indices.append(i)
            out.append([])  # Placeholder

    cache_hits = len(texts_list) - len(uncached_texts)
    if cache_hits > 0:
        logger.debug(
            "Embedding cache: %d hits, %d misses", cache_hits, len(uncached_texts)
        )

    # Fetch and cache only the uncached texts
    if uncached_texts:
        for batch in _batched(uncached_texts, ecfg.batch_size):
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

                embedding = [float(x) for x in v]

                # Find the original index for this embedding
                batch_start_idx = uncached_texts.index(batch[i])
                original_idx = uncached_indices[batch_start_idx]

                # Store in output at correct position
                out[original_idx] = embedding

                # Cache the embedding
                cache_embedding(batch[i], ecfg.model, ecfg.dimension, embedding)

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
