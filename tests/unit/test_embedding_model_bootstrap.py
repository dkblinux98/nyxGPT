"""Tests for first-use bootstrap of the embedding model (#3719).

On a fresh install nothing has ever pulled the configured `[rag]
embedding_model`, so the very first document upload used to fail with
Ollama's raw transport error:

    HTTP error calling http://127.0.0.1:11434/api/embed: 404
    {"error": "model \"nomic-embed-text\" not found, try pulling it first"}

`embed_texts` now completes that bootstrap the same way the vector store
creates its keyspace on demand: pull the model once, replay the batches, and
fall back to an actionable message if the pull is disabled or fails.
"""

from __future__ import annotations

from configparser import ConfigParser

import pytest

from nyxgpt.cache import NoOpCache
from nyxgpt.rag import embeddings as embeddings_module
from nyxgpt.rag.embeddings import (
    EmbeddingError,
    EmbeddingModelMissingError,
    _is_model_missing_error,
    embed_texts,
)

MISSING_MODEL_BODY = (
    "HTTP error calling http://127.0.0.1:11434/api/embed: 404 "
    '{"error":"model \\"nomic-embed-text\\" not found, try pulling it first"}'
)


def _cfg(**rag_overrides: str) -> ConfigParser:
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://127.0.0.1:11434"}
    cfg["rag"] = {
        "embedding_model": "nomic-embed-text",
        "embedding_dim": "3",
        **rag_overrides,
    }
    return cfg


@pytest.fixture(autouse=True)
def _isolated_embedding_env(monkeypatch: pytest.MonkeyPatch):
    """Disable the embedding cache so every test really hits the transport."""
    monkeypatch.setattr(embeddings_module, "_embedding_cache", NoOpCache())
    yield
    monkeypatch.setattr(embeddings_module, "_embedding_cache", None)


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg: ConfigParser) -> None:
    monkeypatch.setattr(embeddings_module, "load_config", lambda *_a, **_k: cfg)


def _patch_post_json(monkeypatch: pytest.MonkeyPatch, responses: list) -> list[dict]:
    """Patch `_post_json` to replay `responses` in order, recording payloads.

    Each entry is either an exception to raise or a dict to return.
    """
    calls: list[dict] = []
    queue = list(responses)

    def fake_post_json(url, payload, timeout):  # noqa: ARG001
        calls.append(payload)
        outcome = queue.pop(0) if queue else responses[-1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(embeddings_module, "_post_json", fake_post_json)
    return calls


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_model_error_is_recognized():
    assert _is_model_missing_error(EmbeddingError(MISSING_MODEL_BODY))


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "HTTP error calling http://127.0.0.1:11434/api/embed: 404 not found",
        "Failed to reach Ollama at http://127.0.0.1:11434/api/embed: timed out",
        "Unexpected Ollama embed response keys: []",
    ],
)
def test_other_errors_are_not_treated_as_missing_model(message: str):
    """A generic 404 (bad base URL, proxy) must not trigger a model pull."""
    assert not _is_model_missing_error(EmbeddingError(message))


# ---------------------------------------------------------------------------
# Auto-pull bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_model_is_pulled_and_embedding_retried(monkeypatch: pytest.MonkeyPatch):
    """The reported failure: first upload on a fresh install now succeeds."""
    _patch_config(monkeypatch, _cfg())
    calls = _patch_post_json(
        monkeypatch,
        [EmbeddingError(MISSING_MODEL_BODY), {"embeddings": [[0.1, 0.2, 0.3]]}],
    )

    pulled: list[tuple[str, str, float]] = []

    def fake_pull_model(
        name, base_url=None, progress_callback=None, timeout_s=600.0
    ):  # noqa: ARG001
        pulled.append((name, base_url, timeout_s))
        return {"status": "success"}

    monkeypatch.setattr("nyxgpt.models.pull_model", fake_pull_model)

    result = embed_texts(["hello"])

    assert result == [[0.1, 0.2, 0.3]]
    assert pulled == [("nomic-embed-text", "http://127.0.0.1:11434", 600.0)]
    # The batch was replayed after the pull, not silently dropped.
    assert len(calls) == 2
    assert calls[1]["input"] == ["hello"]


@pytest.mark.unit
def test_pull_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, _cfg(embedding_pull_timeout_seconds="90"))
    _patch_post_json(
        monkeypatch,
        [EmbeddingError(MISSING_MODEL_BODY), {"embeddings": [[0.1, 0.2, 0.3]]}],
    )

    seen: list[float] = []

    def fake_pull_model(
        name, base_url=None, progress_callback=None, timeout_s=600.0
    ):  # noqa: ARG001
        seen.append(timeout_s)
        return {"status": "success"}

    monkeypatch.setattr("nyxgpt.models.pull_model", fake_pull_model)

    embed_texts(["hello"])

    assert seen == [90.0]


@pytest.mark.unit
def test_no_pull_when_model_is_present(monkeypatch: pytest.MonkeyPatch):
    """The happy path must not gain an extra pull round trip."""
    _patch_config(monkeypatch, _cfg())
    _patch_post_json(monkeypatch, [{"embeddings": [[0.1, 0.2, 0.3]]}])

    def fail_pull(*_a, **_k):
        raise AssertionError("pull_model must not be called when the model exists")

    monkeypatch.setattr("nyxgpt.models.pull_model", fail_pull)

    assert embed_texts(["hello"]) == [[0.1, 0.2, 0.3]]


@pytest.mark.unit
def test_unrelated_embedding_error_is_not_retried(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, _cfg())
    calls = _patch_post_json(
        monkeypatch,
        [EmbeddingError("Failed to reach Ollama at http://127.0.0.1:11434/api/embed: refused")],
    )

    def fail_pull(*_a, **_k):
        raise AssertionError("pull_model must not be called for transport errors")

    monkeypatch.setattr("nyxgpt.models.pull_model", fail_pull)

    with pytest.raises(EmbeddingError) as excinfo:
        embed_texts(["hello"])

    assert "Failed to reach Ollama" in str(excinfo.value)
    assert not isinstance(excinfo.value, EmbeddingModelMissingError)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Actionable failure messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_auto_pull_disabled_raises_actionable_error(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, _cfg(embedding_auto_pull="false"))
    _patch_post_json(monkeypatch, [EmbeddingError(MISSING_MODEL_BODY)])

    def fail_pull(*_a, **_k):
        raise AssertionError("pull_model must not be called when auto-pull is disabled")

    monkeypatch.setattr("nyxgpt.models.pull_model", fail_pull)

    with pytest.raises(EmbeddingModelMissingError) as excinfo:
        embed_texts(["hello"])

    message = str(excinfo.value)
    assert "nyxgpt models pull nomic-embed-text" in message
    assert "embedding_auto_pull is disabled" in message


@pytest.mark.unit
def test_failed_pull_raises_actionable_error(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, _cfg())
    _patch_post_json(monkeypatch, [EmbeddingError(MISSING_MODEL_BODY)])

    def broken_pull(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("nyxgpt.models.pull_model", broken_pull)

    with pytest.raises(EmbeddingModelMissingError) as excinfo:
        embed_texts(["hello"])

    message = str(excinfo.value)
    assert "nyxgpt models pull nomic-embed-text" in message
    assert "connection reset" in message


@pytest.mark.unit
def test_still_missing_after_pull_raises_actionable_error(monkeypatch: pytest.MonkeyPatch):
    """A pull that reports success but leaves the model absent still explains itself."""
    _patch_config(monkeypatch, _cfg())
    _patch_post_json(
        monkeypatch,
        [EmbeddingError(MISSING_MODEL_BODY), EmbeddingError(MISSING_MODEL_BODY)],
    )
    monkeypatch.setattr("nyxgpt.models.pull_model", lambda *_a, **_k: {"status": "success"})

    with pytest.raises(EmbeddingModelMissingError) as excinfo:
        embed_texts(["hello"])

    assert "still missing after an auto-pull" in str(excinfo.value)


@pytest.mark.unit
def test_missing_model_error_is_an_embedding_error():
    """Callers catching EmbeddingError keep working."""
    assert issubclass(EmbeddingModelMissingError, EmbeddingError)
