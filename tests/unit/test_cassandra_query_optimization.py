"""Unit tests for Cassandra query optimization (#2683).

Tests cover:
- cassandra_batch_size config field (default, custom, clamping)
- CassandraConfig.batch_size field and wiring through _cassandra_cfg()
- upsert_chunks batches multiple chunks into a single BatchStatement
- upsert_chunks splits into multiple batches once cassandra_batch_size is exceeded
- upsert_chunks splits into multiple batches once the estimated payload size
  nears Cassandra's batch size limit, even under cassandra_batch_size
- upsert_chunks skips batching entirely for a single chunk
- query_by_embedding prepares the ANN statement once and reuses it across calls
- _candidate_fetch_multiplier scales the ANN candidate pool with active filters
"""

from __future__ import annotations

from configparser import ConfigParser
from datetime import datetime
from unittest.mock import Mock

import pytest
from cassandra.query import BatchStatement, BoundStatement, PreparedStatement

from nyxgpt.rag.vectorstore_cassandra import MetadataFilter, _candidate_fetch_multiplier

# ---------------------------------------------------------------------------
# config.get_cassandra_batch_size
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_cassandra_batch_size_default():
    """get_cassandra_batch_size returns 20 when not configured."""
    from nyxgpt.config import get_cassandra_batch_size

    cfg = ConfigParser()
    assert get_cassandra_batch_size(cfg) == 20


@pytest.mark.unit
def test_get_cassandra_batch_size_custom():
    """get_cassandra_batch_size reads from [rag] cassandra_batch_size."""
    from nyxgpt.config import get_cassandra_batch_size

    cfg = ConfigParser()
    cfg.add_section("rag")
    cfg.set("rag", "cassandra_batch_size", "10")
    assert get_cassandra_batch_size(cfg) == 10


@pytest.mark.unit
def test_get_cassandra_batch_size_clamps_to_range():
    """get_cassandra_batch_size clamps out-of-range values to 1-100."""
    from nyxgpt.config import get_cassandra_batch_size

    cfg_low = ConfigParser()
    cfg_low.add_section("rag")
    cfg_low.set("rag", "cassandra_batch_size", "0")
    assert get_cassandra_batch_size(cfg_low) == 1

    cfg_high = ConfigParser()
    cfg_high.add_section("rag")
    cfg_high.set("rag", "cassandra_batch_size", "1000")
    assert get_cassandra_batch_size(cfg_high) == 100


# ---------------------------------------------------------------------------
# CassandraConfig.batch_size
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cassandra_config_batch_size_default():
    """CassandraConfig defaults batch_size to 20."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraConfig

    cfg = CassandraConfig(hosts=["localhost"], port=9042, keyspace="ks", table="tbl")
    assert cfg.batch_size == 20


@pytest.mark.unit
def test_cassandra_cfg_wires_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """_cassandra_cfg() reads cassandra_batch_size from the [rag] config section."""
    from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg

    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_batch_size": "5"}
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    result = _cassandra_cfg()
    assert result.batch_size == 5


# ---------------------------------------------------------------------------
# upsert_chunks batching
# ---------------------------------------------------------------------------


def _prepared_statement_mock() -> Mock:
    """Build a Mock that satisfies BatchStatement.add()'s isinstance checks.

    The real driver's BatchStatement.add() branches on isinstance(statement,
    PreparedStatement) and then calls .bind(...) on it, storing the resulting
    BoundStatement. A bare Mock() doesn't pass the isinstance check and falls
    through to the "raw query string" branch, so both the prepared statement
    and its bound statement need `spec=` to look like the real driver types.
    """
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


def _make_store(monkeypatch: pytest.MonkeyPatch, *, batch_size: str = "20"):
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_keyspace": "test_ks",
        "cassandra_table": "test_tbl",
        "cassandra_batch_size": batch_size,
    }
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


@pytest.mark.unit
def test_upsert_chunks_single_chunk_skips_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-chunk upsert executes directly instead of building a BatchStatement."""
    store, mock_session = _make_store(monkeypatch)

    store.upsert_chunks("doc1", ["chunk1"], [[0.1, 0.2]], [{"key": "value1"}])

    assert mock_session.execute.call_count == 1
    executed = mock_session.execute.call_args[0][0]
    assert not isinstance(executed, BatchStatement)


@pytest.mark.unit
def test_upsert_chunks_batches_multiple_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple chunks under the batch size are grouped into one BatchStatement."""
    store, mock_session = _make_store(monkeypatch, batch_size="20")

    texts = [f"chunk{i}" for i in range(5)]
    embeddings = [[0.1, 0.2] for _ in range(5)]
    metadatas = [{"key": i} for i in range(5)]

    store.upsert_chunks("doc1", texts, embeddings, metadatas)

    assert mock_session.execute.call_count == 1
    executed = mock_session.execute.call_args[0][0]
    assert isinstance(executed, BatchStatement)


@pytest.mark.unit
def test_upsert_chunks_splits_across_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """More chunks than cassandra_batch_size are split into multiple batches."""
    store, mock_session = _make_store(monkeypatch, batch_size="2")

    texts = [f"chunk{i}" for i in range(5)]
    embeddings = [[0.1, 0.2] for _ in range(5)]
    metadatas = [{"key": i} for i in range(5)]

    store.upsert_chunks("doc1", texts, embeddings, metadatas)

    # 5 chunks with batch_size=2 -> batches of [2, 2, 1] = 3 execute() calls
    assert mock_session.execute.call_count == 3


@pytest.mark.unit
def test_upsert_chunks_splits_across_byte_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Large rows are split across batches by estimated size even under cassandra_batch_size.

    Each row here is ~48KB (768-dim embedding + 40KB of text), so packing
    cassandra_batch_size=20 of them into one BatchStatement would produce a
    ~960KB payload - far past what a real Cassandra cluster accepts. The
    _MAX_BATCH_BYTES cap should force a flush after each row.
    """
    store, mock_session = _make_store(monkeypatch, batch_size="20")

    large_text = "x" * 40_000
    embedding = [0.1] * 768
    texts = [large_text for _ in range(4)]
    embeddings = [embedding for _ in range(4)]
    metadatas = [{} for _ in range(4)]

    store.upsert_chunks("doc1", texts, embeddings, metadatas)

    # Each ~48KB row exceeds _MAX_BATCH_BYTES (40KB) on its own, so every row
    # is flushed individually -> 4 execute() calls, none of them batches.
    assert mock_session.execute.call_count == 4
    for call in mock_session.execute.call_args_list:
        assert not isinstance(call[0][0], BatchStatement)


@pytest.mark.unit
def test_upsert_chunks_reuses_prepared_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    """The insert statement is prepared once and reused across upsert_chunks calls."""
    store, mock_session = _make_store(monkeypatch)

    store.upsert_chunks("doc1", ["chunk1"], [[0.1]], [{"k": 1}])
    store.upsert_chunks("doc2", ["chunk2"], [[0.2]], [{"k": 2}])

    assert mock_session.prepare.call_count == 1


# ---------------------------------------------------------------------------
# query_by_embedding read-path tuning
# ---------------------------------------------------------------------------


def _mock_row(**overrides):
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


@pytest.mark.unit
def test_query_by_embedding_prepares_ann_statement_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ANN query is prepared once per instance and reused on subsequent calls."""
    store, mock_session = _make_store(monkeypatch)
    mock_session.execute.return_value = [_mock_row()]

    store.query_by_embedding([0.1, 0.2], k=3)
    store.query_by_embedding([0.3, 0.4], k=3)

    assert mock_session.prepare.call_count == 1
    assert mock_session.execute.call_count == 2


@pytest.mark.unit
def test_query_by_embedding_binds_limit_from_fetch_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_by_embedding binds k * multiplier as the ANN LIMIT / fetch_size."""
    store, mock_session = _make_store(monkeypatch)
    mock_session.execute.return_value = [_mock_row()]

    mock_prepared = mock_session.prepare.return_value
    bound_stmt = Mock()
    mock_prepared.bind.side_effect = None
    mock_prepared.bind.return_value = bound_stmt

    metadata_filter = MetadataFilter(doc_ids=["doc1"], filename="file.txt")
    store.query_by_embedding([0.1, 0.2], k=3, metadata_filter=metadata_filter)

    # 2 active filters -> multiplier = 2 + 2 = 4 -> fetch_n = 12
    bind_args = mock_prepared.bind.call_args[0][0]
    assert bind_args[2] == 12
    assert bound_stmt.fetch_size == 12


# ---------------------------------------------------------------------------
# _candidate_fetch_multiplier
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_candidate_fetch_multiplier_no_filter():
    assert _candidate_fetch_multiplier(None) == 1


@pytest.mark.unit
def test_candidate_fetch_multiplier_empty_filter():
    assert _candidate_fetch_multiplier(MetadataFilter()) == 1


@pytest.mark.unit
def test_candidate_fetch_multiplier_single_filter():
    assert _candidate_fetch_multiplier(MetadataFilter(doc_ids=["a"])) == 3


@pytest.mark.unit
def test_candidate_fetch_multiplier_multiple_filters():
    mf = MetadataFilter(doc_ids=["a"], filename="f", tags=["t"])
    assert _candidate_fetch_multiplier(mf) == 5


@pytest.mark.unit
def test_candidate_fetch_multiplier_caps_at_six():
    mf = MetadataFilter(
        doc_ids=["a"],
        filename="f",
        tags=["t"],
        date_from=datetime(2024, 1, 1),
        date_to=datetime(2024, 12, 31),
    )
    assert _candidate_fetch_multiplier(mf) == 6
