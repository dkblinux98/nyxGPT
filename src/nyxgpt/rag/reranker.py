"""Cross-encoder reranking for improved retrieval quality.

This module implements reranking of search results using Ollama-based
cross-encoder scoring. Reranking improves precision by re-scoring
initial retrieval results with a more sophisticated relevance model.

**Why Reranking?**

Initial retrieval (vector/hybrid search) is fast but may not capture
subtle relevance signals. Reranking uses a cross-encoder approach to
score query-document pairs, providing more accurate relevance scores.

**Local-First Design:**

Unlike traditional cross-encoder systems that require sentence-transformers,
this implementation uses Ollama to score relevance locally, maintaining
the nyxGPT philosophy of zero external dependencies.

**Performance:**

Reranking is more expensive than initial retrieval, so it's applied
only to the top-K candidates from the first-pass search. The reranked
results are then truncated to top-N for final use.

**Configuration:**

- `enable_reranking`: Enable/disable reranking (default: false)
- `reranker_model`: Model to use for reranking (default: same as default_model)
- `rerank_top_n`: Number of results to return after reranking (default: 3)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, TypedDict

from nyxgpt.config import get_default_model, get_ollama_base_url, load_config

log = logging.getLogger(__name__)


class SearchResult(TypedDict, total=False):
    """Type definition for search result dictionaries.

    This defines the structure of search results passed to and returned
    from the reranker. Not all fields are required in input results.
    Additional fields beyond those listed here may be present.
    """

    text: str  # Document text (required for reranking)
    score: float | None  # Relevance score (updated by reranking, may be None)
    original_score: float | None  # Original score before reranking (added by reranker)


@dataclass
class RerankerConfig:
    """Configuration for reranking operations."""

    base_url: str
    model: str
    timeout: int
    top_n: int
    enabled: bool


@dataclass
class RerankerDebugMetrics:
    """Debug metrics for reranking operations."""

    reranker_model: str
    num_candidates: int
    num_reranked: int
    reranking_time_ms: float
    score_min: float | None
    score_max: float | None
    score_mean: float | None


class RerankError(RuntimeError):
    """Exception raised when reranking fails."""

    pass


def _reranker_cfg() -> RerankerConfig:
    """Load reranker configuration from config file.

    Returns:
        RerankerConfig with model, timeout, and top-N settings
    """
    cfg = load_config(None)
    base_url = get_ollama_base_url(cfg).rstrip("/")

    # Reranker can use a dedicated model or fall back to default
    model = cfg.get("rag", "reranker_model", fallback="").strip() or get_default_model(cfg)

    timeout = cfg.getint("rag", "reranker_timeout_seconds", fallback=30)
    top_n = cfg.getint("rag", "rerank_top_n", fallback=3)
    enabled = cfg.getboolean("rag", "enable_reranking", fallback=False)

    return RerankerConfig(
        base_url=base_url,
        model=model,
        timeout=timeout,
        top_n=top_n,
        enabled=enabled,
    )


def _score_relevance(query: str, document: str, config: RerankerConfig) -> float:
    """Score relevance of a document to a query using Ollama.

    Uses a prompt-based approach to score relevance from 0.0 to 1.0.

    Args:
        query: User's search query
        document: Document text to score
        config: Reranker configuration

    Returns:
        Relevance score between 0.0 (not relevant) and 1.0 (highly relevant)

    Raises:
        RerankError: If Ollama request fails
    """
    # Construct a scoring prompt
    system_prompt = (
        "You are a relevance scoring system. Given a query and a document, "
        "score how relevant the document is to the query. "
        "Return ONLY a JSON object with a single 'score' field (float between 0.0 and 1.0). "
        "0.0 means completely irrelevant, 1.0 means highly relevant. "
        "Do not include any explanation, only the JSON."
    )

    user_prompt = f"Query: {query}\n\nDocument: {document}\n\nScore:"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    url = f"{config.base_url}/api/chat"
    payload = {
        "model": config.model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.0,  # Deterministic scoring
            "num_predict": 50,  # Limit response length
        },
    }

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}

        # Extract content from response
        if "message" in data and "content" in data["message"]:
            content = data["message"]["content"].strip()
        else:
            raise RerankError(f"Unexpected Ollama response format: {list(data.keys())}")

        # Parse JSON score
        # Strip markdown code blocks if present
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        score_data = json.loads(content)

        if not isinstance(score_data, dict) or "score" not in score_data:
            raise RerankError(f"Invalid score format: {content}")

        score = float(score_data["score"])

        # Clamp to [0.0, 1.0]
        return max(0.0, min(1.0, score))

    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RerankError(f"HTTP error calling {url}: {e.code} {msg}") from e
    except urllib.error.URLError as e:
        raise RerankError(f"Failed to reach Ollama at {url}: {e}") from e
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        raise RerankError(f"Failed to parse reranking score: {e}") from e


def rerank_results(
    query: str,
    results: list[dict[str, Any]],
    *,
    collect_metrics: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], RerankerDebugMetrics]:
    """Rerank search results using cross-encoder scoring.

    Takes initial retrieval results and re-scores them using a more
    sophisticated relevance model. Returns the top-N most relevant results.

    **Pipeline:**

    1. Check if reranking is enabled in config
    2. For each result, score relevance using Ollama
    3. Sort by reranked scores (descending)
    4. Return top-N results

    **Configuration:**

    Controlled by `[rag]` section in config.ini:
    - `enable_reranking`: Enable/disable (default: false)
    - `reranker_model`: Model to use (default: same as default_model)
    - `rerank_top_n`: Number of results to return (default: 3)
    - `reranker_timeout_seconds`: Timeout per score request (default: 30)

    **Performance Notes:**

    - Reranking is expensive (one LLM call per result)
    - Apply reranking only to top-K candidates (e.g., top 10-20)
    - Use a fast model for reranking (e.g., qwen2.5:0.5b)

    Args:
        query: User's search query
        results: Initial retrieval results (list of SearchResult-like dicts).
                 Each dict should have 'text' and optionally 'score' fields.
        collect_metrics: If True, return tuple of (results, metrics)

    Returns:
        Reranked results (top-N), sorted by relevance.
        If collect_metrics=True, returns tuple of (results, RerankerDebugMetrics).
        Results follow the SearchResult type structure.

    Raises:
        RerankError: If reranking fails for all results

    Example:
        >>> initial_results = retrieve_context("Python programming", top_k=10)
        >>> reranked = rerank_results("Python programming", initial_results)
        >>> print(f"Top result: {reranked[0]['text'][:100]}")
    """
    start_time = time.perf_counter()
    config = _reranker_cfg()

    # If reranking is disabled, return results unchanged
    if not config.enabled:
        log.debug("Reranking disabled, returning original results")
        if collect_metrics:
            metrics = RerankerDebugMetrics(
                reranker_model=config.model,
                num_candidates=len(results),
                num_reranked=len(results),
                reranking_time_ms=0.0,
                score_min=None,
                score_max=None,
                score_mean=None,
            )
            return results, metrics
        return results

    # If no results, return empty list
    if not results:
        log.debug("No results to rerank")
        if collect_metrics:
            metrics = RerankerDebugMetrics(
                reranker_model=config.model,
                num_candidates=0,
                num_reranked=0,
                reranking_time_ms=0.0,
                score_min=None,
                score_max=None,
                score_mean=None,
            )
            return [], metrics
        return []

    log.debug(
        "Reranking %d results with model %s, returning top %d",
        len(results),
        config.model,
        config.top_n,
    )

    # Score each result
    reranked: list[tuple[dict[str, Any], float]] = []
    failed_count = 0

    for result in results:
        text = result.get("text", "").strip()
        if not text:
            continue

        try:
            score = _score_relevance(query, text, config)
            # Create a copy with updated score
            reranked_result = result.copy()
            reranked_result["score"] = score
            reranked_result["original_score"] = result.get("score")
            reranked.append((reranked_result, score))
        except RerankError as e:
            log.warning(
                "Failed to rerank result: %s",
                e,
                extra={"component": "rag", "doc_id": result.get("doc_id")},
            )
            failed_count += 1
            # Keep original result if reranking fails
            reranked.append((result, result.get("score", 0.0)))

    # Sort by reranked score (descending) and take top-N
    reranked.sort(key=lambda x: x[1], reverse=True)
    top_results = [r for r, _ in reranked[: config.top_n]]

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    scores = [r["score"] for r in top_results if r.get("score") is not None]

    log.debug(
        "Reranking completed",
        extra={
            "component": "rag",
            "candidate_count": len(results),
            "result_count": len(top_results),
            "failed_count": failed_count,
            "score_min": min(scores) if scores else None,
            "score_max": max(scores) if scores else None,
            "duration_ms": round(elapsed_ms, 1),
        },
    )

    if collect_metrics:
        metrics = RerankerDebugMetrics(
            reranker_model=config.model,
            num_candidates=len(results),
            num_reranked=len(top_results),
            reranking_time_ms=elapsed_ms,
            score_min=min(scores) if scores else None,
            score_max=max(scores) if scores else None,
            score_mean=sum(scores) / len(scores) if scores else None,
        )
        return top_results, metrics

    return top_results
