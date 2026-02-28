"""Unit tests for per-session RAG functionality."""

import pytest

from nyxgpt.sessions import (
    SessionMetadata,
    ensure_meta_defaults,
    load_session_meta,
    meta_file_for,
    save_session_meta,
    session_file_for,
)


@pytest.mark.unit
def test_session_metadata_rag_enabled_default(tmp_path, monkeypatch):
    """Test that rag_enabled defaults to False in new sessions."""
    # Create a minimal config that returns False for RAG
    config_content = "[rag]\nenable_chat_context = false\n"
    config_file = tmp_path / "config.ini"
    config_file.write_text(config_content)

    # Mock config path
    monkeypatch.setenv("HOME", str(tmp_path))

    # Test with empty metadata
    meta: SessionMetadata = {}
    meta = ensure_meta_defaults(meta)

    assert "rag_enabled" in meta
    assert isinstance(meta["rag_enabled"], bool)
    assert meta["rag_enabled"] is False


@pytest.mark.unit
def test_session_metadata_rag_enabled_fallback():
    """Test that rag_enabled falls back to False when config errors occur."""
    # Test with empty metadata (will try to read from config)
    meta: SessionMetadata = {}
    meta = ensure_meta_defaults(meta)

    # Should have rag_enabled field (either from config or False fallback)
    assert "rag_enabled" in meta
    assert isinstance(meta["rag_enabled"], bool)


@pytest.mark.unit
def test_session_metadata_rag_enabled_preserved():
    """Test that rag_enabled is preserved when already set."""
    # Test with rag_enabled=True
    meta: SessionMetadata = {"rag_enabled": True}
    meta = ensure_meta_defaults(meta)
    assert meta["rag_enabled"] is True

    # Test with rag_enabled=False
    meta2: SessionMetadata = {"rag_enabled": False}
    meta2 = ensure_meta_defaults(meta2)
    assert meta2["rag_enabled"] is False


@pytest.mark.unit
def test_session_metadata_rag_enabled_invalid_type():
    """Test that non-boolean rag_enabled values are replaced with defaults."""
    # Test with invalid types
    invalid_values = ["true", 1, None, [], {}]

    for invalid in invalid_values:
        meta: SessionMetadata = {"rag_enabled": invalid}  # type: ignore[typeddict-item]
        meta = ensure_meta_defaults(meta)

        # Should be replaced with a boolean (default False)
        assert isinstance(
            meta.get("rag_enabled"), bool
        ), f"rag_enabled should be bool after fixing invalid value: {invalid}"


# --- Tests for attached_doc_ids (force-include) ---


@pytest.mark.unit
def test_session_metadata_attached_doc_ids_not_present_by_default():
    """Test that attached_doc_ids is not set by default (NotRequired field)."""
    meta: SessionMetadata = {}
    meta = ensure_meta_defaults(meta)
    # attached_doc_ids is not initialized by ensure_meta_defaults (optional)
    assert "attached_doc_ids" not in meta


@pytest.mark.unit
def test_session_metadata_attached_doc_ids_preserved():
    """Test that attached_doc_ids is preserved when already set."""
    doc_ids = ["doc-abc", "doc-xyz"]
    meta: SessionMetadata = {"attached_doc_ids": doc_ids}
    meta = ensure_meta_defaults(meta)
    assert meta.get("attached_doc_ids") == doc_ids


@pytest.mark.unit
def test_session_metadata_attached_doc_ids_roundtrip(tmp_path):
    """Test that attached_doc_ids survives save/load cycle."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sf = session_file_for("test-attach", sessions_dir)
    mf = meta_file_for(sf)

    doc_ids = ["doc-1", "doc-2", "doc-3"]
    meta: dict = {"attached_doc_ids": doc_ids, "rag_enabled": True}
    save_session_meta(mf, meta)

    loaded = load_session_meta(mf)
    assert loaded.get("attached_doc_ids") == doc_ids


@pytest.mark.unit
def test_session_documents_api_list():
    """Test listing attached documents returns correct structure."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app

    with TestClient(app) as client:
        session_name = "test-list-docs"
        resp = client.get(f"/api/v1/sessions/{session_name}/documents")
        # Should return 200 with empty list for new session
        assert resp.status_code == 200
        data = resp.json()
        assert "session" in data
        assert "attached_doc_ids" in data
        assert isinstance(data["attached_doc_ids"], list)


@pytest.mark.unit
def test_session_documents_attach_detach_cycle(tmp_path, monkeypatch):
    """Test attaching and detaching a document from a session."""
    import nyxgpt.app as app_module

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    monkeypatch.setattr(app_module, "get_sessions_dir", lambda cfg: sessions_dir)

    from fastapi.testclient import TestClient

    from nyxgpt.app import app

    with TestClient(app) as client:
        session_name = "cycle-test"

        # Initially no documents
        resp = client.get(f"/api/v1/sessions/{session_name}/documents")
        assert resp.status_code == 200
        assert resp.json()["attached_doc_ids"] == []

        # Attach a document
        resp = client.post(
            f"/api/v1/sessions/{session_name}/documents",
            json={"doc_id": "doc-attach-001"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "doc-attach-001" in data["attached_doc_ids"]

        # List again - should include the attached doc
        resp = client.get(f"/api/v1/sessions/{session_name}/documents")
        assert resp.status_code == 200
        assert "doc-attach-001" in resp.json()["attached_doc_ids"]

        # Detach
        resp = client.delete(f"/api/v1/sessions/{session_name}/documents/doc-attach-001")
        assert resp.status_code == 200
        assert "doc-attach-001" not in resp.json()["attached_doc_ids"]

        # List - should be empty again
        resp = client.get(f"/api/v1/sessions/{session_name}/documents")
        assert resp.status_code == 200
        assert resp.json()["attached_doc_ids"] == []


@pytest.mark.unit
def test_attach_document_idempotent(tmp_path, monkeypatch):
    """Test that attaching the same document twice does not duplicate it."""
    import nyxgpt.app as app_module

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(app_module, "get_sessions_dir", lambda cfg: sessions_dir)

    from fastapi.testclient import TestClient

    from nyxgpt.app import app

    with TestClient(app) as client:
        session_name = "idempotent-test"

        client.post(
            f"/api/v1/sessions/{session_name}/documents",
            json={"doc_id": "doc-idempotent"},
        )
        resp = client.post(
            f"/api/v1/sessions/{session_name}/documents",
            json={"doc_id": "doc-idempotent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should appear only once
        assert data["attached_doc_ids"].count("doc-idempotent") == 1
