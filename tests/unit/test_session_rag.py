"""Unit tests for per-session RAG functionality."""

import pytest
from nyxgpt.sessions import SessionMetadata, ensure_meta_defaults


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
        assert isinstance(meta.get("rag_enabled"), bool), (
            f"rag_enabled should be bool after fixing invalid value: {invalid}"
        )
