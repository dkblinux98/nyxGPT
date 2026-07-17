"""Coverage for src/nyxgpt/rag/model_compare.py (issue #3238, scoped split of #3222):

- benchmark_embedding_speed happy path (averages elapsed time over num_runs)
- benchmark_embedding_speed error path (EmbeddingError -> ValueError)
- benchmark_query_speed happy path (embed + query + close, averaged over runs)
- compare_models embedding-failure and query-failure branches
- print_comparison_table: empty metrics, single metric, multiple metrics with
  and without any query times recorded
"""

from __future__ import annotations

import pytest

from nyxgpt.rag.embeddings import EmbeddingError
from nyxgpt.rag.model_compare import (
    ModelPerformanceMetrics,
    benchmark_embedding_speed,
    benchmark_query_speed,
    compare_models,
    print_comparison_table,
)


@pytest.mark.unit
def test_benchmark_embedding_speed_averages_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path calls embed_texts once per run and averages the elapsed times."""
    call_count = 0

    def fake_embed_texts(texts, **kwargs):
        nonlocal call_count
        call_count += 1
        assert texts == ["hello", "world"]
        assert kwargs.get("model") == "test-model"
        assert kwargs.get("dimension") == 768
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr("nyxgpt.rag.model_compare.embed_texts", fake_embed_texts)

    avg_time = benchmark_embedding_speed("test-model", 768, ["hello", "world"], num_runs=3)

    assert call_count == 3
    assert avg_time >= 0.0


@pytest.mark.unit
def test_benchmark_embedding_speed_wraps_embedding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EmbeddingError from embed_texts is re-raised as a ValueError with context."""

    def fake_embed_texts(texts, **kwargs):
        raise EmbeddingError("backend unreachable")

    monkeypatch.setattr("nyxgpt.rag.model_compare.embed_texts", fake_embed_texts)

    with pytest.raises(ValueError, match="Failed to embed with model test-model"):
        benchmark_embedding_speed("test-model", 768, ["hello"], num_runs=1)


@pytest.mark.unit
def test_benchmark_query_speed_averages_over_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """benchmark_query_speed embeds each query, queries the store, and closes it."""
    embed_calls = []
    query_calls = []
    closed = []

    def fake_embed_texts(texts, **kwargs):
        embed_calls.append((tuple(texts), kwargs.get("model"), kwargs.get("dimension")))
        return [[0.1, 0.2, 0.3]]

    class FakeStore:
        def __init__(self, *, collection: str) -> None:
            self.collection = collection

        def query_by_embedding(self, embedding, *, k, embedding_model):
            query_calls.append((embedding, k, embedding_model))
            return [{"id": "doc-1"}]

        def close(self):
            closed.append(True)

    monkeypatch.setattr("nyxgpt.rag.model_compare.embed_texts", fake_embed_texts)
    monkeypatch.setattr("nyxgpt.rag.model_compare.CassandraVectorStore", FakeStore)

    avg_time = benchmark_query_speed(
        "default", ["query one", "query two"], "test-model", 768, k=3, num_runs=2
    )

    assert avg_time >= 0.0
    # 2 queries x 2 runs
    assert len(embed_calls) == 4
    assert len(query_calls) == 4
    assert all(call == ([0.1, 0.2, 0.3], 3, "test-model") for call in query_calls)
    # Store is closed exactly once even though the loop ran multiple times.
    assert closed == [True]


@pytest.mark.unit
def test_benchmark_query_speed_closes_store_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store is closed even if embedding/querying raises."""
    closed = []

    def fake_embed_texts(texts, **kwargs):
        raise EmbeddingError("boom")

    class FakeStore:
        def __init__(self, *, collection: str) -> None:
            self.collection = collection

        def query_by_embedding(self, embedding, *, k, embedding_model):
            raise AssertionError("should not be reached")

        def close(self):
            closed.append(True)

    monkeypatch.setattr("nyxgpt.rag.model_compare.embed_texts", fake_embed_texts)
    monkeypatch.setattr("nyxgpt.rag.model_compare.CassandraVectorStore", FakeStore)

    with pytest.raises(EmbeddingError):
        benchmark_query_speed("default", ["q"], "test-model", 768, num_runs=1)

    assert closed == [True]


@pytest.mark.unit
def test_compare_models_handles_embedding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When embedding benchmarking raises, compare_models records 0.0 and continues."""

    def fake_benchmark_embedding_speed(model, dim, texts, num_runs=3):
        raise RuntimeError("embed exploded")

    monkeypatch.setattr(
        "nyxgpt.rag.model_compare.benchmark_embedding_speed", fake_benchmark_embedding_speed
    )

    results = compare_models([("bad-model", 128, "default")], ["text"])

    assert len(results) == 1
    assert results[0].model_name == "bad-model"
    assert results[0].avg_embedding_time_ms == 0.0
    assert results[0].avg_query_time_ms == 0.0


@pytest.mark.unit
def test_compare_models_handles_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When query benchmarking raises, compare_models keeps query_time at 0.0."""

    monkeypatch.setattr(
        "nyxgpt.rag.model_compare.benchmark_embedding_speed",
        lambda model, dim, texts, num_runs=3: 1.5,
    )

    def fake_benchmark_query_speed(collection, queries, model, dim, k=5, num_runs=3):
        raise RuntimeError("query exploded")

    monkeypatch.setattr(
        "nyxgpt.rag.model_compare.benchmark_query_speed", fake_benchmark_query_speed
    )

    results = compare_models([("m", 128, "default")], ["text"], test_queries=["q"])

    assert len(results) == 1
    assert results[0].avg_embedding_time_ms == 1.5
    assert results[0].avg_query_time_ms == 0.0


@pytest.mark.unit
def test_compare_models_query_benchmark_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """When query benchmarking succeeds, compare_models records the timing."""

    monkeypatch.setattr(
        "nyxgpt.rag.model_compare.benchmark_embedding_speed",
        lambda model, dim, texts, num_runs=3: 1.5,
    )
    monkeypatch.setattr(
        "nyxgpt.rag.model_compare.benchmark_query_speed",
        lambda collection, queries, model, dim, k=5, num_runs=3: 7.5,
    )

    results = compare_models([("m", 128, "default")], ["text"], test_queries=["q"])

    assert len(results) == 1
    assert results[0].avg_embedding_time_ms == 1.5
    assert results[0].avg_query_time_ms == 7.5


@pytest.mark.unit
def test_compare_models_skips_query_benchmark_without_test_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No test_queries means benchmark_query_speed is never invoked."""
    monkeypatch.setattr(
        "nyxgpt.rag.model_compare.benchmark_embedding_speed",
        lambda model, dim, texts, num_runs=3: 2.0,
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("benchmark_query_speed should not be called")

    monkeypatch.setattr("nyxgpt.rag.model_compare.benchmark_query_speed", fail_if_called)

    results = compare_models([("m", 128, "default")], ["text"], test_queries=None)

    assert results[0].avg_query_time_ms == 0.0


@pytest.mark.unit
def test_print_comparison_table_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """An empty metrics list prints a placeholder message and returns early."""
    print_comparison_table([])

    out = capsys.readouterr().out
    assert "No metrics to display." in out


@pytest.mark.unit
def test_print_comparison_table_single_metric_no_query_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A single metric prints the table but skips the 'fastest' summary lines."""
    metrics = [
        ModelPerformanceMetrics(
            model_name="solo-model",
            dimension=768,
            avg_embedding_time_ms=12.34,
            avg_query_time_ms=0.0,
            avg_recall_at_k=None,
            disk_usage_mb=None,
        )
    ]

    print_comparison_table(metrics)

    out = capsys.readouterr().out
    assert "solo-model" in out
    assert "Fastest embedding" not in out
    assert "Fastest query" not in out


@pytest.mark.unit
def test_print_comparison_table_multiple_metrics_with_query_times(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multiple metrics with query times print both 'fastest' summary lines."""
    metrics = [
        ModelPerformanceMetrics(
            model_name="slow-model",
            dimension=768,
            avg_embedding_time_ms=20.0,
            avg_query_time_ms=15.0,
            avg_recall_at_k=None,
            disk_usage_mb=None,
        ),
        ModelPerformanceMetrics(
            model_name="fast-model",
            dimension=384,
            avg_embedding_time_ms=5.0,
            avg_query_time_ms=2.0,
            avg_recall_at_k=None,
            disk_usage_mb=None,
        ),
    ]

    print_comparison_table(metrics)

    out = capsys.readouterr().out
    assert "Fastest embedding: fast-model" in out
    assert "Fastest query: fast-model" in out


@pytest.mark.unit
def test_print_comparison_table_multiple_metrics_without_any_query_times(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When no metric recorded a query time, the 'fastest query' line is omitted."""
    metrics = [
        ModelPerformanceMetrics(
            model_name="model-a",
            dimension=768,
            avg_embedding_time_ms=20.0,
            avg_query_time_ms=0.0,
            avg_recall_at_k=None,
            disk_usage_mb=None,
        ),
        ModelPerformanceMetrics(
            model_name="model-b",
            dimension=384,
            avg_embedding_time_ms=5.0,
            avg_query_time_ms=0.0,
            avg_recall_at_k=None,
            disk_usage_mb=None,
        ),
    ]

    print_comparison_table(metrics)

    out = capsys.readouterr().out
    assert "Fastest embedding: model-b" in out
    assert "Fastest query" not in out
