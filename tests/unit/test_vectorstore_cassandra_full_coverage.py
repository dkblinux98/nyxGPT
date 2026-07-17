"""Additional unit tests raising rag/vectorstore_cassandra.py to 100% coverage (#3239).

Covers gaps left by test_cassandra_connection_pool.py and
test_cassandra_query_optimization.py:

- `_is_missing_schema_error` with a non-`InvalidRequest` exception
- `_filter_and_score_rows` embedding-model / tags / date-range reject branches
- `CassandraConnectionPool._make_cluster` optional-dependency fallback branches
  (`ExponentialReconnectionPolicy` unavailable, `PoolingOptions` available)
- `CassandraVectorStore._ensure_keyspace_selected` / `_ensure_schema_migrated`
  full flow (idempotent early-return, column migration, and migration failure
  swallowed)
- `upsert_chunks` lazy keyspace selection and empty-batch flush no-op
- `query_by_embeddings_batch` / `list_docs` re-raising non-schema
  `InvalidRequest` errors
- `delete_doc`, `truncate`, `drop_collection`, `list_collections`,
  `get_document_hash`, `get_document_info`, `document_needs_update`,
  `get_all_chunks`, `get_collection_settings`, `update_collection_settings`

The Cassandra driver is fully mocked throughout; no real cluster is used.
"""

from __future__ import annotations

from configparser import ConfigParser
from datetime import datetime
from unittest.mock import Mock

import pytest
from cassandra import InvalidRequest
from cassandra.query import BoundStatement, PreparedStatement

from nyxgpt.rag.vectorstore_cassandra import (
    CassandraConnectionPool,
    MetadataFilter,
    _filter_and_score_rows,
    _is_missing_schema_error,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prepared_statement_mock() -> Mock:
    """Prepared-statement mock that satisfies BatchStatement.add()'s isinstance checks."""
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


def _make_store(monkeypatch: pytest.MonkeyPatch, *, keyspace_ready: bool = True):
    """Build a CassandraVectorStore with a fully mocked driver session."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_keyspace": "test_ks",
        "cassandra_table": "test_tbl",
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
    store._keyspace_ready = keyspace_ready
    if keyspace_ready:
        store._migration_checked = True
    return store, mock_session


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


# ---------------------------------------------------------------------------
# _is_missing_schema_error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_missing_schema_error_false_for_non_invalid_request():
    """Non-InvalidRequest exceptions are never treated as a missing-schema condition."""
    assert _is_missing_schema_error(RuntimeError("does not exist")) is False


# ---------------------------------------------------------------------------
# _filter_and_score_rows
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filter_and_score_rows_skips_embedding_model_mismatch():
    """Rows whose embedding_model differs from the requested filter are dropped."""
    rows = [_mock_row(embedding_model="other-model"), _mock_row(embedding_model="wanted")]
    out, scores = _filter_and_score_rows(rows, k=5, embedding_model="wanted", metadata_filter=None)
    assert len(out) == 1
    assert out[0]["embedding_model"] == "wanted"
    assert scores == [0.9]


@pytest.mark.unit
def test_filter_and_score_rows_skips_non_list_tags():
    """A row whose stored `tags` metadata isn't a list is dropped when tag-filtering."""
    row_bad = _mock_row(metadata='{"tags": "not-a-list"}')
    row_good = _mock_row(metadata='{"tags": ["a", "b"]}')
    mf = MetadataFilter(tags=["a"])

    out, _scores = _filter_and_score_rows(
        [row_bad, row_good], k=5, embedding_model=None, metadata_filter=mf
    )

    assert len(out) == 1
    assert out[0]["metadata"]["tags"] == ["a", "b"]


@pytest.mark.unit
def test_filter_and_score_rows_skips_missing_ingested_at_when_date_filtered():
    """A date filter rejects rows lacking (or with None) `ingested_at`."""
    row_no_attr = Mock(spec=["doc_id", "chunk_id", "text", "metadata", "score"])
    row_no_attr.doc_id = "doc1"
    row_no_attr.chunk_id = 0
    row_no_attr.text = "content"
    row_no_attr.metadata = "{}"
    row_no_attr.score = 0.5

    row_none_ingested = _mock_row(ingested_at=None)
    row_good = _mock_row(ingested_at=datetime(2024, 6, 1))

    mf = MetadataFilter(date_from=datetime(2024, 1, 1))

    out, _scores = _filter_and_score_rows(
        [row_no_attr, row_none_ingested, row_good], k=5, embedding_model=None, metadata_filter=mf
    )

    assert len(out) == 1
    assert out[0]["doc_id"] == "doc1"


@pytest.mark.unit
def test_filter_and_score_rows_rejects_row_after_date_to():
    """A row ingested after `date_to` is excluded from results."""
    row_after = _mock_row(ingested_at=datetime(2024, 12, 31))
    row_within = _mock_row(ingested_at=datetime(2024, 6, 1))
    mf = MetadataFilter(date_to=datetime(2024, 7, 1))

    out, scores = _filter_and_score_rows(
        [row_after, row_within], k=5, embedding_model=None, metadata_filter=mf
    )

    assert len(out) == 1
    assert scores == [0.9]


# ---------------------------------------------------------------------------
# CassandraConnectionPool._make_cluster optional-dependency branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_cluster_falls_back_when_reconnection_policy_unavailable(monkeypatch):
    """If constructing ExponentialReconnectionPolicy fails, Cluster is built without it."""
    import cassandra.policies as policies_module

    monkeypatch.setattr(
        policies_module,
        "ExponentialReconnectionPolicy",
        Mock(side_effect=RuntimeError("unavailable")),
    )

    cfg_kwargs = {"hosts": ["127.0.0.1"], "port": 9042, "keyspace": "ks", "table": "tbl"}
    from nyxgpt.rag.vectorstore_cassandra import CassandraConfig

    pool = CassandraConnectionPool(CassandraConfig(**cfg_kwargs))

    captured_kwargs = {}

    def _fake_cluster(hosts, **kwargs):
        captured_kwargs.update(kwargs)
        return Mock()

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.Cluster", _fake_cluster)

    pool._make_cluster()

    assert "reconnection_policy" not in captured_kwargs


@pytest.mark.unit
def test_make_cluster_sets_pooling_options_when_available(monkeypatch):
    """When PoolingOptions/HostDistance are importable, Cluster is built with pooling_options."""
    import cassandra.cluster as cluster_module
    import cassandra.policies as policies_module

    class _FakePoolingOptions:
        def __init__(self):
            self.core_connections_per_host = {}
            self.max_connections_per_host = {}

    class _FakeHostDistance:
        LOCAL = "LOCAL"

    monkeypatch.setattr(cluster_module, "PoolingOptions", _FakePoolingOptions, raising=False)
    monkeypatch.setattr(policies_module, "HostDistance", _FakeHostDistance, raising=False)

    from nyxgpt.rag.vectorstore_cassandra import CassandraConfig

    cfg = CassandraConfig(hosts=["127.0.0.1"], port=9042, keyspace="ks", table="tbl", pool_size=3)
    pool = CassandraConnectionPool(cfg)

    captured_kwargs = {}

    def _fake_cluster(hosts, **kwargs):
        captured_kwargs.update(kwargs)
        return Mock()

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.Cluster", _fake_cluster)

    pool._make_cluster()

    pooling_opts = captured_kwargs["pooling_options"]
    assert pooling_opts.core_connections_per_host["LOCAL"] == 3
    assert pooling_opts.max_connections_per_host["LOCAL"] == 6


# ---------------------------------------------------------------------------
# _ensure_keyspace_selected / _ensure_schema_migrated
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ensure_keyspace_selected_is_idempotent(monkeypatch):
    """A second call to _ensure_keyspace_selected is a no-op (early return)."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.return_value = []

    store._ensure_keyspace_selected()
    assert store._keyspace_ready is True
    call_count_after_first = mock_session.execute.call_count

    store._ensure_keyspace_selected()
    assert mock_session.execute.call_count == call_count_after_first


@pytest.mark.unit
def test_ensure_keyspace_selected_triggers_schema_migration(monkeypatch):
    """Selecting the keyspace runs the one-time column migration against system_schema."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)

    def _execute(query, *args, **kwargs):
        if "system_schema.columns" in query:
            return []  # no existing columns -> all ALTERs run
        return Mock()

    mock_session.execute.side_effect = _execute

    store._ensure_keyspace_selected()

    executed_queries = [c.args[0] for c in mock_session.execute.call_args_list]
    assert any("USE test_ks" in q for q in executed_queries)
    assert any("system_schema.columns" in q for q in executed_queries)
    assert any("ADD embedding_model" in q for q in executed_queries)
    assert any("ADD embedding_dim" in q for q in executed_queries)
    assert any("ADD doc_hash" in q for q in executed_queries)
    assert any("ADD ingested_at" in q for q in executed_queries)
    assert any("ADD updated_at" in q for q in executed_queries)
    assert any("CREATE INDEX IF NOT EXISTS" in q and "_model_idx" in q for q in executed_queries)
    assert store._migration_checked is True


@pytest.mark.unit
def test_ensure_schema_migrated_skips_existing_columns(monkeypatch):
    """Columns already present in system_schema are not re-added."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)

    existing_row = Mock()
    existing_row.column_name = "embedding_model"

    def _execute(query, *args, **kwargs):
        if "system_schema.columns" in query:
            return [existing_row]
        return Mock()

    mock_session.execute.side_effect = _execute

    store._ensure_keyspace_selected()

    executed_queries = [c.args[0] for c in mock_session.execute.call_args_list]
    assert not any("ADD embedding_model" in q for q in executed_queries)
    assert any("ADD embedding_dim" in q for q in executed_queries)


@pytest.mark.unit
def test_ensure_schema_migrated_swallows_errors(monkeypatch):
    """A failure during migration (e.g. table doesn't exist yet) is swallowed."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)

    def _execute(query, *args, **kwargs):
        if "USE " in query:
            return Mock()
        raise RuntimeError("table does not exist yet")

    mock_session.execute.side_effect = _execute

    # Should not raise despite the migration query failing.
    store._ensure_keyspace_selected()
    assert store._migration_checked is True
    assert store._keyspace_ready is True


@pytest.mark.unit
def test_ensure_schema_migrated_is_idempotent(monkeypatch):
    """A second call to _ensure_schema_migrated is a no-op once already checked."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.return_value = []

    store._ensure_keyspace_selected()
    call_count = mock_session.execute.call_count

    store._ensure_schema_migrated()
    assert mock_session.execute.call_count == call_count


# ---------------------------------------------------------------------------
# upsert_chunks lazy keyspace selection + empty batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_upsert_chunks_ensures_keyspace_selected_when_not_ready(monkeypatch):
    """upsert_chunks lazily selects the keyspace if it hasn't been already."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.return_value = []

    store.upsert_chunks("doc1", ["chunk1"], [[0.1, 0.2]], [{"key": "value"}])

    assert store._keyspace_ready is True
    executed_queries = [
        c.args[0] for c in mock_session.execute.call_args_list if isinstance(c.args[0], str)
    ]
    assert any("USE test_ks" in q for q in executed_queries)


@pytest.mark.unit
def test_upsert_chunks_empty_input_is_a_noop(monkeypatch):
    """upsert_chunks with no chunks flushes nothing (no execute call)."""
    store, mock_session = _make_store(monkeypatch)

    store.upsert_chunks("doc1", [], [], [])

    mock_session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# query_by_embeddings_batch / list_docs - re-raise non-schema InvalidRequest
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_by_embeddings_batch_reraises_non_schema_invalid_request(monkeypatch):
    """A non-missing-schema InvalidRequest during batch query propagates instead of being swallowed."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.side_effect = InvalidRequest("syntax error near SELECT")

    with pytest.raises(InvalidRequest):
        store.query_by_embeddings_batch([[0.1, 0.2]], k=3)


@pytest.mark.unit
def test_list_docs_reraises_non_schema_invalid_request(monkeypatch):
    """A non-missing-schema InvalidRequest during list_docs propagates."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.side_effect = InvalidRequest("syntax error near SELECT")

    with pytest.raises(InvalidRequest):
        store.list_docs()


@pytest.mark.unit
def test_list_docs_returns_empty_on_missing_schema(monkeypatch):
    """A missing-schema InvalidRequest during list_docs is swallowed, returning []."""
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.side_effect = InvalidRequest("keyspace test_ks does not exist")

    assert store.list_docs() == []


# ---------------------------------------------------------------------------
# delete_doc / truncate / drop_collection / list_collections
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_delete_doc_selects_keyspace_and_deletes(monkeypatch):
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.return_value = []

    store.delete_doc("doc1")

    assert store._keyspace_ready is True
    delete_call = mock_session.execute.call_args_list[-1]
    assert "DELETE FROM" in str(delete_call.args[0])
    assert delete_call.args[1] == ("doc1",)


@pytest.mark.unit
def test_truncate_selects_keyspace_and_truncates(monkeypatch):
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.return_value = []

    store.truncate()

    assert store._keyspace_ready is True
    truncate_call = mock_session.execute.call_args_list[-1]
    assert "TRUNCATE" in str(truncate_call.args[0])


@pytest.mark.unit
def test_drop_collection_rejects_default_collection(monkeypatch):
    store, _mock_session = _make_store(monkeypatch)

    with pytest.raises(ValueError, match="Cannot drop the default collection"):
        store.drop_collection()


@pytest.mark.unit
def test_drop_collection_drops_non_default_collection(monkeypatch):
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )
    mock_session.execute.return_value = []

    store = CassandraVectorStore(collection="other")
    store._keyspace_ready = False

    store.drop_collection()

    assert store._keyspace_ready is True
    drop_call = mock_session.execute.call_args_list[-1]
    assert "DROP TABLE IF EXISTS test_tbl_other" in str(drop_call.args[0])


@pytest.mark.unit
def test_list_collections_parses_default_and_named_tables(monkeypatch):
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)

    row_default = Mock(table_name="test_tbl")
    row_other = Mock(table_name="test_tbl_other")
    row_unrelated = Mock(table_name="unrelated_table")

    def _execute(query, *args, **kwargs):
        if "system_schema.tables" in str(query):
            return [row_default, row_other, row_unrelated]
        return []

    mock_session.execute.side_effect = _execute

    collections = store.list_collections()

    assert collections == ["default", "other"]
    assert store._keyspace_ready is True


# ---------------------------------------------------------------------------
# get_document_hash / get_document_info / document_needs_update
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_document_hash_selects_keyspace_and_returns_hash(monkeypatch):
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)

    row = Mock()
    row.doc_hash = "abc123"
    result_set = Mock()
    result_set.one.return_value = row

    def _execute(query, *args, **kwargs):
        if "SELECT doc_hash" in str(query):
            return result_set
        return []

    mock_session.execute.side_effect = _execute

    assert store.get_document_hash("doc1") == "abc123"
    assert store._keyspace_ready is True


@pytest.mark.unit
def test_get_document_hash_returns_none_when_hash_is_none(monkeypatch):
    store, mock_session = _make_store(monkeypatch)

    row = Mock()
    row.doc_hash = None
    result_set = Mock()
    result_set.one.return_value = row
    mock_session.execute.return_value = result_set

    assert store.get_document_hash("doc1") is None


@pytest.mark.unit
def test_get_document_hash_returns_none_when_no_row(monkeypatch):
    store, mock_session = _make_store(monkeypatch)

    result_set = Mock()
    result_set.one.return_value = None
    mock_session.execute.return_value = result_set

    assert store.get_document_hash("doc1") is None


@pytest.mark.unit
def test_get_document_info_returns_none_when_no_rows(monkeypatch):
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)
    mock_session.execute.return_value = []

    assert store.get_document_info("doc1") is None
    assert store._keyspace_ready is True


@pytest.mark.unit
def test_get_document_info_aggregates_chunks(monkeypatch):
    store, mock_session = _make_store(monkeypatch)

    row1 = Mock()
    row1.doc_id = "doc1"
    row1.doc_hash = "hash1"
    row1.ingested_at = datetime(2024, 1, 1)
    row1.updated_at = datetime(2024, 1, 2)
    row1.embedding_model = "nomic"

    row2 = Mock()
    row2.doc_id = "doc1"
    row2.doc_hash = "hash1"
    row2.ingested_at = datetime(2024, 1, 1)
    row2.updated_at = datetime(2024, 1, 2)
    row2.embedding_model = "nomic"

    mock_session.execute.return_value = [row1, row2]

    info = store.get_document_info("doc1")

    assert info == {
        "doc_id": "doc1",
        "doc_hash": "hash1",
        "ingested_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-02T00:00:00",
        "chunks": 2,
        "embedding_model": "nomic",
    }


@pytest.mark.unit
def test_document_needs_update_true_when_doc_missing(monkeypatch):
    store, mock_session = _make_store(monkeypatch)
    result_set = Mock()
    result_set.one.return_value = None
    mock_session.execute.return_value = result_set

    assert store.document_needs_update("doc1", "newhash") is True


@pytest.mark.unit
def test_document_needs_update_true_when_hash_differs(monkeypatch):
    store, mock_session = _make_store(monkeypatch)
    row = Mock()
    row.doc_hash = "oldhash"
    result_set = Mock()
    result_set.one.return_value = row
    mock_session.execute.return_value = result_set

    assert store.document_needs_update("doc1", "newhash") is True


@pytest.mark.unit
def test_document_needs_update_false_when_hash_matches(monkeypatch):
    store, mock_session = _make_store(monkeypatch)
    row = Mock()
    row.doc_hash = "samehash"
    result_set = Mock()
    result_set.one.return_value = row
    mock_session.execute.return_value = result_set

    assert store.document_needs_update("doc1", "samehash") is False


# ---------------------------------------------------------------------------
# get_all_chunks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_all_chunks_returns_parsed_rows(monkeypatch):
    store, mock_session = _make_store(monkeypatch, keyspace_ready=False)

    row = Mock()
    row.doc_id = "doc1"
    row.chunk_id = 0
    row.text = "hello"
    row.metadata = "{}"
    row.embedding = [0.1, 0.2]
    row.embedding_model = "nomic"
    row.embedding_dim = 768
    row.doc_hash = "hash1"
    row.ingested_at = datetime(2024, 1, 1)
    row.updated_at = datetime(2024, 1, 2)

    mock_session.execute.return_value = [row]

    chunks = store.get_all_chunks()

    assert store._keyspace_ready is True
    assert chunks == [
        {
            "doc_id": "doc1",
            "chunk_id": 0,
            "text": "hello",
            "metadata": "{}",
            "embedding": [0.1, 0.2],
            "embedding_model": "nomic",
            "embedding_dim": 768,
            "doc_hash": "hash1",
            "ingested_at": datetime(2024, 1, 1),
            "updated_at": datetime(2024, 1, 2),
        }
    ]


@pytest.mark.unit
def test_get_all_chunks_handles_none_embedding(monkeypatch):
    store, mock_session = _make_store(monkeypatch)

    row = Mock()
    row.doc_id = "doc1"
    row.chunk_id = 0
    row.text = "hello"
    row.metadata = "{}"
    row.embedding = None
    row.embedding_model = None
    row.embedding_dim = None
    row.doc_hash = None
    row.ingested_at = None
    row.updated_at = None

    mock_session.execute.return_value = [row]

    chunks = store.get_all_chunks()
    assert chunks[0]["embedding"] is None


# ---------------------------------------------------------------------------
# Collection settings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_collection_settings_returns_defaults_when_no_row(monkeypatch):
    store, mock_session = _make_store(monkeypatch)

    result_set = Mock()
    result_set.one.return_value = None
    mock_session.execute.return_value = result_set

    settings = store.get_collection_settings()
    assert settings == {"embedding_model": None, "chunk_size": None, "chunk_overlap": None}


@pytest.mark.unit
def test_get_collection_settings_returns_saved_values(monkeypatch):
    store, mock_session = _make_store(monkeypatch)

    row = Mock()
    row.embedding_model = "nomic"
    row.chunk_size = 512
    row.chunk_overlap = 64
    result_set = Mock()
    result_set.one.return_value = row
    mock_session.execute.return_value = result_set

    settings = store.get_collection_settings()
    assert settings == {"embedding_model": "nomic", "chunk_size": 512, "chunk_overlap": 64}


@pytest.mark.unit
def test_update_collection_settings_creates_table_and_inserts(monkeypatch):
    store, mock_session = _make_store(monkeypatch)
    mock_session.execute.return_value = Mock()

    store.update_collection_settings(embedding_model="nomic", chunk_size=512, chunk_overlap=64)

    insert_call = mock_session.execute.call_args_list[-1]
    assert "INSERT INTO test_ks.collection_settings" in str(insert_call.args[0])
    assert insert_call.args[1] == ["default", "nomic", 512, 64]
