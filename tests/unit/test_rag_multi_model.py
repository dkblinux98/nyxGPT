"""Tests for multi-model embedding support in RAG."""

from __future__ import annotations

import pytest

from nyxgpt.rag.embeddings import _embedding_cfg
from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore


@pytest.mark.unit
def test_embedding_cfg_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that model parameter overrides config setting."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["nyxgpt"] = {"default_model": "llama3.1:8b"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_model": "nomic-embed-text",
        "embedding_dim": "768",
    }

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.get_default_model", lambda *_: "llama3.1:8b")
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings.get_ollama_base_url", lambda *_: "http://localhost:11434"
    )

    # Default config
    ecfg1 = _embedding_cfg()
    assert ecfg1.model == "nomic-embed-text"
    assert ecfg1.dimension == 768

    # Override model
    ecfg2 = _embedding_cfg(model="all-minilm:latest")
    assert ecfg2.model == "all-minilm:latest"
    assert ecfg2.dimension == 768

    # Override both
    ecfg3 = _embedding_cfg(model="mxbai-embed-large:latest", dimension=1024)
    assert ecfg3.model == "mxbai-embed-large:latest"
    assert ecfg3.dimension == 1024


@pytest.mark.unit
def test_embedding_cfg_dimension_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that dimension parameter overrides config setting."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["nyxgpt"] = {"default_model": "llama3.1:8b"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_model": "nomic-embed-text",
        "embedding_dim": "768",
    }

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.get_default_model", lambda *_: "llama3.1:8b")
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings.get_ollama_base_url", lambda *_: "http://localhost:11434"
    )

    # Default config
    ecfg1 = _embedding_cfg()
    assert ecfg1.dimension == 768

    # Override dimension
    ecfg2 = _embedding_cfg(dimension=384)
    assert ecfg2.dimension == 384


@pytest.mark.unit
def test_cassandra_vectorstore_collection_table_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that collection parameter affects table name."""
    # Mock Cassandra Cluster to avoid actual connection
    from unittest.mock import MagicMock

    mock_cluster = MagicMock()
    mock_session = MagicMock()
    mock_cluster.connect.return_value = mock_session
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda *args, **kwargs: mock_cluster
    )

    # Default collection
    store1 = CassandraVectorStore(collection="default")
    assert store1.collection == "default"
    assert store1.table_name == "rag_chunks"  # Assumes default cfg.table

    # Custom collection (hyphens are sanitized to underscores for CQL compatibility)
    store2 = CassandraVectorStore(collection="all-minilm")
    assert store2.collection == "all-minilm"
    assert store2.table_name == "rag_chunks_all_minilm"


@pytest.mark.unit
def test_ingest_document_with_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that ingest_document passes collection to vectorstore."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "800",
        "chunk_overlap": "100",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_: cfg)

    # Mock embed_texts to return fake embeddings
    def fake_embed_texts(texts, **kwargs):
        _ = kwargs.get("model", "nomic-embed-text")
        dimension = kwargs.get("dimension", 768)
        return [[0.1] * dimension for _ in texts]

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", fake_embed_texts)

    # Track calls to CassandraVectorStore
    store_calls = []

    class FakeStore:
        def __init__(self, collection="default"):
            store_calls.append({"collection": collection})
            self.collection = collection

        def schema_exists(self):
            return False

        def ensure_schema(self, dim, collection="default"):
            pass

        def get_collection_settings(self):
            return {"embedding_model": None, "chunk_size": None, "chunk_overlap": None}

        def document_needs_update(self, doc_id, doc_hash):
            return True

        def get_document_hash(self, doc_id):
            return None

        def delete_doc(self, doc_id):
            pass

        def upsert_chunks(self, **kwargs):
            store_calls.append({"upsert": kwargs})

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings._embedding_cfg",
        lambda **kw: type(
            "obj",
            (),
            {"model": kw.get("model", "nomic"), "dimension": kw.get("dimension", 768)},
        )(),
    )

    from nyxgpt.rag.rag import ingest_document

    # Ingest with custom collection
    result = ingest_document(
        doc_id="test-doc",
        text="Hello world. This is a test.",
        collection="all-minilm",
        embedding_model="all-minilm:latest",
        embedding_dim=384,
    )

    assert result["chunks_ingested"] > 0
    assert any(call.get("collection") == "all-minilm" for call in store_calls)
    upsert_call = next((call for call in store_calls if "upsert" in call), None)
    assert upsert_call is not None
    assert upsert_call["upsert"]["embedding_model"] == "all-minilm:latest"
    assert upsert_call["upsert"]["embedding_dim"] == 384


def _fake_ingest_store(store_calls: list, *, stored_embedding_model: str | None) -> type:
    """Build a minimal CassandraVectorStore stand-in for ingest_document tests,
    with a configurable `get_collection_settings()` to test the settings
    round-trip (#3461: a collection's configured embedding model must
    actually be used by ingestion, not just displayed).
    """

    class FakeStore:
        def __init__(self, collection="default"):
            self.collection = collection

        def schema_exists(self):
            return True

        def get_collection_settings(self):
            return {
                "embedding_model": stored_embedding_model,
                "chunk_size": None,
                "chunk_overlap": None,
            }

        def document_needs_update(self, doc_id, doc_hash):
            return True

        def get_document_hash(self, doc_id):
            return None

        def get_document_info(self, doc_id):
            return None

        def delete_doc(self, doc_id):
            pass

        def upsert_chunks(self, **kwargs):
            store_calls.append({"upsert": kwargs})

        def close(self):
            pass

    return FakeStore


@pytest.mark.unit
def test_ingest_document_falls_back_to_collection_configured_embedding_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller doesn't pass `embedding_model`, ingest_document must use
    the collection's stored setting (from POST .../collections or PUT
    .../settings) instead of silently using the global config default."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "800", "chunk_overlap": "100"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_: cfg)

    embed_calls = []

    def fake_embed_texts(texts, **kwargs):
        embed_calls.append(kwargs.get("model"))
        return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", fake_embed_texts)
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings._embedding_cfg",
        lambda **kw: type(
            "obj",
            (),
            {"model": kw.get("model") or "nomic", "dimension": kw.get("dimension") or 8},
        )(),
    )

    store_calls: list = []
    monkeypatch.setattr(
        "nyxgpt.rag.rag.CassandraVectorStore",
        _fake_ingest_store(store_calls, stored_embedding_model="mxbai-embed-large:latest"),
    )

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document(doc_id="doc1", text="Some text to chunk", collection="mycoll")

    assert result["chunks_ingested"] > 0
    assert embed_calls == ["mxbai-embed-large:latest"]
    upsert_call = next(call for call in store_calls if "upsert" in call)
    assert upsert_call["upsert"]["embedding_model"] == "mxbai-embed-large:latest"


@pytest.mark.unit
def test_ingest_document_explicit_embedding_model_overrides_collection_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `embedding_model` argument wins over the collection's stored
    setting -- the fallback only applies when the caller doesn't specify one."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "800", "chunk_overlap": "100"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_: cfg)

    embed_calls = []

    def fake_embed_texts(texts, **kwargs):
        embed_calls.append(kwargs.get("model"))
        return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", fake_embed_texts)
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings._embedding_cfg",
        lambda **kw: type(
            "obj",
            (),
            {"model": kw.get("model") or "nomic", "dimension": kw.get("dimension") or 8},
        )(),
    )

    store_calls: list = []
    monkeypatch.setattr(
        "nyxgpt.rag.rag.CassandraVectorStore",
        _fake_ingest_store(store_calls, stored_embedding_model="mxbai-embed-large:latest"),
    )

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document(
        doc_id="doc1",
        text="Some text to chunk",
        collection="mycoll",
        embedding_model="explicit-override:latest",
    )

    assert result["chunks_ingested"] > 0
    assert embed_calls == ["explicit-override:latest"]


@pytest.mark.unit
def test_retrieve_context_with_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that retrieve_context uses collection parameter."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "dedupe": "true",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_: cfg)

    # Mock embed_text
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda *args, **kwargs: [0.1] * 384)
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings._embedding_cfg",
        lambda **kw: type(
            "obj",
            (),
            {"model": kw.get("model", "nomic"), "dimension": kw.get("dimension", 768)},
        )(),
    )

    # Track calls to CassandraVectorStore
    store_calls = []

    class FakeStore:
        def __init__(self, collection="default"):
            store_calls.append({"collection": collection})

        def query_by_embedding(self, emb, k, **kwargs):
            embedding_model = kwargs.get("embedding_model")
            store_calls.append({"query": {"k": k, "embedding_model": embedding_model}})
            return [
                {
                    "text": "result 1",
                    "score": 0.9,
                    "embedding_model": embedding_model,
                    "doc_id": "doc1",
                    "chunk_id": 0,
                },
                {
                    "text": "result 2",
                    "score": 0.8,
                    "embedding_model": embedding_model,
                    "doc_id": "doc2",
                    "chunk_id": 0,
                },
            ]

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    # Retrieve with custom collection
    results = retrieve_context(
        query="test query",
        collection="all-minilm",
        embedding_model="all-minilm:latest",
        embedding_dim=384,
    )

    assert len(results) >= 2
    assert any(call.get("collection") == "all-minilm" for call in store_calls)
    query_call = next((call for call in store_calls if "query" in call), None)
    assert query_call is not None
    assert query_call["query"]["embedding_model"] == "all-minilm:latest"


@pytest.mark.unit
def test_retrieve_context_empty_collection_returns_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for #3464: with a collection selected, retrieval must
    be scoped to *only* that collection's data. Reproduces the reported
    acceptance-test failure directly: an empty collection returned a chunk
    (`smoke-test-doc`) that actually lives in the "default" collection.

    Each collection is backed by its own Cassandra table
    (`CassandraVectorStore.table_name`), so `FakeStore` here keys its data by
    `self.collection` -- exactly like two real, physically separate tables
    would -- rather than mocking `retrieve_context` and merely asserting the
    parameter was forwarded (as the existing API-level tests do).
    """
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "dedupe": "true",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda *args, **kwargs: [0.1] * 768)
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings._embedding_cfg",
        lambda **kw: type(
            "obj",
            (),
            {"model": kw.get("model", "nomic"), "dimension": kw.get("dimension", 768)},
        )(),
    )

    # Data keyed by collection, mirroring physically separate per-collection
    # tables: "default" holds the smoke-test doc, "empty-collection" holds
    # nothing.
    collection_data = {
        "default": [
            {
                "doc_id": "smoke-test-doc",
                "chunk_id": 0,
                "text": "the secret is hunter2",
                "score": 0.95,
            }
        ],
        "empty-collection": [],
    }

    class FakeStore:
        def __init__(self, collection: str = "default") -> None:
            self.collection = collection

        def query_by_embedding(self, emb, k, **kwargs):
            embedding_model = kwargs.get("embedding_model")
            return [
                {**row, "embedding_model": embedding_model}
                for row in collection_data[self.collection]
            ]

        def list_docs(self):
            return [
                {"doc_id": row["doc_id"], "chunks": 1, "embedding_model": None}
                for row in collection_data[self.collection]
            ]

        def close(self) -> None:
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    default_results = retrieve_context(query="what is your secret?", collection="default")
    assert len(default_results) == 1
    assert default_results[0]["doc_id"] == "smoke-test-doc"

    empty_results = retrieve_context(query="what is your secret?", collection="empty-collection")
    assert empty_results == []


@pytest.mark.unit
def test_model_comparison_benchmark_embedding_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test benchmark_embedding_speed function."""
    from nyxgpt.rag.model_compare import benchmark_embedding_speed

    # Mock embed_texts
    call_count = 0

    def fake_embed_texts(texts, **kwargs):
        nonlocal call_count
        call_count += 1
        _ = kwargs.get("model", "nomic-embed-text")
        dimension = kwargs.get("dimension", 768)
        return [[0.1] * dimension for _ in texts]

    monkeypatch.setattr("nyxgpt.rag.model_compare.embed_texts", fake_embed_texts)

    test_texts = ["hello", "world"]
    avg_time = benchmark_embedding_speed("test-model", 768, test_texts, num_runs=2)

    assert avg_time >= 0.0
    assert call_count == 2  # num_runs


@pytest.mark.unit
def test_model_comparison_compare_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test compare_models function."""
    from nyxgpt.rag.model_compare import compare_models

    # Mock benchmark functions
    monkeypatch.setattr(
        "nyxgpt.rag.model_compare.benchmark_embedding_speed",
        lambda model, dim, texts, num_runs=3: 10.0 if "nomic" in model else 5.0,
    )

    models = [
        ("nomic-embed-text", 768, "default"),
        ("all-minilm:latest", 384, "all-minilm"),
    ]

    test_texts = ["test"]
    results = compare_models(models, test_texts)

    assert len(results) == 2
    assert results[0].model_name == "nomic-embed-text"
    assert results[0].dimension == 768
    assert results[0].avg_embedding_time_ms == 10.0
    assert results[1].model_name == "all-minilm:latest"
    assert results[1].dimension == 384
    assert results[1].avg_embedding_time_ms == 5.0
