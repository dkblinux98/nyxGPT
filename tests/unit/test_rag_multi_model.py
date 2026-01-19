"""Tests for multi-model embedding support in RAG."""

from __future__ import annotations

import pytest

from mygpt.rag.embeddings import _embedding_cfg
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore


@pytest.mark.unit
def test_embedding_cfg_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that model parameter overrides config setting."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["mygpt"] = {"default_model": "llama3.1:8b"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_model": "nomic-embed-text",
        "embedding_dim": "768",
    }

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_: cfg)
    monkeypatch.setattr(
        "mygpt.rag.embeddings.get_default_model", lambda *_: "llama3.1:8b"
    )
    monkeypatch.setattr(
        "mygpt.rag.embeddings.get_ollama_base_url", lambda *_: "http://localhost:11434"
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
    cfg["mygpt"] = {"default_model": "llama3.1:8b"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_model": "nomic-embed-text",
        "embedding_dim": "768",
    }

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_: cfg)
    monkeypatch.setattr(
        "mygpt.rag.embeddings.get_default_model", lambda *_: "llama3.1:8b"
    )
    monkeypatch.setattr(
        "mygpt.rag.embeddings.get_ollama_base_url", lambda *_: "http://localhost:11434"
    )

    # Default config
    ecfg1 = _embedding_cfg()
    assert ecfg1.dimension == 768

    # Override dimension
    ecfg2 = _embedding_cfg(dimension=384)
    assert ecfg2.dimension == 384


@pytest.mark.unit
def test_cassandra_vectorstore_collection_table_name() -> None:
    """Test that collection parameter affects table name."""
    # Default collection
    store1 = CassandraVectorStore(collection="default")
    assert store1.collection == "default"
    assert store1.table_name == "rag_chunks"  # Assumes default cfg.table

    # Custom collection
    store2 = CassandraVectorStore(collection="all-minilm")
    assert store2.collection == "all-minilm"
    assert store2.table_name == "rag_chunks_all-minilm"


@pytest.mark.unit
def test_ingest_document_with_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that ingest_document passes collection to vectorstore."""
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "800",
        "chunk_overlap": "100",
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_: cfg)

    # Mock embed_texts to return fake embeddings
    def fake_embed_texts(texts, **kwargs):
        _ = kwargs.get("model", "nomic-embed-text")
        dimension = kwargs.get("dimension", 768)
        return [[0.1] * dimension for _ in texts]

    monkeypatch.setattr("mygpt.rag.rag.embed_texts", fake_embed_texts)

    # Track calls to CassandraVectorStore
    store_calls = []

    class FakeStore:
        def __init__(self, collection="default"):
            store_calls.append({"collection": collection})
            self.collection = collection

        def ensure_schema(self, dim, collection="default"):
            pass

        def upsert_chunks(self, **kwargs):
            store_calls.append({"upsert": kwargs})

        def close(self):
            pass

    monkeypatch.setattr("mygpt.rag.rag.CassandraVectorStore", FakeStore)
    monkeypatch.setattr(
        "mygpt.rag.embeddings._embedding_cfg",
        lambda **kw: type(
            "obj",
            (),
            {"model": kw.get("model", "nomic"), "dimension": kw.get("dimension", 768)},
        )(),
    )

    from mygpt.rag.rag import ingest_document

    # Ingest with custom collection
    n = ingest_document(
        doc_id="test-doc",
        text="Hello world. This is a test.",
        collection="all-minilm",
        embedding_model="all-minilm:latest",
        embedding_dim=384,
    )

    assert n > 0
    assert any(call.get("collection") == "all-minilm" for call in store_calls)
    upsert_call = next((call for call in store_calls if "upsert" in call), None)
    assert upsert_call is not None
    assert upsert_call["upsert"]["embedding_model"] == "all-minilm:latest"
    assert upsert_call["upsert"]["embedding_dim"] == 384


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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_: cfg)

    # Mock embed_text
    monkeypatch.setattr("mygpt.rag.rag.embed_text", lambda *args, **kwargs: [0.1] * 384)
    monkeypatch.setattr(
        "mygpt.rag.embeddings._embedding_cfg",
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
                {"text": "result 1", "score": 0.9, "embedding_model": embedding_model},
                {"text": "result 2", "score": 0.8, "embedding_model": embedding_model},
            ]

        def close(self):
            pass

    monkeypatch.setattr("mygpt.rag.rag.CassandraVectorStore", FakeStore)

    from mygpt.rag.rag import retrieve_context

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
def test_model_comparison_benchmark_embedding_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test benchmark_embedding_speed function."""
    from mygpt.rag.model_compare import benchmark_embedding_speed

    # Mock embed_texts
    call_count = 0

    def fake_embed_texts(texts, **kwargs):
        nonlocal call_count
        call_count += 1
        _ = kwargs.get("model", "nomic-embed-text")
        dimension = kwargs.get("dimension", 768)
        return [[0.1] * dimension for _ in texts]

    monkeypatch.setattr("mygpt.rag.model_compare.embed_texts", fake_embed_texts)

    test_texts = ["hello", "world"]
    avg_time = benchmark_embedding_speed("test-model", 768, test_texts, num_runs=2)

    assert avg_time >= 0.0
    assert call_count == 2  # num_runs


@pytest.mark.unit
def test_model_comparison_compare_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test compare_models function."""
    from mygpt.rag.model_compare import compare_models

    # Mock benchmark functions
    monkeypatch.setattr(
        "mygpt.rag.model_compare.benchmark_embedding_speed",
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
