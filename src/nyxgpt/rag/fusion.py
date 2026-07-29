"""Reciprocal Rank Fusion (RRF) for combining multiple search result rankings.

This module implements Reciprocal Rank Fusion, an algorithm for merging
ranked lists from different retrieval systems. It's used in hybrid search
to combine BM25 keyword results with vector similarity results.

**Why RRF?**

1. **No Score Normalization Needed**: Works with raw rankings, not scores.
   This is important because BM25 scores and cosine similarity scores have
   different ranges and distributions.

2. **Robust to Outliers**: A document ranked very high in one system but
   missing from another still gets fair treatment.

3. **Simple and Effective**: Despite its simplicity, RRF often outperforms
   more complex fusion methods in practice.

**Algorithm Overview:**

RRF assigns each document a fusion score based on its ranks across all systems:

    RRF_score(d) = Σ(1 / (k + rank(d, system)))

Where:
- d: document
- rank(d, system): position of d in that system's ranking (1-indexed)
- k: constant to prevent division by small numbers (typically 60)
- Σ: sum over all systems that returned document d

**Example:**

    System A ranks: [doc1, doc2, doc3]  -> doc1=rank 1, doc2=rank 2, doc3=rank 3
    System B ranks: [doc2, doc1, doc4]  -> doc2=rank 1, doc1=rank 2, doc4=rank 3

    RRF scores (k=60):
    - doc1: 1/(60+1) + 1/(60+2) = 0.0164 + 0.0161 = 0.0325
    - doc2: 1/(60+2) + 1/(60+1) = 0.0161 + 0.0164 = 0.0325
    - doc3: 1/(60+3) = 0.0159
    - doc4: 1/(60+3) = 0.0159

    Final ranking: [doc1, doc2, doc3, doc4] (ties broken by original order)

**Configuration:**

Controlled by `[rag]` section in config.ini:
- `rrf_k`: RRF constant for rank weighting (default: 60)
- `hybrid_alpha`: Weight between vector (1.0) and keyword (0.0) when using
  weighted fusion instead of pure RRF (default: None for pure RRF)

**References:**

- Cormack, G. V., Clarke, C. L., & Buettcher, S. (2009). "Reciprocal rank
  fusion outperforms condorcet and individual rank learning methods."
  Proceedings of SIGIR '09.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nyxgpt.config import load_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RRFConfig:
    """Reciprocal Rank Fusion parameters."""

    k: float  # Constant for preventing division by small numbers (typically 60)


def _rrf_cfg() -> RRFConfig:
    """Load RRF configuration from config.ini."""
    cfg = load_config(None)
    k = cfg.getfloat("rag", "rrf_k", fallback=60.0)
    return RRFConfig(k=k)


def reciprocal_rank_fusion(
    rankings: list[list[tuple[Any, float]]], k: float | None = None
) -> list[tuple[Any, float]]:
    """Merge multiple ranked lists using Reciprocal Rank Fusion.

    This function takes multiple ranking systems' outputs and combines them
    into a single unified ranking. Each system provides a list of
    (item_id, score) tuples, where score is the system's relevance score.

    **Important Notes:**

    1. **Input scores are ignored**: RRF only uses the rank position (order)
       of items in each list. The actual score values are discarded.

    2. **Item identity**: Items are matched across systems by identity (==).
       Ensure that the same document/item uses the same identifier across
       all ranking systems.

    3. **Partial rankings**: An item may appear in some rankings but not others.
       RRF handles this gracefully - the item only contributes from systems
       where it appears.

    **Algorithm:**

    For each item that appears in any ranking:
    1. Look up its rank position in each system (1-indexed)
    2. Compute: score = Σ(1 / (k + rank)) over all systems
    3. Sort items by score descending

    **Performance:**

    - Time: O(N * M) where N=total unique items, M=number of systems
    - Space: O(N) for storing fusion scores

    Args:
        rankings: List of ranked results from different systems.
                 Each inner list contains (item_id, score) tuples sorted by score.
                 Example: [
                     [(doc1, 0.9), (doc2, 0.8)],  # System A results
                     [(doc2, 12.3), (doc1, 10.1)]  # System B results
                 ]
        k: RRF constant (default: from config or 60.0).
           Higher k reduces the importance of rank differences.
           Lower k amplifies the importance of top-ranked items.

    Returns:
        Unified ranking as list of (item_id, rrf_score) tuples,
        sorted by RRF score descending.

    Examples:
        >>> # Combine vector and keyword results
        >>> vector_results = [("doc1", 0.95), ("doc3", 0.87), ("doc2", 0.82)]
        >>> keyword_results = [("doc2", 15.2), ("doc1", 12.1), ("doc4", 8.3)]
        >>>
        >>> fused = reciprocal_rank_fusion([vector_results, keyword_results])
        >>> # fused = [("doc1", 0.0325), ("doc2", 0.0325), ("doc3", 0.0159), ("doc4", 0.0159)]
        >>>
        >>> # Documents appearing in both systems rank higher than those in only one
    """
    if k is None:
        k = _rrf_cfg().k

    # Build a map: item_id -> list of ranks across systems
    item_ranks: dict[Any, list[int]] = {}

    for ranking in rankings:
        for rank, (item_id, _score) in enumerate(ranking, start=1):
            if item_id not in item_ranks:
                item_ranks[item_id] = []
            item_ranks[item_id].append(rank)

    # Compute RRF score for each item
    rrf_scores: list[tuple[Any, float]] = []
    for item_id, ranks in item_ranks.items():
        # RRF formula: sum of 1/(k + rank) over all systems
        score = sum(1.0 / (k + rank) for rank in ranks)
        rrf_scores.append((item_id, score))

    # Sort by RRF score descending
    rrf_scores.sort(key=lambda x: x[1], reverse=True)

    log.debug(
        "RRF fusion completed",
        extra={
            "component": "rag",
            "input_lists": len(rankings),
            "unique_items": len(item_ranks),
        },
    )
    return rrf_scores


def weighted_fusion(
    vector_results: list[tuple[Any, float]],
    keyword_results: list[tuple[Any, float]],
    alpha: float = 0.5,
) -> list[tuple[Any, float]]:
    """Combine vector and keyword results using weighted score fusion.

    This is an alternative to RRF that directly combines normalized scores
    from both systems using a weighted average. Unlike RRF, this method
    preserves the relative magnitudes of scores within each system.

    **Formula:**

        final_score = alpha * norm_vector_score + (1 - alpha) * norm_keyword_score

    Where:
    - norm_vector_score: Vector score normalized to [0, 1] using min-max scaling
    - norm_keyword_score: Keyword score normalized to [0, 1] using min-max scaling
    - alpha: Weight for vector scores (0.0 = keyword only, 1.0 = vector only)

    **When to Use:**

    - Use weighted_fusion when you want to control the relative importance
      of vector vs keyword search explicitly.
    - Use reciprocal_rank_fusion (default) when you want robust fusion that
      doesn't require score normalization or tuning alpha.

    **Score Normalization:**

    Each system's scores are normalized to [0, 1] using min-max scaling:

        norm_score = (score - min_score) / (max_score - min_score)

    If all scores are identical, normalization results in 0.0 for all items.

    Args:
        vector_results: Results from vector search as (item_id, score) tuples
        keyword_results: Results from keyword search as (item_id, score) tuples
        alpha: Weight for vector scores in [0.0, 1.0].
               - 0.0: keyword only
               - 0.5: equal weight (default)
               - 1.0: vector only

    Returns:
        Fused ranking as list of (item_id, weighted_score) tuples,
        sorted by weighted score descending.

    Examples:
        >>> vector = [("doc1", 0.95), ("doc2", 0.85)]
        >>> keyword = [("doc2", 12.0), ("doc3", 8.0)]
        >>>
        >>> # Prefer vector search (alpha=0.7)
        >>> fused = weighted_fusion(vector, keyword, alpha=0.7)
        >>>
        >>> # Prefer keyword search (alpha=0.3)
        >>> fused = weighted_fusion(vector, keyword, alpha=0.3)
    """
    # Validate alpha
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0.0, 1.0], got {alpha}")

    # Normalize vector scores to [0, 1]
    vector_map: dict[Any, float] = {}
    if vector_results:
        v_scores = [score for _, score in vector_results]
        v_min, v_max = min(v_scores), max(v_scores)
        v_range = v_max - v_min
        for item_id, score in vector_results:
            norm_score = (score - v_min) / v_range if v_range > 0 else 0.0
            vector_map[item_id] = norm_score

    # Normalize keyword scores to [0, 1]
    keyword_map: dict[Any, float] = {}
    if keyword_results:
        k_scores = [score for _, score in keyword_results]
        k_min, k_max = min(k_scores), max(k_scores)
        k_range = k_max - k_min
        for item_id, score in keyword_results:
            norm_score = (score - k_min) / k_range if k_range > 0 else 0.0
            keyword_map[item_id] = norm_score

    # Combine scores using weighted average
    all_items = set(vector_map.keys()) | set(keyword_map.keys())
    fused: list[tuple[Any, float]] = []

    for item_id in all_items:
        v_score = vector_map.get(item_id, 0.0)
        k_score = keyword_map.get(item_id, 0.0)
        final_score = alpha * v_score + (1.0 - alpha) * k_score
        fused.append((item_id, final_score))

    # Sort by final score descending
    fused.sort(key=lambda x: x[1], reverse=True)

    log.debug(
        "Weighted fusion completed",
        extra={
            "component": "rag",
            "vector_count": len(vector_results),
            "keyword_count": len(keyword_results),
            "unique_items": len(all_items),
            "alpha": alpha,
        },
    )
    return fused
