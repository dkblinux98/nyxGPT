"""Unit tests for vector search optimization (#2686).

Tests cover:
- vector_similarity_function / ann_oversample_factor / cassandra_batch_query_concurrency
  config getters (defaults, custom values, clamping/validation)
- CassandraConfig wiring of the new fields via _cassandra_cfg()
- ensure_schema builds the SAI index with the configured similarity function
- query_by_embedding uses the configured similarity function in the ANN query
  and scales the ANN candidate pool by the oversample factor
- query_by_embeddings_batch runs concurrent ANN searches and preserves order,
  tolerates per-item failures, and respects the configured concurrency cap
"""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import Mock

import pytest
from cassandra.query import BoundStatement, PreparedStatement

# ---------------------------------------------------------------------------
# config getters
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_vector_similarity_function_default():
    from nyxgpt.config import get_vector_similarity_function

    cfg = ConfigParser()
    assert get_vector_similarity_function(cfg) == "cosine"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["cosine", "dot_product", "euclidean"])
def test_get_vector_similarity_function_valid_values(value):
    from nyxgpt.config import get_vector_similarity_function

    cfg = ConfigParser()
    cfg.add_section("rag")
    cfg.set("rag", "vector_similarity_function", value.upper())
    assert get_vector_similarity_function(cfg) == value


@pytest.mark.unit
def test_get_vector_similarity_function_invalid_falls_back_to_cosine():
    from nyxgpt.config import get_vector_similarity_function

    cfg = ConfigParser()
    cfg.add_section("rag")
    cfg.set("rag", "vector_similarity_function", "manhattan")
    assert get_vector_similarity_function(cfg) == "cosine"


@pytest.mark.unit
def test_get_ann_oversample_factor_default():
    from nyxgpt.config import get_ann_oversample_factor

    cfg = ConfigParser()
    assert get_ann_oversample_factor(cfg) == 1.0


@pytest.mark.unit
def test_get_ann_oversample_factor_custom():
    from nyxgpt.config import get_ann_oversample_factor

    cfg = ConfigParser()
    cfg.add_section("rag")
    cfg.set("rag", "ann_oversample_factor", "2.5")
    assert get_ann_oversample_factor(cfg) == 2.5


@pytest.mark.unit
def test_get_ann_oversample_factor_clamps_to_range():
    from nyxgpt.config import get_ann_oversample_factor

    cfg_low = ConfigParser()
    cfg_low.add_section("rag")
    cfg_low.set("rag", "ann_oversample_factor", "0.1")
    assert get_ann_oversample_factor(cfg_low) == 1.0

    cfg_high = ConfigParser()
    cfg_high.add_section("rag")
    cfg_high.set("rag", "ann_oversample_factor", "50")
    assert get_ann_oversample_factor(cfg_high) == 5.0


@pytest.mark.unit
def test_get_cassandra_batch_query_concurrency_default():
    from nyxgpt.config import get_cassandra_batch_query_concurrency

    cfg = ConfigParser()
    assert get_cassandra_batch_query_concurrency(cfg) == 4


@pytest.mark.unit
def test_get_cassandra_batch_query_concurrency_clamps_to_range():
    from nyxgpt.config import get_cassandra_batch_query_concurrency

    cfg_low = ConfigParser()
    cfg_low.add_section("rag")
    cfg_low.set("rag", "cassandra_batch_query_concurrency", "0")
    assert get_cassandra_batch_query_concurrency(cfg_low) == 1

    cfg_high = ConfigParser()
    cfg_high.add_section("rag")
    cfg_high.set("rag", "cassandra_batch_query_concurrency", "1000")
    assert get_cassandra_batch_query_concurrency(cfg_high) == 32


# ---------------------------------------------------------------------------
# CassandraConfig wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cassandra_cfg_wires_vector_search_tuning(monkeypatch: pytest.MonkeyPatch) -> None:
    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    cfg = ConfigParser()
    cfg["rag"] = {
        "vector_similarity_function": "dot_product",
        "ann_oversample_factor": "3.0",
        "cassandra_batch_query_concurrency": "8",
    }
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    result = _cassandra_cfg()
    assert result.similarity_function == "dot_product"
    assert result.ann_oversample_factor == 3.0
    assert result.batch_query_concurrency == 8


@pytest.mark.unit
def test_cassandra_config_defaults():
    from nyxgpt.rag.vectorstore_cassandra import CassandraConfig

    cfg = CassandraConfig(hosts=["localhost"], port=9042, keyspace="ks", table="tbl")
    assert cfg.similarity_function == "cosine"
    assert cfg.ann_oversample_factor == 1.0
    assert cfg.batch_query_concurrency == 4


# ---------------------------------------------------------------------------
# test helpers shared by ensure_schema / query_by_embedding(s) tests
# ---------------------------------------------------------------------------


def _prepared_statement_mock() -> Mock:
    prepared = Mock(spec=PreparedStatement)

    def _bind(params):
        bound = Mock(spec=BoundStatement)
        bound.values = params
        bound.custom_payload = None
        bound.keyspace = None
        bound.routing_key = None
        return bound

    prepared.bind.side_effect = _bind
    return prepared


def _make_store(monkeypatch: pytest.MonkeyPatch, *, extra_rag_cfg: dict | None = None):
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    cfg = ConfigParser()
    rag_cfg = {
        "cassandra_keyspace": "test_ks",
        "cassandra_table": "test_tbl",
    }
    rag_cfg.update(extra_rag_cfg or {})
    cfg["rag"] = rag_cfg
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )
    mock_session.prepare.return_value = _prepared_statement_mock()

    store = CassandraVectorStore()
    store._keyspace_ready = True
    return store, mock_session


def _mock_row(**overrides):
    from datetime import datetime

    row = Mock()
    row.doc_id = "doc1"
    row.chunk_id = 0
    row.text = "content"
    row.metadata = "{}"
    row.score = 0.9
    row.embedding_model = "test-model"
    row.embedding_dim = 768
    row.ingested_at = datetime(2024, 1, 1)
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


# ---------------------------------------------------------------------------
# ensure_schema: SAI index similarity function option
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "config_value,expected_option",
    [("cosine", "COSINE"), ("dot_product", "DOT_PRODUCT"), ("euclidean", "EUCLIDEAN")],
)
def test_ensure_schema_uses_configured_similarity_function(
    monkeypatch: pytest.MonkeyPatch, config_value, expected_option
) -> None:
    store, mock_session = _make_store(
        monkeypatch, extra_rag_cfg={"vector_similarity_function": config_value}
    )

    store.ensure_schema(768)

    index_calls = [
        call for call in mock_session.execute.call_args_list if "_embedding_sai" in call[0][0]
    ]
    assert len(index_calls) == 1
    cql = index_calls[0][0][0]
    assert f"'similarity_function': '{expected_option}'" in cql


# ---------------------------------------------------------------------------
# query_by_embedding: configurable similarity function + oversample factor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_by_embedding_uses_configured_similarity_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, mock_session = _make_store(
        monkeypatch, extra_rag_cfg={"vector_similarity_function": "dot_product"}
    )
    mock_session.execute.return_value = [_mock_row()]

    store.query_by_embedding([0.1, 0.2], k=3)

    prepared_cql = mock_session.prepare.call_args[0][0]
    assert "similarity_dot_product(embedding, ?) AS score" in prepared_cql
    assert "similarity_cosine" not in prepared_cql


@pytest.mark.unit
def test_query_by_embedding_scales_fetch_n_by_oversample_factor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, mock_session = _make_store(monkeypatch, extra_rag_cfg={"ann_oversample_factor": "2.0"})
    mock_session.execute.return_value = [_mock_row()]

    mock_prepared = mock_session.prepare.return_value
    bound_stmt = Mock()
    mock_prepared.bind.side_effect = None
    mock_prepared.bind.return_value = bound_stmt

    store.query_by_embedding([0.1, 0.2], k=3)

    # No metadata filter -> multiplier 1, oversample 2.0 -> fetch_n = 6
    bind_args = mock_prepared.bind.call_args[0][0]
    assert bind_args[2] == 6
    assert bound_stmt.fetch_size == 6


# ---------------------------------------------------------------------------
# query_by_embeddings_batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_by_embeddings_batch_empty_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _mock_session = _make_store(monkeypatch)
    assert store.query_by_embeddings_batch([]) == []


def _fake_execute_concurrent_unpacking(responses):
    """Build a fake execute_concurrent that enforces the real driver's contract:
    ``statements_and_parameters`` must be an iterable of ``(statement, params)``
    tuples, unpacked via ``for (statement, params) in statements_and_parameters``.
    A bare list of statement objects (not tuples) will raise here, just as it
    would against the real ``cassandra.concurrent.execute_concurrent``.
    """

    def _fake(_session, statements_and_parameters, **_kwargs):
        for _statement, params in statements_and_parameters:
            assert isinstance(params, tuple)
        return responses

    return _fake


@pytest.mark.unit
def test_query_by_embeddings_batch_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    store, mock_session = _make_store(monkeypatch)

    responses = [
        (True, [_mock_row(doc_id="doc-a")]),
        (True, [_mock_row(doc_id="doc-b")]),
        (True, [_mock_row(doc_id="doc-c")]),
    ]
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.execute_concurrent",
        _fake_execute_concurrent_unpacking(responses),
    )

    results = store.query_by_embeddings_batch(
        [[0.1], [0.2], [0.3]],
        k=1,
    )

    assert [r[0]["doc_id"] for r in results] == ["doc-a", "doc-b", "doc-c"]


@pytest.mark.unit
def test_query_by_embeddings_batch_tolerates_per_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, mock_session = _make_store(monkeypatch)

    responses = [
        (True, [_mock_row(doc_id="doc-a")]),
        (False, RuntimeError("driver timeout")),
    ]
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.execute_concurrent",
        _fake_execute_concurrent_unpacking(responses),
    )

    results = store.query_by_embeddings_batch([[0.1], [0.2]], k=1)

    assert len(results) == 2
    assert results[0][0]["doc_id"] == "doc-a"
    assert results[1] == []


@pytest.mark.unit
def test_query_by_embeddings_batch_passes_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, mock_session = _make_store(
        monkeypatch, extra_rag_cfg={"cassandra_batch_query_concurrency": "7"}
    )

    captured_kwargs = {}

    def _fake_execute_concurrent(_session, statements_and_parameters, **kwargs):
        captured_kwargs.update(kwargs)
        statements_and_parameters = list(statements_and_parameters)
        for _statement, params in statements_and_parameters:
            assert isinstance(params, tuple)
        return [(True, []) for _ in statements_and_parameters]

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.execute_concurrent",
        _fake_execute_concurrent,
    )

    store.query_by_embeddings_batch([[0.1], [0.2]], k=1)

    assert captured_kwargs["concurrency"] == 7


@pytest.mark.unit
def test_query_by_embeddings_batch_caps_fetch_size_per_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batched searches should cap fetch_size the same way query_by_embedding does,
    instead of falling back to the driver's default page size (5000 rows)."""
    store, mock_session = _make_store(monkeypatch, extra_rag_cfg={"ann_oversample_factor": "2.0"})

    captured_statements = []

    def _fake_execute_concurrent(_session, statements_and_parameters, **_kwargs):
        statements_and_parameters = list(statements_and_parameters)
        for statement, params in statements_and_parameters:
            assert isinstance(params, tuple)
            captured_statements.append(statement)
        return [(True, []) for _ in statements_and_parameters]

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.execute_concurrent",
        _fake_execute_concurrent,
    )

    store.query_by_embeddings_batch([[0.1], [0.2]], k=3)

    # No metadata filter -> multiplier 1, oversample 2.0 -> fetch_n = 6
    assert len(captured_statements) == 2
    assert all(s.fetch_size == 6 for s in captured_statements)
