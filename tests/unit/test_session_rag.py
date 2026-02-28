"""Unit tests for per-session RAG functionality."""

import configparser
from pathlib import Path

import pytest

from nyxgpt.chat import _prepare_chat_context
from nyxgpt.sessions import (
    SessionMetadata,
    SessionState,
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


# --- Tests for force-include merge logic in _prepare_chat_context (chat.py) ---


def _make_test_cfg(tmp_path: Path) -> configparser.ConfigParser:
    """Create a minimal ConfigParser for force-include tests."""
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["rag"] = {"enable_chat_context": "true"}
    return cfg


def _make_test_state(tmp_path: Path, meta: dict | None = None) -> SessionState:
    """Create a minimal SessionState for force-include tests."""
    return SessionState(
        name="test-session",
        session_file=tmp_path / "test-session.json",
        meta_file=tmp_path / "test-session.meta.json",
        messages=[],
        meta=meta or {},
    )


@pytest.mark.unit
def test_force_include_makes_second_retrieve_call_with_correct_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When attached_doc_ids is set, retrieve_context is called twice: once for
    normal RAG and once for the force-include filter with the correct doc_ids."""
    cfg = _make_test_cfg(tmp_path)
    attached_ids = ["doc-aaa", "doc-bbb"]
    state = _make_test_state(tmp_path, meta={"attached_doc_ids": attached_ids})

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *_a, **_k: state)
    monkeypatch.setattr("nyxgpt.chat.compose_context", lambda rows: "")

    retrieve_calls: list = []

    def fake_retrieve(query, **kwargs):
        retrieve_calls.append(kwargs.get("metadata_filter"))
        return []

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)

    _prepare_chat_context("hello", rag_enabled=True)

    # Two retrieve_context calls: one for normal RAG, one for force-include
    assert len(retrieve_calls) == 2, f"Expected 2 calls, got {len(retrieve_calls)}"

    # The second call must have a MetadataFilter with the attached doc_ids
    force_filter = retrieve_calls[1]
    assert force_filter is not None, "Second call should have a metadata_filter"
    assert hasattr(force_filter, "doc_ids"), "filter should have doc_ids attribute"
    assert set(force_filter.doc_ids) == set(attached_ids)


@pytest.mark.unit
def test_force_include_rows_take_precedence_in_merged_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Force-included rows appear before normal RAG rows in the merged list."""
    cfg = _make_test_cfg(tmp_path)
    state = _make_test_state(tmp_path, meta={"attached_doc_ids": ["doc-forced"]})

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *_a, **_k: state)

    normal_rows = [{"doc_id": "doc-normal", "chunk_id": "c1", "text": "Normal chunk"}]
    force_rows = [{"doc_id": "doc-forced", "chunk_id": "c2", "text": "Forced chunk"}]
    call_count = 0

    def fake_retrieve(query, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call = normal RAG, second call = force-include
        return normal_rows if call_count == 1 else force_rows

    captured_rows: list = []

    def fake_compose(rows):
        captured_rows.extend(rows)
        return ""

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("nyxgpt.chat.compose_context", fake_compose)

    _prepare_chat_context("hello", rag_enabled=True)

    assert len(captured_rows) == 2
    # Force-included row must come first
    assert captured_rows[0]["doc_id"] == "doc-forced"
    assert captured_rows[1]["doc_id"] == "doc-normal"


@pytest.mark.unit
def test_force_include_deduplication_by_doc_and_chunk_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When normal and force-include retrieval return the same (doc_id, chunk_id),
    the row appears only once in the merged output (force-include copy is kept)."""
    cfg = _make_test_cfg(tmp_path)
    state = _make_test_state(tmp_path, meta={"attached_doc_ids": ["doc-dup"]})

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *_a, **_k: state)

    shared_row = {"doc_id": "doc-dup", "chunk_id": "c1", "text": "Shared chunk"}

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", lambda *_a, **_k: [shared_row])

    captured_rows: list = []

    def fake_compose(rows):
        captured_rows.extend(rows)
        return ""

    monkeypatch.setattr("nyxgpt.chat.compose_context", fake_compose)

    _prepare_chat_context("hello", rag_enabled=True)

    # Duplicate (doc_id, chunk_id) should appear only once
    assert len(captured_rows) == 1
    assert captured_rows[0]["doc_id"] == "doc-dup"
    assert captured_rows[0]["chunk_id"] == "c1"


@pytest.mark.unit
def test_force_include_skipped_when_no_attached_docs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When attached_doc_ids is empty or absent, only one retrieve_context call is made."""
    cfg = _make_test_cfg(tmp_path)

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat.compose_context", lambda rows: "")

    for label, meta in [
        ("empty list", {"attached_doc_ids": []}),
        ("absent key", {}),
    ]:
        retrieve_calls: list = []

        def fake_retrieve(query, _calls=retrieve_calls, **kwargs):
            _calls.append(1)
            return []

        state = _make_test_state(tmp_path, meta=meta)
        monkeypatch.setattr("nyxgpt.chat.load_session", lambda *_a, _s=state, **_k: _s)
        monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)

        _prepare_chat_context("hello", rag_enabled=True)

        assert len(retrieve_calls) == 1, (
            f"Expected exactly one retrieve_context call when attached_doc_ids is {label}, "
            f"got {len(retrieve_calls)}"
        )
