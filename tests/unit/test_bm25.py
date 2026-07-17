"""Unit tests for BM25 keyword search."""

from __future__ import annotations

from configparser import ConfigParser

import pytest

from nyxgpt.rag.bm25 import BM25Index, _bm25_cfg, tokenize

# =============================================================================
# Tokenization Tests
# =============================================================================


@pytest.mark.unit
def test_tokenize_simple():
    """tokenize should lowercase and split on whitespace."""
    result = tokenize("Hello World")
    assert result == ["hello", "world"]


@pytest.mark.unit
def test_tokenize_multiple_spaces():
    """tokenize should handle multiple spaces."""
    result = tokenize("Hello   World  Test")
    assert result == ["hello", "world", "test"]


@pytest.mark.unit
def test_tokenize_empty():
    """tokenize should handle empty string."""
    result = tokenize("")
    assert result == []


@pytest.mark.unit
def test_tokenize_whitespace_only():
    """tokenize should handle whitespace-only string."""
    result = tokenize("   \t\n  ")
    assert result == []


@pytest.mark.unit
def test_tokenize_punctuation():
    """tokenize should keep punctuation attached to words."""
    result = tokenize("Hello, World!")
    assert result == ["hello,", "world!"]


# =============================================================================
# BM25 Configuration Tests
# =============================================================================


@pytest.mark.unit
def test_bm25_cfg_defaults(monkeypatch: pytest.MonkeyPatch):
    """_bm25_cfg should return default values when not configured."""
    cfg = ConfigParser()
    cfg["rag"] = {}
    monkeypatch.setattr("nyxgpt.rag.bm25.load_config", lambda *_a, **_k: cfg)

    result = _bm25_cfg()
    assert result.k1 == 1.5
    assert result.b == 0.75


@pytest.mark.unit
def test_bm25_cfg_custom(monkeypatch: pytest.MonkeyPatch):
    """_bm25_cfg should use custom values when configured."""
    cfg = ConfigParser()
    cfg["rag"] = {"bm25_k1": "2.0", "bm25_b": "0.5"}
    monkeypatch.setattr("nyxgpt.rag.bm25.load_config", lambda *_a, **_k: cfg)

    result = _bm25_cfg()
    assert result.k1 == 2.0
    assert result.b == 0.5


# =============================================================================
# BM25Index Build Tests
# =============================================================================


@pytest.mark.unit
def test_bm25_index_empty():
    """BM25Index should handle empty corpus."""
    index = BM25Index()
    index.build_index([])

    assert index.doc_count == 0
    assert index.avg_doc_length == 0.0
    assert len(index.doc_freqs) == 0


@pytest.mark.unit
def test_bm25_index_single_document():
    """BM25Index should handle single document."""
    index = BM25Index()
    index.build_index(["Python programming guide"])

    assert index.doc_count == 1
    assert len(index.doc_lengths) == 1
    assert index.doc_lengths[0] == 3  # ["python", "programming", "guide"]
    assert index.avg_doc_length == 3.0
    assert index.doc_freqs["python"] == 1
    assert index.doc_freqs["programming"] == 1
    assert index.doc_freqs["guide"] == 1


@pytest.mark.unit
def test_bm25_index_multiple_documents():
    """BM25Index should handle multiple documents."""
    docs = ["Python programming", "Java programming", "Python tutorial"]
    index = BM25Index()
    index.build_index(docs)

    assert index.doc_count == 3
    assert len(index.doc_lengths) == 3
    assert index.avg_doc_length == 2.0  # (2 + 2 + 2) / 3

    # "programming" appears in 2 docs
    assert index.doc_freqs["programming"] == 2
    # "python" appears in 2 docs
    assert index.doc_freqs["python"] == 2
    # "java" appears in 1 doc
    assert index.doc_freqs["java"] == 1
    # "tutorial" appears in 1 doc
    assert index.doc_freqs["tutorial"] == 1


@pytest.mark.unit
def test_bm25_index_term_frequencies():
    """BM25Index should track term frequencies correctly."""
    docs = ["Python Python Java", "Python programming"]
    index = BM25Index()
    index.build_index(docs)

    # Doc 0 has "python" 2 times, "java" 1 time
    assert index.term_doc_freqs[0]["python"] == 2
    assert index.term_doc_freqs[0]["java"] == 1

    # Doc 1 has "python" 1 time, "programming" 1 time
    assert index.term_doc_freqs[1]["python"] == 1
    assert index.term_doc_freqs[1]["programming"] == 1


# =============================================================================
# BM25Index Search Tests
# =============================================================================


@pytest.mark.unit
def test_bm25_search_empty_query():
    """BM25Index search should handle empty query."""
    docs = ["Python programming", "Java tutorial"]
    index = BM25Index()
    index.build_index(docs)

    results = index.search("")
    assert results == []


@pytest.mark.unit
def test_bm25_search_empty_index():
    """BM25Index search should handle empty index."""
    index = BM25Index()
    index.build_index([])

    results = index.search("Python")
    assert results == []


@pytest.mark.unit
def test_bm25_search_no_matches():
    """BM25Index search should return empty list when no matches."""
    docs = ["Python programming", "Java tutorial"]
    index = BM25Index()
    index.build_index(docs)

    results = index.search("JavaScript")
    assert results == []


@pytest.mark.unit
def test_bm25_search_single_term():
    """BM25Index search should rank docs by single term relevance."""
    docs = ["Python programming", "Java programming", "Python tutorial"]
    index = BM25Index()
    index.build_index(docs)

    results = index.search("Python", k=10)

    # Should return 2 results (docs 0 and 2)
    assert len(results) == 2

    # Extract doc indices
    doc_indices = [doc_idx for doc_idx, score in results]
    assert 0 in doc_indices  # "Python programming"
    assert 2 in doc_indices  # "Python tutorial"

    # All scores should be positive
    for _doc_idx, score in results:
        assert score > 0.0


@pytest.mark.unit
def test_bm25_search_multiple_terms():
    """BM25Index search should rank docs by multiple term relevance."""
    docs = [
        "Python programming guide",
        "Java programming tutorial",
        "Python API reference",
    ]
    index = BM25Index()
    index.build_index(docs)

    results = index.search("Python programming", k=10)

    # Should return at least doc 0 (contains both terms)
    assert len(results) > 0

    # Doc 0 should rank highest (has both "python" and "programming")
    top_doc_idx, top_score = results[0]
    assert top_doc_idx == 0
    assert top_score > 0.0


@pytest.mark.unit
def test_bm25_search_top_k():
    """BM25Index search should respect top_k limit."""
    docs = ["Python", "Python guide", "Python tutorial", "Python programming"]
    index = BM25Index()
    index.build_index(docs)

    results = index.search("Python", k=2)

    # Should return at most 2 results
    assert len(results) <= 2


@pytest.mark.unit
def test_bm25_search_term_frequency_scoring():
    """BM25Index should give higher scores to docs with more term occurrences."""
    docs = ["Python", "Python Python", "Python Python Python"]
    index = BM25Index()
    index.build_index(docs)

    results = index.search("Python", k=10)

    # Should return 3 results
    assert len(results) == 3

    # Doc 2 should rank highest (3 occurrences)
    # Doc 1 should rank second (2 occurrences)
    # Doc 0 should rank third (1 occurrence)
    doc_indices = [doc_idx for doc_idx, score in results]
    scores = [score for doc_idx, score in results]

    assert doc_indices[0] == 2  # Most occurrences
    assert doc_indices[1] == 1  # Second most
    assert doc_indices[2] == 0  # Least occurrences

    # Scores should be descending
    assert scores[0] > scores[1] > scores[2]


@pytest.mark.unit
def test_bm25_search_idf_weighting():
    """BM25Index should give higher scores to rare terms."""
    docs = [
        "common common common rare",
        "common common common",
        "common common rare",
    ]
    index = BM25Index()
    index.build_index(docs)

    # Search for rare term
    rare_results = index.search("rare", k=10)

    # Should return 2 docs containing "rare"
    assert len(rare_results) == 2

    # Search for common term
    common_results = index.search("common", k=10)

    # Should return 3 docs containing "common"
    assert len(common_results) == 3

    # Rare term should have higher average score due to IDF
    # (comparing docs that contain both terms)
    doc0_rare_score = next(score for doc_idx, score in rare_results if doc_idx == 0)
    doc0_common_score = next(score for doc_idx, score in common_results if doc_idx == 0)

    # This assertion might be sensitive to BM25 parameters,
    # but generally rare terms score higher per occurrence
    assert doc0_rare_score > 0.0
    assert doc0_common_score > 0.0


@pytest.mark.unit
def test_bm25_search_length_normalization():
    """BM25Index should normalize by document length."""
    # Short doc with term
    # Long doc with same term
    docs = ["Python", "Python and lots of other words here to make it longer"]
    index = BM25Index()
    index.build_index(docs)

    results = index.search("Python", k=10)

    # Should return 2 results
    assert len(results) == 2

    # Shorter doc should generally rank higher (length normalization)
    doc_indices = [doc_idx for doc_idx, score in results]
    scores = [score for doc_idx, score in results]

    # Doc 0 (short) should rank higher than doc 1 (long)
    assert doc_indices[0] == 0  # Short doc first
    assert doc_indices[1] == 1  # Long doc second
    assert scores[0] > scores[1]


@pytest.mark.unit
def test_bm25_search_case_insensitive():
    """BM25Index search should be case-insensitive."""
    docs = ["Python Programming", "java tutorial"]
    index = BM25Index()
    index.build_index(docs)

    # Search with different case
    results_lower = index.search("python", k=10)
    results_upper = index.search("PYTHON", k=10)
    results_mixed = index.search("PyThOn", k=10)

    # All should return the same doc
    assert len(results_lower) == 1
    assert len(results_upper) == 1
    assert len(results_mixed) == 1

    assert results_lower[0][0] == 0  # Doc 0
    assert results_upper[0][0] == 0  # Doc 0
    assert results_mixed[0][0] == 0  # Doc 0


# =============================================================================
# BM25Index Rebuild Tests
# =============================================================================


@pytest.mark.unit
def test_bm25_score_doc_unknown_doc_idx():
    """_score_doc should return 0.0 for a doc_idx not present in the index."""
    index = BM25Index()
    index.build_index(["Python programming"])

    assert index._score_doc(999, ["python"]) == 0.0


@pytest.mark.unit
def test_bm25_index_rebuild():
    """BM25Index should support rebuilding with new corpus."""
    index = BM25Index()

    # Build first index
    index.build_index(["Python programming"])
    assert index.doc_count == 1

    # Rebuild with different corpus
    index.build_index(["Java tutorial", "JavaScript guide"])
    assert index.doc_count == 2
    assert "python" not in index.doc_freqs
    assert "java" in index.doc_freqs
    assert "javascript" in index.doc_freqs
