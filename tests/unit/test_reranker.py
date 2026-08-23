"""Unit tests for RAG reranking functionality."""

from __future__ import annotations

import json as json_module
import urllib.error
from configparser import ConfigParser
from unittest.mock import Mock, patch

import pytest


@pytest.mark.unit
def test_rerank_results_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should return original results when disabled."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {"enable_reranking": "false"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import rerank_results

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
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {"enable_reranking": "true"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import rerank_results

    output = rerank_results("test query", [])
    assert output == []


@pytest.mark.unit
def test_rerank_results_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should return metrics when requested."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {
        "enable_reranking": "true",
        "rerank_top_n": "2",
        "reranker_timeout_seconds": "30",
    }

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

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

    monkeypatch.setattr("nyxgpt.rag.reranker._score_relevance", mock_score_relevance)

    from nyxgpt.rag.reranker import rerank_results

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
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {
        "enable_reranking": "true",
        "rerank_top_n": "3",
    }

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    def mock_score_relevance(query: str, document: str, config):
        return 0.75

    monkeypatch.setattr("nyxgpt.rag.reranker._score_relevance", mock_score_relevance)

    from nyxgpt.rag.reranker import rerank_results

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
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {
        "enable_reranking": "true",
        "rerank_top_n": "3",
    }

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankError

    def mock_score_relevance(query: str, document: str, config):
        # Fail for first result, succeed for others
        if "Result 1" in document:
            raise RerankError("Scoring failed")
        return 0.7

    monkeypatch.setattr("nyxgpt.rag.reranker._score_relevance", mock_score_relevance)

    from nyxgpt.rag.reranker import rerank_results

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
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, _score_relevance

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
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, _score_relevance

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


@pytest.mark.unit
def test_rerank_results_disabled_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should return metrics even when reranking is disabled."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {"enable_reranking": "false"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import rerank_results

    results = [{"text": "Result 1", "score": 0.8}]

    output, metrics = rerank_results("test query", results, collect_metrics=True)

    assert output == results
    assert metrics.num_candidates == 1
    assert metrics.num_reranked == 1
    assert metrics.reranking_time_ms == 0.0
    assert metrics.score_min is None
    assert metrics.score_max is None
    assert metrics.score_mean is None


@pytest.mark.unit
def test_rerank_results_empty_list_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should return metrics for an empty candidate list."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {"enable_reranking": "true"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import rerank_results

    output, metrics = rerank_results("test query", [], collect_metrics=True)

    assert output == []
    assert metrics.num_candidates == 0
    assert metrics.num_reranked == 0
    assert metrics.score_min is None
    assert metrics.score_max is None
    assert metrics.score_mean is None


@pytest.mark.unit
def test_rerank_results_skips_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank_results should skip results whose text is empty/whitespace."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {"enable_reranking": "true", "rerank_top_n": "3"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    def mock_score_relevance(query: str, document: str, config):
        return 0.5

    monkeypatch.setattr("nyxgpt.rag.reranker._score_relevance", mock_score_relevance)

    from nyxgpt.rag.reranker import rerank_results

    results = [
        {"text": "", "score": 0.8},
        {"text": "   ", "score": 0.7},
        {"text": "Real result", "score": 0.6},
    ]

    output = rerank_results("test query", results)

    assert len(output) == 1
    assert output[0]["text"] == "Real result"


@pytest.mark.unit
def test_score_relevance_unexpected_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """_score_relevance should raise RerankError when the response lacks message.content."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, RerankError, _score_relevance

    config = RerankerConfig(
        base_url="http://localhost:11434", model="qwen2.5:0.5b", timeout=30, top_n=3, enabled=True
    )

    mock_response = Mock()
    mock_response.read.return_value = json_module.dumps({"unexpected": "shape"}).encode("utf-8")
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        pytest.raises(RerankError, match="Unexpected Ollama response format"),
    ):
        _score_relevance("test query", "test document", config)


@pytest.mark.unit
def test_score_relevance_invalid_score_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """_score_relevance should raise RerankError when the parsed content has no 'score' key."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, RerankError, _score_relevance

    config = RerankerConfig(
        base_url="http://localhost:11434", model="qwen2.5:0.5b", timeout=30, top_n=3, enabled=True
    )

    response_data = {"message": {"content": '{"not_score": 0.5}'}}
    mock_response = Mock()
    mock_response.read.return_value = json_module.dumps(response_data).encode("utf-8")
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        pytest.raises(RerankError, match="Invalid score format"),
    ):
        _score_relevance("test query", "test document", config)


@pytest.mark.unit
def test_score_relevance_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """_score_relevance should raise RerankError on HTTPError."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, RerankError, _score_relevance

    config = RerankerConfig(
        base_url="http://localhost:11434", model="qwen2.5:0.5b", timeout=30, top_n=3, enabled=True
    )

    import io

    error = urllib.error.HTTPError(
        url="http://localhost:11434/api/chat",
        code=500,
        msg="Internal Error",
        hdrs=None,
        fp=io.BytesIO(b"server error"),
    )

    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(RerankError, match="HTTP error"),
    ):
        _score_relevance("test query", "test document", config)


@pytest.mark.unit
def test_score_relevance_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """_score_relevance should raise RerankError on URLError."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, RerankError, _score_relevance

    config = RerankerConfig(
        base_url="http://localhost:11434", model="qwen2.5:0.5b", timeout=30, top_n=3, enabled=True
    )

    error = urllib.error.URLError("connection refused")

    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(RerankError, match="Failed to reach Ollama"),
    ):
        _score_relevance("test query", "test document", config)


@pytest.mark.unit
def test_score_relevance_json_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """_score_relevance should raise RerankError when the score content isn't valid JSON."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}

    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, RerankError, _score_relevance

    config = RerankerConfig(
        base_url="http://localhost:11434", model="qwen2.5:0.5b", timeout=30, top_n=3, enabled=True
    )

    response_data = {"message": {"content": "not valid json at all"}}
    mock_response = Mock()
    mock_response.read.return_value = json_module.dumps(response_data).encode("utf-8")
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        pytest.raises(RerankError, match="Failed to parse reranking score"),
    ):
        _score_relevance("test query", "test document", config)


def test_reranker_does_not_let_the_model_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fourth ollama caller, invisible to an `ollama_chat(` sweep (#4029 review).

    This one builds the /api/chat payload itself with urllib, so it was missed
    when the other three were threaded. It caps the reply at `num_predict: 50`
    -- with the shipped reasoning model that budget goes entirely on thinking
    and no score comes back, so reranking degrades silently.
    """
    import json as _json
    from unittest.mock import Mock

    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    monkeypatch.setattr("nyxgpt.rag.reranker.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.reranker import RerankerConfig, _score_relevance

    config = RerankerConfig(
        base_url="http://localhost:11434",
        model="qwen2.5:0.5b",
        timeout=30,
        top_n=3,
        enabled=True,
    )

    sent: dict = {}

    def _capture(req, *_a, **_k):
        sent.update(_json.loads(req.data.decode("utf-8")))
        resp = Mock()
        resp.read.return_value = _json.dumps({"message": {"content": '{"score": 0.9}'}}).encode()
        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=False)
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    _score_relevance("query", "document text", config)

    assert sent, "no request was sent"
    assert sent.get("think") is False, (
        "the reranker let the model reason into a 50-token budget; nothing "
        "reads that reasoning and the score never arrives"
    )
