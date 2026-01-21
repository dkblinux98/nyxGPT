"""Unit tests for RAG reranking functionality."""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import Mock, patch
import pytest


@pytest.mark.unit
def test_rerank_results_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should return original results when disabled."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["mygpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {"enable_reranking": "false"}

    monkeypatch.setattr("mygpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.reranker import rerank_results

    results = [
        {"text": "Result 1", "score": 0.8},
        {"text": "Result 2", "score": 0.6},
        {"text": "Result 3", "score": 0.4},
    ]

    output = rerank_results("test query", results)
    assert output == results


@pytest.mark.unit
def test_rerank_results_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should handle empty list."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["mygpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {"enable_reranking": "true"}

    monkeypatch.setattr("mygpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.reranker import rerank_results

    output = rerank_results("test query", [])
    assert output == []


@pytest.mark.unit
def test_rerank_results_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should return metrics when requested."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["mygpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {
        "enable_reranking": "true",
        "rerank_top_n": "2",
        "reranker_timeout_seconds": "30",
    }

    monkeypatch.setattr("mygpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    # Mock _score_relevance to return deterministic scores
    def mock_score_relevance(query: str, document: str, config):
        # Score inversely to test reranking (flip the order)
        if "Result 1" in document:
            return 0.3
        elif "Result 2" in document:
            return 0.9
        elif "Result 3" in document:
            return 0.6
        return 0.5

    monkeypatch.setattr(
        "mygpt.rag.reranker._score_relevance", mock_score_relevance
    )

    from mygpt.rag.reranker import rerank_results

    results = [
        {"text": "Result 1", "score": 0.8},
        {"text": "Result 2", "score": 0.6},
        {"text": "Result 3", "score": 0.4},
    ]

    output, metrics = rerank_results("test query", results, collect_metrics=True)

    # Should return top 2 after reranking
    assert len(output) == 2
    assert output[0]["text"] == "Result 2"  # Highest reranked score (0.9)
    assert output[1]["text"] == "Result 3"  # Second highest (0.6)

    # Check metrics
    assert metrics.reranker_model == "qwen2.5:0.5b"
    assert metrics.num_candidates == 3
    assert metrics.num_reranked == 2
    assert metrics.reranking_time_ms > 0
    assert metrics.score_max == 0.9
    assert metrics.score_min == 0.6


@pytest.mark.unit
def test_rerank_results_preserves_original_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should preserve original score in metadata."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["mygpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {
        "enable_reranking": "true",
        "rerank_top_n": "3",
    }

    monkeypatch.setattr("mygpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    def mock_score_relevance(query: str, document: str, config):
        return 0.75

    monkeypatch.setattr(
        "mygpt.rag.reranker._score_relevance", mock_score_relevance
    )

    from mygpt.rag.reranker import rerank_results

    results = [
        {"text": "Result 1", "score": 0.8},
    ]

    output = rerank_results("test query", results)

    assert output[0]["score"] == 0.75  # Reranked score
    assert output[0]["original_score"] == 0.8  # Original preserved


@pytest.mark.unit
def test_rerank_results_handles_failure_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should handle scoring failures gracefully."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["mygpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {
        "enable_reranking": "true",
        "rerank_top_n": "3",
    }

    monkeypatch.setattr("mygpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.reranker import RerankError

    def mock_score_relevance(query: str, document: str, config):
        # Fail for first result, succeed for others
        if "Result 1" in document:
            raise RerankError("Scoring failed")
        return 0.7

    monkeypatch.setattr(
        "mygpt.rag.reranker._score_relevance", mock_score_relevance
    )

    from mygpt.rag.reranker import rerank_results

    results = [
        {"text": "Result 1", "score": 0.8},
        {"text": "Result 2", "score": 0.6},
    ]

    output = rerank_results("test query", results)

    # Should still return results even if one fails
    assert len(output) == 2
    # Failed result keeps original score and gets sorted correctly
    # Result 1 failed (score 0.8), Result 2 succeeded (score 0.7)
    # Since 0.8 > 0.7, Result 1 comes first despite failing
    assert output[0]["text"] == "Result 1"  # Failed, kept original (0.8)
    assert output[1]["text"] == "Result 2"  # Reranked successfully (0.7)


@pytest.mark.unit
def test_score_relevance_json_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """_score_relevance should parse various JSON formats."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["mygpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("mygpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.reranker import _score_relevance, RerankerConfig

    config = RerankerConfig(
        base_url="http://localhost:11434",
        model="qwen2.5:0.5b",
        timeout=30,
        top_n=3,
        enabled=True,
    )

    # Mock urlopen to return different response formats
    test_cases = [
        '{"score": 0.85}',  # Plain JSON
        '```json\n{"score": 0.85}\n```',  # Markdown code block
        '```\n{"score": 0.85}\n```',  # Generic code block
    ]

    for response_content in test_cases:
        mock_response = Mock()
        # Properly construct JSON with escaped inner content
        import json as json_module
        response_data = {"message": {"content": response_content}}
        mock_response.read.return_value = json_module.dumps(response_data).encode("utf-8")
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            score = _score_relevance("test query", "test document", config)
            assert score == 0.85


@pytest.mark.unit
def test_score_relevance_clamps_to_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """_score_relevance should clamp scores to [0.0, 1.0]."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["mygpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("mygpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.reranker import _score_relevance, RerankerConfig

    config = RerankerConfig(
        base_url="http://localhost:11434",
        model="qwen2.5:0.5b",
        timeout=30,
        top_n=3,
        enabled=True,
    )

    # Test scores outside [0.0, 1.0] range
    test_cases = [
        ('{"score": 1.5}', 1.0),  # Above 1.0
        ('{"score": -0.3}', 0.0),  # Below 0.0
        ('{"score": 0.5}', 0.5),  # Within range
    ]

    for response_content, expected_score in test_cases:
        mock_response = Mock()
        # Properly construct JSON with escaped inner content
        import json as json_module
        response_data = {"message": {"content": response_content}}
        mock_response.read.return_value = json_module.dumps(response_data).encode("utf-8")
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            score = _score_relevance("test query", "test document", config)
            assert score == expected_score
