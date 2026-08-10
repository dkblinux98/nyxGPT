"""Integration tests for RAG collection scoping on the chat and playground paths.

Regression coverage for #3544: owner acceptance testing observed a chat query
scoped to an empty collection citing a chunk from the *default* collection.
#3463/#3519 (chat) and #3464/#3522 (playground) were previously reviewed with
`retrieve_context` mocked out, which verified the call *wiring* but not
whether the live Cassandra layer (per-collection tables in
`vectorstore_cassandra.py`) actually isolates results end to end.

These tests exercise the real FastAPI app (`nyxgpt.app.app`) against a real
Cassandra instance (skipped via `require_cassandra` when one isn't reachable)
so document ingestion, collection creation, and retrieval all hit genuine
per-collection tables -- the exact layer a mock can't catch a leak in. Only
the embedding vectors and the LLM reply are stubbed, since collection
isolation is a Cassandra/table-routing concern that has nothing to do with
embedding fidelity or model output, and stubbing them keeps these tests from
also requiring a live Ollama server.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

_FAKE_DIM = 8


def _fake_embed_text(text: str, *, model: str | None = None, dimension: int | None = None):
    dim = dimension or _FAKE_DIM
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) * 2 - 1) for i in range(dim)]


def _fake_embed_texts(
    texts: list[str],
    *,
    collect_metrics: bool = False,
    model: str | None = None,
    dimension: int | None = None,
):
    vecs = [_fake_embed_text(t, model=model, dimension=dimension) for t in texts]
    if collect_metrics:
        from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

        metrics = EmbeddingDebugMetrics(
            embedding_time_ms=0.0,
            embedding_model=model or "fake-test-embedder",
            embedding_dim=dimension or _FAKE_DIM,
            num_texts_embedded=len(texts),
            batch_size=len(texts),
        )
        return vecs, metrics
    return vecs


@pytest.fixture(autouse=True)
def _stub_embeddings_and_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub embedding generation and the Ollama LLM call.

    Collection scoping happens entirely in Cassandra table routing
    (`vectorstore_cassandra.py`) and `retrieve_context` -- it doesn't depend
    on embedding semantics or model output, so stubbing these keeps the
    tests deterministic and independent of a live Ollama server while still
    exercising the real vector store.
    """
    import nyxgpt.chat as chat_mod
    import nyxgpt.rag.embeddings as embeddings_mod
    import nyxgpt.rag.rag as rag_mod

    monkeypatch.setattr(embeddings_mod, "embed_text", _fake_embed_text)
    monkeypatch.setattr(embeddings_mod, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(rag_mod, "embed_text", _fake_embed_text)
    monkeypatch.setattr(rag_mod, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(chat_mod, "ollama_chat", lambda *a, **k: "canned reply")

    def _fake_stream_tokens(*a: Any, **k: Any) -> Iterator[str]:
        yield "canned reply"

    monkeypatch.setattr(chat_mod, "ollama_chat_stream_tokens", _fake_stream_tokens)


@pytest.fixture
def api_client(require_cassandra: None) -> TestClient:
    from nyxgpt.app import app

    return TestClient(app)


def _create_collection(api_client: TestClient, name: str) -> None:
    resp = api_client.post(
        "/api/v1/rag/collections", json={"name": name, "embedding_dim": _FAKE_DIM}
    )
    assert resp.status_code == 201, resp.text


def _ingest(
    api_client: TestClient, doc_id: str, text: str, *, collection: str | None = None
) -> None:
    payload: dict[str, Any] = {"doc_id": doc_id, "text": text, "ensure_schema": True}
    if collection is not None:
        payload["collection"] = collection
    resp = api_client.post("/api/v1/rag/ingest", json=payload)
    assert resp.status_code in (200, 201), resp.text


def test_empty_collection_chat_returns_zero_chunks(api_client: TestClient) -> None:
    """A chat scoped to a genuinely empty collection must not cite any chunk.

    Regression for #3544: the owner's report showed a query scoped to an
    empty collection citing a chunk from the default collection instead.
    """
    secret_doc_id = f"test-secret-{uuid.uuid4().hex[:10]}"
    secret_phrase = f"XYZZY-{uuid.uuid4().hex[:8]}-ACCEPT"
    _ingest(api_client, secret_doc_id, f"The secret phrase is {secret_phrase}.")

    empty_collection = f"test_empty_{uuid.uuid4().hex[:10]}"
    _create_collection(api_client, empty_collection)

    resp = api_client.post(
        "/api/v1/chat",
        json={
            "session": f"test-empty-collection-{uuid.uuid4().hex[:8]}",
            "new": True,
            "prompt": f"what is the secret phrase, {secret_phrase}?",
            "rag_enabled": True,
            "rag_filters": {"collection": empty_collection},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rag_chunks"] == []


def test_empty_collection_chat_stream_returns_zero_chunks(api_client: TestClient) -> None:
    """Same guarantee on the streaming chat path used by ChatPane.

    Per #3585 (AC2), a RAG-on turn that retrieves zero rows must still emit
    a `rag_context` event with an empty `chunks` list -- silently omitting
    the event entirely was indistinguishable in the UI from a cited answer,
    letting conversation-history answers masquerade as RAG-grounded ones.
    """
    secret_doc_id = f"test-secret-{uuid.uuid4().hex[:10]}"
    secret_phrase = f"XYZZY-{uuid.uuid4().hex[:8]}-ACCEPT"
    _ingest(api_client, secret_doc_id, f"The secret phrase is {secret_phrase}.")

    empty_collection = f"test_empty_{uuid.uuid4().hex[:10]}"
    _create_collection(api_client, empty_collection)

    resp = api_client.post(
        "/api/v1/chat/stream",
        json={
            "session": f"test-empty-collection-stream-{uuid.uuid4().hex[:8]}",
            "new": True,
            "prompt": f"what is the secret phrase, {secret_phrase}?",
            "rag_enabled": True,
            "rag_filters": {"collection": empty_collection},
        },
        headers={
            "X-Client-Supports-SSE": "true",
            "X-Client-Supports-Structured-Events": "true",
        },
    )
    assert resp.status_code == 200, resp.text
    # The zero-chunk indicator event is emitted (AC2)...
    assert "event: rag_context" in resp.text
    assert '"chunks": []' in resp.text or '"chunks":[]' in resp.text
    # ...but it never cites a chunk from a collection it wasn't scoped to.
    assert secret_doc_id not in resp.text


def test_populated_collections_are_isolated_from_each_other(api_client: TestClient) -> None:
    """Two populated collections must each only surface their own chunks."""
    phrase_a = f"QUUXA-{uuid.uuid4().hex[:8]}"
    phrase_b = f"QUUXB-{uuid.uuid4().hex[:8]}"
    doc_a = f"test-doc-a-{uuid.uuid4().hex[:10]}"
    doc_b = f"test-doc-b-{uuid.uuid4().hex[:10]}"

    collection_a = f"test_coll_a_{uuid.uuid4().hex[:10]}"
    collection_b = f"test_coll_b_{uuid.uuid4().hex[:10]}"
    _create_collection(api_client, collection_a)
    _create_collection(api_client, collection_b)

    _ingest(api_client, doc_a, f"Collection A secret is {phrase_a}.", collection=collection_a)
    _ingest(api_client, doc_b, f"Collection B secret is {phrase_b}.", collection=collection_b)

    resp = api_client.post(
        "/api/v1/chat",
        json={
            "session": f"test-isolation-a-{uuid.uuid4().hex[:8]}",
            "new": True,
            "prompt": f"secret {phrase_a}",
            "rag_enabled": True,
            "rag_filters": {"collection": collection_a},
        },
    )
    assert resp.status_code == 200, resp.text
    chunks_a = resp.json()["rag_chunks"]
    assert len(chunks_a) >= 1
    assert all(c["doc_id"] == doc_a for c in chunks_a)
    assert all(c["collection"] == collection_a for c in chunks_a)

    resp = api_client.post(
        "/api/v1/chat",
        json={
            "session": f"test-isolation-b-{uuid.uuid4().hex[:8]}",
            "new": True,
            "prompt": f"secret {phrase_b}",
            "rag_enabled": True,
            "rag_filters": {"collection": collection_b},
        },
    )
    assert resp.status_code == 200, resp.text
    chunks_b = resp.json()["rag_chunks"]
    assert len(chunks_b) >= 1
    assert all(c["doc_id"] == doc_b for c in chunks_b)
    assert all(c["collection"] == collection_b for c in chunks_b)


def test_playground_query_shares_collection_scoping_with_chat(api_client: TestClient) -> None:
    """The playground `/rag/query` path must use the same isolation as chat.

    #3464's AC requires a single shared scoping implementation between the
    playground and chat paths -- this verifies they didn't fork.
    """
    phrase = f"PLAYGROUND-{uuid.uuid4().hex[:8]}"
    doc_id = f"test-playground-{uuid.uuid4().hex[:10]}"
    collection = f"test_playground_{uuid.uuid4().hex[:10]}"
    _create_collection(api_client, collection)
    _ingest(api_client, doc_id, f"Playground-only secret is {phrase}.", collection=collection)

    # An empty collection query must return zero results.
    empty_collection = f"test_playground_empty_{uuid.uuid4().hex[:10]}"
    _create_collection(api_client, empty_collection)
    resp = api_client.post(
        "/api/v1/rag/query",
        json={"query": phrase, "top_k": 5, "collection": empty_collection},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []

    # The populated collection must return only its own doc.
    resp = api_client.post(
        "/api/v1/rag/query",
        json={"query": phrase, "top_k": 5, "collection": collection},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) >= 1
    assert all(r["doc_id"] == doc_id for r in results)


def test_force_included_attached_doc_respects_collection_scope(api_client: TestClient) -> None:
    """An attached (force-included) doc must not leak across collections.

    Attaching a document to a session force-includes it in RAG retrieval,
    but per #3463's merge this must still respect the request's selected
    collection -- attaching a doc ingested into the default collection must
    not surface it when the chat is scoped to a different, empty collection.
    """
    secret_doc_id = f"test-attach-{uuid.uuid4().hex[:10]}"
    secret_phrase = f"ATTACH-{uuid.uuid4().hex[:8]}"
    _ingest(api_client, secret_doc_id, f"The attached secret is {secret_phrase}.")

    session_name = f"test-attach-session-{uuid.uuid4().hex[:8]}"
    resp = api_client.post(
        f"/api/v1/sessions/{session_name}/documents", json={"doc_id": secret_doc_id}
    )
    assert resp.status_code == 200, resp.text

    empty_collection = f"test_attach_empty_{uuid.uuid4().hex[:10]}"
    _create_collection(api_client, empty_collection)

    resp = api_client.post(
        "/api/v1/chat",
        json={
            "session": session_name,
            "new": True,
            "prompt": "what is the attached secret?",
            "rag_enabled": True,
            "rag_filters": {"collection": empty_collection},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rag_chunks"] == []
