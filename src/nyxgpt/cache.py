"""Caching utilities for embeddings and responses.

Provides configurable memory and disk caching with TTL support.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable

from nyxgpt.config import load_config

logger = logging.getLogger(__name__)

# Lazy-loaded cache instances
_embedding_cache: Any | None = None
_response_cache: Any | None = None


def _get_cache_dir() -> Path:
    """Get the configured cache directory."""
    cfg = load_config(None)
    cache_dir_str = cfg.get("nyxgpt", "cache_dir", fallback="~/.nyxGPT/cache")
    cache_dir = Path(cache_dir_str).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_embedding_cache():
    """Get or create the embedding cache instance."""
    global _embedding_cache

    if _embedding_cache is not None:
        return _embedding_cache

    cfg = load_config(None)
    enabled = cfg.getboolean("cache", "embedding_cache_enabled", fallback=True)

    if not enabled:
        logger.debug("Embedding cache is disabled")
        return None

    try:
        import diskcache
    except ImportError:
        logger.warning("diskcache not installed, embedding caching disabled")
        return None

    cache_dir = _get_cache_dir() / "embeddings"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Get TTL from config (None = no expiration)
    ttl = cfg.getint("cache", "embedding_cache_ttl_seconds", fallback=0)
    ttl = None if ttl == 0 else ttl

    _embedding_cache = diskcache.Cache(
        str(cache_dir),
        size_limit=1024 * 1024 * 1024,  # 1GB max
    )

    logger.info("Embedding cache initialized at %s (TTL: %s)", cache_dir, ttl or "none")
    return _embedding_cache


def _get_response_cache():
    """Get or create the response cache instance."""
    global _response_cache

    if _response_cache is not None:
        return _response_cache

    cfg = load_config(None)
    enabled = cfg.getboolean("cache", "response_cache_enabled", fallback=False)

    if not enabled:
        logger.debug("Response cache is disabled")
        return None

    try:
        import diskcache
    except ImportError:
        logger.warning("diskcache not installed, response caching disabled")
        return None

    cache_dir = _get_cache_dir() / "responses"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Get TTL from config (default: 1 hour)
    ttl = cfg.getint("cache", "response_cache_ttl_seconds", fallback=3600)
    ttl = None if ttl == 0 else ttl

    _response_cache = diskcache.Cache(
        str(cache_dir),
        size_limit=512 * 1024 * 1024,  # 512MB max
    )

    logger.info("Response cache initialized at %s (TTL: %s)", cache_dir, ttl or "none")
    return _response_cache


def make_embedding_cache_key(text: str, model: str, dimension: int) -> str:
    """Create a cache key for an embedding.

    Args:
        text: Text to embed
        model: Model name
        dimension: Expected embedding dimension

    Returns:
        Hash string suitable for use as a cache key
    """
    content = f"{text}|{model}|{dimension}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_cached_embedding(text: str, model: str, dimension: int) -> list[float] | None:
    """Get a cached embedding if available.

    Args:
        text: Text that was embedded
        model: Model used for embedding
        dimension: Expected embedding dimension

    Returns:
        Cached embedding vector or None if not cached
    """
    cache = _get_embedding_cache()
    if cache is None:
        return None

    key = make_embedding_cache_key(text, model, dimension)

    cfg = load_config(None)
    ttl = cfg.getint("cache", "embedding_cache_ttl_seconds", fallback=0)
    ttl = None if ttl == 0 else ttl

    try:
        if ttl is not None:
            result = cache.get(key, default=None, expire_time=True)
            if result is not None:
                value, expire = result
                logger.debug("Embedding cache hit for key %s", key[:16])
                return value
        else:
            result = cache.get(key, default=None)
            if result is not None:
                logger.debug("Embedding cache hit for key %s", key[:16])
                return result
    except Exception as e:
        logger.warning("Error reading from embedding cache: %s", e)

    logger.debug("Embedding cache miss for key %s", key[:16])
    return None


def cache_embedding(text: str, model: str, dimension: int, embedding: list[float]) -> None:
    """Cache an embedding.

    Args:
        text: Text that was embedded
        model: Model used for embedding
        dimension: Embedding dimension
        embedding: Embedding vector to cache
    """
    cache = _get_embedding_cache()
    if cache is None:
        return

    key = make_embedding_cache_key(text, model, dimension)

    cfg = load_config(None)
    ttl = cfg.getint("cache", "embedding_cache_ttl_seconds", fallback=0)
    ttl = None if ttl == 0 else ttl

    try:
        cache.set(key, embedding, expire=ttl)
        logger.debug("Cached embedding for key %s (TTL: %s)", key[:16], ttl or "none")
    except Exception as e:
        logger.warning("Error writing to embedding cache: %s", e)


def make_response_cache_key(
    messages: list[dict[str, str]],
    model: str,
    session: str | None = None,
) -> str:
    """Create a cache key for a chat response.

    Args:
        messages: Message history (list of {role, content} dicts)
        model: Model used for generation
        session: Optional session name (for session-scoped caching)

    Returns:
        Hash string suitable for use as a cache key
    """
    # Serialize messages to a stable string representation
    msg_str = "|".join(f"{m.get('role', '')}:{m.get('content', '')}" for m in messages)

    cfg = load_config(None)
    scope = cfg.get("cache", "response_cache_scope", fallback="global")

    if scope == "session" and session:
        content = f"{session}|{model}|{msg_str}"
    else:
        content = f"{model}|{msg_str}"

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_cached_response(
    messages: list[dict[str, str]],
    model: str,
    session: str | None = None,
) -> str | None:
    """Get a cached chat response if available.

    Args:
        messages: Message history
        model: Model name
        session: Optional session name

    Returns:
        Cached response text or None if not cached
    """
    cache = _get_response_cache()
    if cache is None:
        return None

    key = make_response_cache_key(messages, model, session)

    cfg = load_config(None)
    ttl = cfg.getint("cache", "response_cache_ttl_seconds", fallback=3600)
    ttl = None if ttl == 0 else ttl

    try:
        if ttl is not None:
            result = cache.get(key, default=None, expire_time=True)
            if result is not None:
                value, expire = result
                logger.debug("Response cache hit for key %s", key[:16])
                return value
        else:
            result = cache.get(key, default=None)
            if result is not None:
                logger.debug("Response cache hit for key %s", key[:16])
                return result
    except Exception as e:
        logger.warning("Error reading from response cache: %s", e)

    logger.debug("Response cache miss for key %s", key[:16])
    return None


def cache_response(
    messages: list[dict[str, str]],
    model: str,
    response: str,
    session: str | None = None,
) -> None:
    """Cache a chat response.

    Args:
        messages: Message history
        model: Model used
        response: Generated response text
        session: Optional session name
    """
    cache = _get_response_cache()
    if cache is None:
        return

    key = make_response_cache_key(messages, model, session)

    cfg = load_config(None)
    ttl = cfg.getint("cache", "response_cache_ttl_seconds", fallback=3600)
    ttl = None if ttl == 0 else ttl

    try:
        cache.set(key, response, expire=ttl)
        logger.debug("Cached response for key %s (TTL: %s)", key[:16], ttl or "none")
    except Exception as e:
        logger.warning("Error writing to response cache: %s", e)


def clear_embedding_cache() -> None:
    """Clear all cached embeddings."""
    cache = _get_embedding_cache()
    if cache is not None:
        try:
            cache.clear()
            logger.info("Embedding cache cleared")
        except Exception as e:
            logger.warning("Error clearing embedding cache: %s", e)


def clear_response_cache() -> None:
    """Clear all cached responses."""
    cache = _get_response_cache()
    if cache is not None:
        try:
            cache.clear()
            logger.info("Response cache cleared")
        except Exception as e:
            logger.warning("Error clearing response cache: %s", e)
