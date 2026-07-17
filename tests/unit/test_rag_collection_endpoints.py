"""Unit tests for the RAG collection administration endpoints in app.py.

Covers list/create/delete/reindex/get-settings/update-settings for named RAG
collections (`/api/v1/rag/collections*`), which are all backed by
`nyxgpt.rag.vectorstore_cassandra.CassandraVectorStore`. The handlers do a
lazy `from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore`
import inside each function body, so tests patch the class at its origin
module (`nyxgpt.rag.vectorstore_cassandra.CassandraVectorStore`) rather than
on `nyxgpt.app`, matching the pattern used in test_rag_cache_api.py.
"""

from __future__ import annotations

from configparser import ConfigParser
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def _client() -> TestClient:
    return TestClient(app)


def _patch_store(monkeypatch: pytest.MonkeyPatch, resolver: Any) -> None:
    """Patch CassandraVectorStore so each construction call is routed to `resolver`.

    `resolver` is called with the `collection` kwarg (or "default" if the
    constructor is called with no kwarg at all, mirroring the real class's
    default) and must return the mock/fake instance to use.
    """

    def _ctor(*, collection: str = "default") -> Any:
        return resolver(collection)

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.CassandraVectorStore", _ctor)


# ----------------------------
# GET /rag/collections (list)
# ----------------------------


def test_list_collections_success_with_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: two collections, each with docs and embedding models."""
    temp_store = Mock()
    temp_store.list_collections.return_value = ["alpha", "beta"]

    alpha_store = Mock()
    alpha_store.list_docs.return_value = [
        {"doc_id": "d1", "chunks": 3, "embedding_model": "text-embedding-3"},
        {"doc_id": "d2", "chunks": 2, "embedding_model": "text-embedding-3"},
    ]

    beta_store = Mock()
    beta_store.list_docs.return_value = [
        {"doc_id": "d3", "chunks": 5, "embedding_model": None},
    ]

    stores = {"alpha": alpha_store, "beta": beta_store}

    def resolver(collection: str) -> Any:
        if collection == "default":
            return temp_store
        return stores[collection]

    _patch_store(monkeypatch, resolver)

    with _client() as client:
        resp = client.get("/api/v1/rag/collections")

    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"]: c for c in body["collections"]}
    assert names["alpha"]["doc_count"] == 2
    assert names["alpha"]["chunk_count"] == 5
    assert names["alpha"]["embedding_models"] == ["text-embedding-3"]
    assert names["beta"]["doc_count"] == 1
    assert names["beta"]["chunk_count"] == 5
    assert names["beta"]["embedding_models"] == []
    temp_store.close.assert_called_once()
    alpha_store.close.assert_called_once()
    beta_store.close.assert_called_once()


def test_list_collections_top_level_error_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure in the top-level list_collections() call surfaces as 500."""
    temp_store = Mock()
    temp_store.list_collections.side_effect = RuntimeError("cassandra down")

    _patch_store(monkeypatch, lambda _collection: temp_store)

    with _client() as client:
        resp = client.get("/api/v1/rag/collections")

    assert resp.status_code == 500
    body = resp.json()
    assert "Failed to list collections" in body["error"]["message"]
    assert body["error"]["code"] == "http_error"
    temp_store.close.assert_called_once()


def test_list_collections_per_collection_stats_error_falls_back_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure gathering stats for one collection still returns it with zero stats."""
    temp_store = Mock()
    temp_store.list_collections.return_value = ["broken"]

    broken_store = Mock()
    broken_store.list_docs.side_effect = RuntimeError("table missing")

    _patch_store(
        monkeypatch,
        lambda collection: temp_store if collection == "default" else broken_store,
    )

    with _client() as client:
        resp = client.get("/api/v1/rag/collections")

    assert resp.status_code == 200
    body = resp.json()
    assert body["collections"] == [
        {"name": "broken", "doc_count": 0, "chunk_count": 0, "embedding_models": []}
    ]
    broken_store.close.assert_called_once()


# ----------------------------
# POST /rag/collections (create)
# ----------------------------


def test_create_collection_rejects_default_name() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections", json={"name": "default", "embedding_dim": 768}
        )

    assert resp.status_code == 400
    assert "default" in resp.json()["error"]["message"]


def test_create_collection_rejects_invalid_name_format() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections", json={"name": "bad-name!", "embedding_dim": 768}
        )

    assert resp.status_code == 400
    assert "letters, numbers, and underscores" in resp.json()["error"]["message"]


@pytest.mark.parametrize("embedding_dim", [0, -5, 10001])
def test_create_collection_rejects_invalid_embedding_dim(embedding_dim: int) -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections",
            json={"name": "valid_name", "embedding_dim": embedding_dim},
        )

    assert resp.status_code == 400
    assert "Embedding dimension" in resp.json()["error"]["message"]


def test_create_collection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["other"]

    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections", json={"name": "new_coll", "embedding_dim": 512}
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["collection"] == "new_coll"
    assert body["embedding_dim"] == 512
    store.ensure_schema.assert_called_once_with(embedding_dim=512, collection="new_coll")
    store.close.assert_called_once()


def test_create_collection_already_exists_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["existing_coll"]

    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections", json={"name": "existing_coll", "embedding_dim": 512}
        )

    assert resp.status_code == 409
    assert "already exists" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_create_collection_import_error_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.side_effect = ImportError("cassandra-driver not installed")

    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections", json={"name": "new_coll", "embedding_dim": 512}
        )

    assert resp.status_code == 503
    assert "Cassandra driver not found" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_create_collection_generic_error_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = []
    store.ensure_schema.side_effect = RuntimeError("schema boom")

    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections", json={"name": "new_coll", "embedding_dim": 512}
        )

    assert resp.status_code == 500
    assert "Failed to create collection" in resp.json()["error"]["message"]
    store.close.assert_called_once()


# ----------------------------
# DELETE /rag/collections/{name}
# ----------------------------


def test_delete_collection_rejects_default() -> None:
    with _client() as client:
        resp = client.delete("/api/v1/rag/collections/default")

    assert resp.status_code == 400
    assert "protected" in resp.json()["error"]["message"]


def test_delete_collection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.delete("/api/v1/rag/collections/mycoll")

    assert resp.status_code == 200
    assert resp.json()["collection"] == "mycoll"
    store.truncate.assert_called_once()
    store.close.assert_called_once()


def test_delete_collection_import_error_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.truncate.side_effect = ImportError("cassandra-driver not installed")
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.delete("/api/v1/rag/collections/mycoll")

    assert resp.status_code == 503
    assert "Cassandra driver not found" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_delete_collection_generic_error_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.truncate.side_effect = RuntimeError("truncate boom")
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.delete("/api/v1/rag/collections/mycoll")

    assert resp.status_code == 500
    assert "Failed to clear collection" in resp.json()["error"]["message"]
    store.close.assert_called_once()


# ----------------------------
# POST /rag/collections/{name}/reindex
# ----------------------------


def test_reindex_collection_rejects_default() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections/default/reindex",
            json={"target_embedding_model": "m", "embedding_dim": 768},
        )

    assert resp.status_code == 400
    assert "default" in resp.json()["error"]["message"]


@pytest.mark.parametrize("embedding_dim", [0, -1, 10001])
def test_reindex_collection_rejects_invalid_embedding_dim(embedding_dim: int) -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections/mycoll/reindex",
            json={"target_embedding_model": "m", "embedding_dim": embedding_dim},
        )

    assert resp.status_code == 400
    assert "Embedding dimension" in resp.json()["error"]["message"]


def test_reindex_collection_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = []
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections/missing/reindex",
            json={"target_embedding_model": "m", "embedding_dim": 768},
        )

    # The 404 HTTPException raised inside the outer try block is re-caught by
    # the endpoint's own blanket `except Exception` handler and re-wrapped,
    # so the final response is a 500 -- this is the endpoint's real, observed
    # behavior (not a test assumption).
    assert resp.status_code == 500
    assert "Failed to re-index collection" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_reindex_collection_empty_returns_early(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.get_all_chunks.return_value = []
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections/mycoll/reindex",
            json={"target_embedding_model": "m", "embedding_dim": 768},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Collection is empty, nothing to re-index"
    assert body["chunks_processed"] == 0
    assert body["chunks_total"] == 0
    store.close.assert_called_once()


def test_reindex_collection_embed_failure_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.get_all_chunks.return_value = [{"text": "hello"}]
    _patch_store(monkeypatch, lambda _collection: store)
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings.embed_texts",
        Mock(side_effect=RuntimeError("embedding service down")),
    )

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections/mycoll/reindex",
            json={"target_embedding_model": "m", "embedding_dim": 768},
        )

    assert resp.status_code == 500
    assert "Failed to re-index collection" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_reindex_collection_generic_error_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.get_all_chunks.side_effect = RuntimeError("read failure")
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections/mycoll/reindex",
            json={"target_embedding_model": "m", "embedding_dim": 768},
        )

    assert resp.status_code == 500
    assert "Failed to re-index collection" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_reindex_collection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.get_all_chunks.return_value = [
        {"text": "chunk one"},
        {"text": "chunk two"},
    ]
    _patch_store(monkeypatch, lambda _collection: store)
    monkeypatch.setattr("nyxgpt.rag.embeddings.embed_texts", Mock(return_value=[[0.0]] * 2))

    with _client() as client:
        resp = client.post(
            "/api/v1/rag/collections/mycoll/reindex",
            json={"target_embedding_model": "m", "embedding_dim": 768},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "Successfully re-indexed collection"
    assert body["chunks_processed"] == 2
    assert body["chunks_total"] == 2
    store.close.assert_called_once()


# ----------------------------
# GET /rag/collections/{name}/settings
# ----------------------------


def test_get_collection_settings_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = []
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.get("/api/v1/rag/collections/missing/settings")

    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_get_collection_settings_uses_stored_values(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.get_collection_settings.return_value = {
        "embedding_model": "text-embedding-3",
        "chunk_size": 500,
        "chunk_overlap": 50,
    }
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.get("/api/v1/rag/collections/mycoll/settings")

    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"] == {
        "embedding_model": "text-embedding-3",
        "chunk_size": 500,
        "chunk_overlap": 50,
    }
    store.close.assert_called_once()


def test_get_collection_settings_derives_embedding_model_from_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stored embedding_model: derived from docs when exactly one model is used."""
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.get_collection_settings.return_value = {
        "embedding_model": None,
        "chunk_size": None,
        "chunk_overlap": None,
    }
    store.list_docs.return_value = [
        {"doc_id": "d1", "chunks": 1, "embedding_model": "shared-model"},
        {"doc_id": "d2", "chunks": 1, "embedding_model": "shared-model"},
    ]
    _patch_store(monkeypatch, lambda _collection: store)

    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "1000", "chunk_overlap": "200"}

    with _client() as client:
        # Patch load_config only after the app's lifespan startup (which also
        # calls load_config for unrelated setup) has already run, so this
        # minimal config doesn't break startup.
        monkeypatch.setattr("nyxgpt.app.load_config", lambda *_a, **_k: cfg)
        resp = client.get("/api/v1/rag/collections/mycoll/settings")

    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"]["embedding_model"] == "shared-model"
    assert body["settings"]["chunk_size"] == 1000
    assert body["settings"]["chunk_overlap"] == 200
    store.close.assert_called_once()


def test_get_collection_settings_import_error_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.side_effect = ImportError("cassandra-driver not installed")
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.get("/api/v1/rag/collections/mycoll/settings")

    assert resp.status_code == 503
    assert "Cassandra driver not found" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_get_collection_settings_generic_error_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.get_collection_settings.side_effect = RuntimeError("settings read boom")
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.get("/api/v1/rag/collections/mycoll/settings")

    assert resp.status_code == 500
    assert "Failed to get collection settings" in resp.json()["error"]["message"]
    store.close.assert_called_once()


# ----------------------------
# PUT /rag/collections/{name}/settings
# ----------------------------


def test_update_collection_settings_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = []
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.put(
            "/api/v1/rag/collections/missing/settings",
            json={"embedding_model": "m", "chunk_size": 100, "chunk_overlap": 10},
        )

    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_update_collection_settings_success(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.put(
            "/api/v1/rag/collections/mycoll/settings",
            json={"embedding_model": "new-model", "chunk_size": 800, "chunk_overlap": 80},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["collection"] == "mycoll"
    assert body["settings"] == {
        "embedding_model": "new-model",
        "chunk_size": 800,
        "chunk_overlap": 80,
    }
    store.update_collection_settings.assert_called_once_with(
        embedding_model="new-model", chunk_size=800, chunk_overlap=80
    )
    store.close.assert_called_once()


def test_update_collection_settings_import_error_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.update_collection_settings.side_effect = ImportError("cassandra-driver not installed")
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.put(
            "/api/v1/rag/collections/mycoll/settings",
            json={"embedding_model": "m", "chunk_size": 100, "chunk_overlap": 10},
        )

    assert resp.status_code == 503
    assert "Cassandra driver not found" in resp.json()["error"]["message"]
    store.close.assert_called_once()


def test_update_collection_settings_generic_error_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Mock()
    store.list_collections.return_value = ["mycoll"]
    store.update_collection_settings.side_effect = RuntimeError("update boom")
    _patch_store(monkeypatch, lambda _collection: store)

    with _client() as client:
        resp = client.put(
            "/api/v1/rag/collections/mycoll/settings",
            json={"embedding_model": "m", "chunk_size": 100, "chunk_overlap": 10},
        )

    assert resp.status_code == 500
    assert "Failed to update collection settings" in resp.json()["error"]["message"]
    store.close.assert_called_once()
