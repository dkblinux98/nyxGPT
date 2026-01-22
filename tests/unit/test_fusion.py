"""Unit tests for reciprocal rank fusion."""

from __future__ import annotations

import pytest
from configparser import ConfigParser

from mygpt.rag.fusion import reciprocal_rank_fusion, weighted_fusion, _rrf_cfg


# =============================================================================
# Configuration Tests
# =============================================================================


@pytest.mark.unit
def test_rrf_cfg_defaults(monkeypatch: pytest.MonkeyPatch):
    """_rrf_cfg should return default values when not configured."""
    cfg = ConfigParser()
    cfg["rag"] = {}
    monkeypatch.setattr("mygpt.rag.fusion.load_config", lambda *_a, **_k: cfg)

    result = _rrf_cfg()
    assert result.k == 60.0


@pytest.mark.unit
def test_rrf_cfg_custom(monkeypatch: pytest.MonkeyPatch):
    """_rrf_cfg should use custom values when configured."""
    cfg = ConfigParser()
    cfg["rag"] = {"rrf_k": "100.0"}
    monkeypatch.setattr("mygpt.rag.fusion.load_config", lambda *_a, **_k: cfg)

    result = _rrf_cfg()
    assert result.k == 100.0


# =============================================================================
# Reciprocal Rank Fusion Tests
# =============================================================================


@pytest.mark.unit
def test_rrf_empty_rankings():
    """RRF should handle empty rankings."""
    result = reciprocal_rank_fusion([], k=60.0)
    assert result == []


@pytest.mark.unit
def test_rrf_single_ranking():
    """RRF should handle single ranking (degenerates to sorting by rank)."""
    ranking = [("doc1", 0.9), ("doc2", 0.8), ("doc3", 0.7)]

    result = reciprocal_rank_fusion([ranking], k=60.0)

    # Should return all items in order
    assert len(result) == 3

    # Extract item IDs
    item_ids = [item_id for item_id, score in result]
    assert item_ids == ["doc1", "doc2", "doc3"]

    # Scores should be descending
    scores = [score for item_id, score in result]
    assert scores[0] > scores[1] > scores[2]


@pytest.mark.unit
def test_rrf_two_identical_rankings():
    """RRF should handle two identical rankings."""
    ranking1 = [("doc1", 0.9), ("doc2", 0.8)]
    ranking2 = [("doc1", 10.0), ("doc2", 9.0)]

    result = reciprocal_rank_fusion([ranking1, ranking2], k=60.0)

    # Should return items in same order (both systems agree)
    assert len(result) == 2
    item_ids = [item_id for item_id, score in result]
    assert item_ids == ["doc1", "doc2"]


@pytest.mark.unit
def test_rrf_two_different_rankings():
    """RRF should merge rankings with different orders."""
    # System A ranks: doc1 > doc2 > doc3
    ranking_a = [("doc1", 0.9), ("doc2", 0.8), ("doc3", 0.7)]

    # System B ranks: doc2 > doc1 > doc4
    ranking_b = [("doc2", 12.0), ("doc1", 10.0), ("doc4", 8.0)]

    result = reciprocal_rank_fusion([ranking_a, ranking_b], k=60.0)

    # Should return all unique docs
    item_ids = [item_id for item_id, score in result]
    assert set(item_ids) == {"doc1", "doc2", "doc3", "doc4"}

    # doc1 and doc2 appear in both rankings, so they should rank higher
    # than doc3 and doc4 (which appear in only one)
    assert item_ids[0] in ["doc1", "doc2"]
    assert item_ids[1] in ["doc1", "doc2"]


@pytest.mark.unit
def test_rrf_score_calculation():
    """RRF scores should follow the formula: sum(1 / (k + rank))."""
    ranking = [("doc1", 0.9)]

    result = reciprocal_rank_fusion([ranking], k=60.0)

    # Score should be 1 / (60 + 1) = 1/61
    expected_score = 1.0 / 61.0
    assert len(result) == 1
    assert result[0][0] == "doc1"
    assert abs(result[0][1] - expected_score) < 1e-6


@pytest.mark.unit
def test_rrf_multiple_systems_score_calculation():
    """RRF should sum scores from all systems."""
    # System A: doc1 at rank 1
    ranking_a = [("doc1", 0.9)]

    # System B: doc1 at rank 2
    ranking_b = [("other", 10.0), ("doc1", 9.0)]

    result = reciprocal_rank_fusion([ranking_a, ranking_b], k=60.0)

    # Find doc1's score
    doc1_score = next(score for item_id, score in result if item_id == "doc1")

    # Expected: 1/(60+1) + 1/(60+2) = 1/61 + 1/62
    expected_score = (1.0 / 61.0) + (1.0 / 62.0)
    assert abs(doc1_score - expected_score) < 1e-6


@pytest.mark.unit
def test_rrf_partial_rankings():
    """RRF should handle docs that appear in some rankings but not others."""
    # System A has doc1, doc2, doc3
    ranking_a = [("doc1", 0.9), ("doc2", 0.8), ("doc3", 0.7)]

    # System B has doc2, doc4 (no doc1 or doc3)
    ranking_b = [("doc2", 10.0), ("doc4", 8.0)]

    result = reciprocal_rank_fusion([ranking_a, ranking_b], k=60.0)

    # All docs should appear
    item_ids = [item_id for item_id, score in result]
    assert set(item_ids) == {"doc1", "doc2", "doc3", "doc4"}

    # doc2 appears in both systems, should rank highest
    assert item_ids[0] == "doc2"


@pytest.mark.unit
def test_rrf_ignores_scores():
    """RRF should only use ranks, not scores."""
    # System A: large score differences
    ranking_a = [("doc1", 100.0), ("doc2", 1.0)]

    # System B: small score differences
    ranking_b = [("doc2", 0.51), ("doc1", 0.50)]

    result = reciprocal_rank_fusion([ranking_a, ranking_b], k=60.0)

    # RRF doesn't care about score magnitudes, only ranks
    # Both docs appear once at rank 1 and once at rank 2
    # So they should have equal RRF scores
    scores = [score for item_id, score in result]

    # The scores should be very close (tied in ranks)
    assert abs(scores[0] - scores[1]) < 1e-6


@pytest.mark.unit
def test_rrf_custom_k():
    """RRF should use custom k parameter."""
    ranking = [("doc1", 0.9)]

    result = reciprocal_rank_fusion([ranking], k=10.0)

    # Score should be 1 / (10 + 1) = 1/11
    expected_score = 1.0 / 11.0
    assert len(result) == 1
    assert abs(result[0][1] - expected_score) < 1e-6


@pytest.mark.unit
def test_rrf_sorting():
    """RRF results should be sorted by score descending."""
    ranking_a = [("doc1", 0.9), ("doc2", 0.8), ("doc3", 0.7)]
    ranking_b = [("doc3", 10.0), ("doc2", 9.0), ("doc1", 8.0)]

    result = reciprocal_rank_fusion([ranking_a, ranking_b], k=60.0)

    # Extract scores
    scores = [score for item_id, score in result]

    # Should be sorted descending
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1]


# =============================================================================
# Weighted Fusion Tests
# =============================================================================


@pytest.mark.unit
def test_weighted_fusion_alpha_0():
    """weighted_fusion with alpha=0 should use keyword scores only."""
    vector = [("doc1", 0.9), ("doc2", 0.8)]
    keyword = [("doc2", 12.0), ("doc3", 8.0)]

    result = weighted_fusion(vector, keyword, alpha=0.0)

    # With alpha=0, vector scores are ignored
    # So doc2 (keyword score 12.0) should rank above doc3 (keyword score 8.0)
    # doc1 (not in keyword) should have score 0
    item_ids = [item_id for item_id, score in result]

    # doc2 should rank first (has keyword score)
    assert item_ids[0] == "doc2"


@pytest.mark.unit
def test_weighted_fusion_alpha_1():
    """weighted_fusion with alpha=1 should use vector scores only."""
    vector = [("doc1", 0.9), ("doc2", 0.8)]
    keyword = [("doc2", 12.0), ("doc3", 8.0)]

    result = weighted_fusion(vector, keyword, alpha=1.0)

    # With alpha=1, keyword scores are ignored
    # So doc1 (vector score 0.9) should rank above doc2 (vector score 0.8)
    # doc3 (not in vector) should have score 0
    item_ids = [item_id for item_id, score in result]

    # doc1 should rank first (has highest vector score)
    assert item_ids[0] == "doc1"


@pytest.mark.unit
def test_weighted_fusion_alpha_05():
    """weighted_fusion with alpha=0.5 should equally weight both."""
    vector = [("doc1", 1.0), ("doc2", 0.5)]
    keyword = [("doc2", 10.0), ("doc3", 5.0)]

    result = weighted_fusion(vector, keyword, alpha=0.5)

    # All docs should appear
    item_ids = [item_id for item_id, score in result]
    assert set(item_ids) == {"doc1", "doc2", "doc3"}


@pytest.mark.unit
def test_weighted_fusion_score_normalization():
    """weighted_fusion should normalize scores to [0, 1]."""
    vector = [("doc1", 100.0), ("doc2", 50.0)]  # Range: 50
    keyword = [("doc2", 20.0), ("doc3", 10.0)]  # Range: 10

    result = weighted_fusion(vector, keyword, alpha=0.5)

    # Normalized vector scores:
    # doc1: (100 - 50) / 50 = 1.0
    # doc2: (50 - 50) / 50 = 0.0

    # Normalized keyword scores:
    # doc2: (20 - 10) / 10 = 1.0
    # doc3: (10 - 10) / 10 = 0.0

    # Final scores (alpha=0.5):
    # doc1: 0.5 * 1.0 + 0.5 * 0.0 = 0.5
    # doc2: 0.5 * 0.0 + 0.5 * 1.0 = 0.5
    # doc3: 0.5 * 0.0 + 0.5 * 0.0 = 0.0

    # doc1 and doc2 should tie, doc3 should be last
    scores = {item_id: score for item_id, score in result}

    assert abs(scores["doc1"] - 0.5) < 1e-6
    assert abs(scores["doc2"] - 0.5) < 1e-6
    assert abs(scores["doc3"] - 0.0) < 1e-6


@pytest.mark.unit
def test_weighted_fusion_empty_vector():
    """weighted_fusion should handle empty vector results."""
    vector = []
    keyword = [("doc1", 10.0), ("doc2", 5.0)]

    result = weighted_fusion(vector, keyword, alpha=0.5)

    # Should return keyword results with normalized scores
    assert len(result) == 2
    item_ids = [item_id for item_id, score in result]
    assert set(item_ids) == {"doc1", "doc2"}


@pytest.mark.unit
def test_weighted_fusion_empty_keyword():
    """weighted_fusion should handle empty keyword results."""
    vector = [("doc1", 0.9), ("doc2", 0.8)]
    keyword = []

    result = weighted_fusion(vector, keyword, alpha=0.5)

    # Should return vector results with normalized scores
    assert len(result) == 2
    item_ids = [item_id for item_id, score in result]
    assert set(item_ids) == {"doc1", "doc2"}


@pytest.mark.unit
def test_weighted_fusion_both_empty():
    """weighted_fusion should handle both empty results."""
    vector = []
    keyword = []

    result = weighted_fusion(vector, keyword, alpha=0.5)

    assert result == []


@pytest.mark.unit
def test_weighted_fusion_alpha_validation():
    """weighted_fusion should validate alpha range."""
    vector = [("doc1", 0.9)]
    keyword = [("doc2", 10.0)]

    # alpha < 0 should raise ValueError
    with pytest.raises(ValueError, match="alpha must be in"):
        weighted_fusion(vector, keyword, alpha=-0.1)

    # alpha > 1 should raise ValueError
    with pytest.raises(ValueError, match="alpha must be in"):
        weighted_fusion(vector, keyword, alpha=1.1)


@pytest.mark.unit
def test_weighted_fusion_identical_scores():
    """weighted_fusion should handle identical scores gracefully."""
    # All vector scores are the same
    vector = [("doc1", 5.0), ("doc2", 5.0), ("doc3", 5.0)]
    keyword = [("doc1", 10.0), ("doc2", 8.0)]

    result = weighted_fusion(vector, keyword, alpha=0.5)

    # When all vector scores are identical, normalized scores should be 0
    # So only keyword scores matter
    item_ids = [item_id for item_id, score in result]

    # doc1 should rank higher than doc2 (higher keyword score)
    doc1_idx = item_ids.index("doc1")
    doc2_idx = item_ids.index("doc2")
    assert doc1_idx < doc2_idx


@pytest.mark.unit
def test_weighted_fusion_sorting():
    """weighted_fusion results should be sorted by score descending."""
    vector = [("doc1", 0.9), ("doc2", 0.8), ("doc3", 0.7)]
    keyword = [("doc1", 10.0), ("doc2", 8.0), ("doc3", 6.0)]

    result = weighted_fusion(vector, keyword, alpha=0.5)

    # Extract scores
    scores = [score for item_id, score in result]

    # Should be sorted descending
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1]


# =============================================================================
# BM25 Keyword Ranking Tests (for Hybrid Search)
# =============================================================================


@pytest.mark.unit
def test_rrf_exact_keyword_match_ranks_first_with_equal_vector_scores():
    """When vector scores are equal, BM25 exact match should rank first.

    This test verifies that when all documents have equal vector similarity
    scores (e.g., all embeddings are equally similar to the query), the
    BM25 keyword ranking determines the final order. An exact keyword match
    should rank higher than partial or semantic-only matches.

    This is a deterministic unit test for the behavior described in the
    integration test at tests/integration/test_hybrid_search.py (see
    test_hybrid_search_keyword_dominance).
    """
    # Simulate BM25 keyword search results:
    # - doc_exact: Contains the exact query term (highest BM25 score)
    # - doc_partial: Contains related terms (medium BM25 score)
    # - doc_none: No keyword match (not in BM25 results)
    keyword_results = [
        ("doc_exact", 15.0),  # Exact keyword match, highest BM25 score
        ("doc_partial", 8.0),  # Partial match, medium BM25 score
    ]

    # Simulate vector search results with equal scores:
    # All three documents have identical vector similarity scores
    vector_results = [
        ("doc_exact", 0.85),
        ("doc_partial", 0.85),
        ("doc_none", 0.85),  # Semantically similar but no keyword match
    ]

    # Use RRF to fuse the results
    result = reciprocal_rank_fusion([vector_results, keyword_results], k=60.0)

    # Extract document IDs in ranking order
    ranked_doc_ids = [doc_id for doc_id, score in result]

    # doc_exact should rank first because:
    # - It appears in both rankings (rank 1 in keyword, rank 1 in vector)
    # - RRF score = 1/(60+1) + 1/(60+1) = 2/61
    assert ranked_doc_ids[0] == "doc_exact", (
        f"Exact keyword match should rank first, but got: {ranked_doc_ids}"
    )

    # doc_partial should rank second because:
    # - It appears in both rankings (rank 2 in keyword, rank 2 in vector)
    # - RRF score = 1/(60+2) + 1/(60+2) = 2/62
    assert ranked_doc_ids[1] == "doc_partial", (
        f"Partial keyword match should rank second, but got: {ranked_doc_ids}"
    )

    # doc_none should rank last because:
    # - It only appears in vector ranking (rank 3)
    # - RRF score = 1/(60+3) = 1/63
    assert ranked_doc_ids[2] == "doc_none", (
        f"No keyword match should rank last, but got: {ranked_doc_ids}"
    )


@pytest.mark.unit
def test_weighted_fusion_exact_keyword_match_ranks_first_with_equal_vector_scores():
    """When vector scores are equal, BM25 exact match should rank first.

    This test is the weighted_fusion counterpart to the RRF test above.
    When all vector scores are identical, they normalize to 0, so only
    the keyword (BM25) scores determine the final ranking.
    """
    # Simulate BM25 keyword search results:
    # - doc_exact: Contains the exact query term (highest BM25 score)
    # - doc_partial: Contains related terms (medium BM25 score)
    # - doc_none: Weak/no match (lowest BM25 score, needed to ensure doc_partial
    #   doesn't normalize to 0.0 - with only 2 items, the min normalizes to 0)
    keyword_results = [
        ("doc_exact", 15.0),  # Exact keyword match, highest BM25 score
        ("doc_partial", 8.0),  # Partial match, medium BM25 score
        ("doc_none", 1.0),  # Weak keyword match, lowest BM25 score
    ]

    # Simulate vector search results with identical scores:
    # When all scores are the same, normalization results in 0 for all
    vector_results = [
        ("doc_exact", 0.85),
        ("doc_partial", 0.85),
        ("doc_none", 0.85),  # Semantically similar but weak keyword match
    ]

    # Use weighted fusion (alpha=0.5 for equal weight)
    result = weighted_fusion(vector_results, keyword_results, alpha=0.5)

    # Extract document IDs in ranking order
    ranked_doc_ids = [doc_id for doc_id, score in result]

    # When vector scores are identical (all normalize to 0), only keyword
    # scores matter. doc_exact has the highest keyword score.
    assert ranked_doc_ids[0] == "doc_exact", (
        f"Exact keyword match should rank first, but got: {ranked_doc_ids}"
    )

    # doc_partial has the second highest keyword score
    assert ranked_doc_ids[1] == "doc_partial", (
        f"Partial keyword match should rank second, but got: {ranked_doc_ids}"
    )

    # doc_none has the lowest keyword score (normalizes close to 0)
    assert ranked_doc_ids[2] == "doc_none", (
        f"Lowest keyword match should rank last, but got: {ranked_doc_ids}"
    )
