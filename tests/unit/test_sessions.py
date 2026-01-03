from __future__ import annotations

import configparser
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from mygpt import sessions


def _cfg_with_sessions_dir(sessions_dir: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["mygpt"] = {
        "sessions_dir": str(sessions_dir),
        "default_model": "llama3.1:8b",
    }
    return cfg


def test_init_session_creates_files_and_defaults(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")
    sessions_dir = sessions.get_sessions_dir(cfg)

    sf, mf, msgs, meta = sessions.init_session(
        session_name="test-1",
        sessions_dir=sessions_dir,
        new_session=True,
        model="llama3.1:8b",
        system="You are helpful.",
    )

    assert sf.exists(), "session json should be created"
    assert mf.exists(), "meta json should be created"

    # If a system prompt is provided, it is stored as the first message.
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "You are helpful."

    # Meta defaults
    assert meta.get("model") == "llama3.1:8b"
    assert isinstance(meta.get("created_at"), str)
    assert isinstance(meta.get("updated_at"), str)


def test_load_and_save_session_roundtrip(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # Load (creates if missing)
    state = sessions.load_session("s1", cfg, new_session=True)
    assert state.name == "s1"
    assert state.session_file.exists()
    assert state.meta_file.exists()
    assert state.messages == []

    # Add a turn and save
    state.messages.append({"role": "user", "content": "Hello"})
    state.messages.append({"role": "assistant", "content": "Hi!"})
    sessions.save_session(state, cfg)

    # Reload and verify persistence
    state2 = sessions.load_session("s1", cfg)
    assert len(state2.messages) == 2
    assert state2.messages[0]["role"] == "user"
    assert state2.messages[0]["content"] == "Hello"
    assert state2.messages[1]["role"] == "assistant"


def test_sessions_dir_override_is_respected(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    override_dir = tmp_path / "override_sessions"
    state = sessions.load_session("s-override", cfg, sessions_dir_override=str(override_dir), new_session=True)
    assert state.session_file.parent == override_dir

    state.messages.append({"role": "user", "content": "A"})
    state.messages.append({"role": "assistant", "content": "B"})
    sessions.save_session(state, cfg, sessions_dir_override=str(override_dir))

    # Ensure it can be reloaded from override location
    state2 = sessions.load_session("s-override", cfg, sessions_dir_override=str(override_dir))
    assert len(state2.messages) == 2


def test_list_sessions_finds_created_sessions(tmp_path: Path) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")
    sessions_dir = sessions.get_sessions_dir(cfg)

    sessions.init_session("a", sessions_dir, new_session=True, model="llama3.1:8b")
    sessions.init_session("b", sessions_dir, new_session=True, model="llama3.1:8b")

    found = sessions.list_sessions(cfg)
    # list_sessions may return dict rows; normalize to names
    names: set[str] = set()
    for item in found:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and "name" in item:
            names.add(str(item["name"]))
        elif isinstance(item, dict) and "session" in item:
            names.add(str(item["session"]))

    assert "a" in names
    assert "b" in names


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        " ",
        "../escape",
        "..\\escape",
    ],
)
def test_session_name_validation_rejects_path_traversal(tmp_path: Path, bad_name: str) -> None:
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")

    # If your implementation allows these, this test will fail and we can tighten validation.
    with pytest.raises(Exception):
        sessions.load_session(bad_name, cfg, new_session=True)

def test_validate_session_name_rejects_non_string() -> None:
    """validate_session_name should raise ValueError for non-string input."""
    with pytest.raises(ValueError, match="session name must be a string"):
        sessions.validate_session_name(123)  # type: ignore


def test_validate_session_name_rejects_empty_string() -> None:
    """validate_session_name should raise ValueError for empty string."""
    with pytest.raises(ValueError, match="session name cannot be empty"):
        sessions.validate_session_name("")


def test_validate_session_name_rejects_too_long() -> None:
    """validate_session_name should raise ValueError for names > 64 chars."""
    too_long = "a" * 65
    with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
        sessions.validate_session_name(too_long)


def test_validate_session_name_rejects_invalid_chars() -> None:
    """validate_session_name should raise ValueError for invalid characters."""
    with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
        sessions.validate_session_name("invalid@name")


def test_load_session_corrupted_json_file(tmp_path: Path) -> None:
    """load_session should handle corrupted JSON gracefully."""
    cfg = _cfg_with_sessions_dir(tmp_path / "sessions")
    sessions_dir = sessions.get_sessions_dir(cfg)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a corrupted session file
    session_file = sessions_dir / "corrupted.json"
    session_file.write_text("{invalid json content")
    
    # Should handle corrupted file (may return empty or raise specific error)
    # The exact behavior depends on implementation
    try:
        state = sessions.load_session("corrupted", cfg)
        # If it succeeds, messages should be empty or default
        assert isinstance(state.messages, list)
    except (json.JSONDecodeError, ValueError):
        # Also acceptable to raise an error for corrupted files
        pass


def test_save_session_creates_parent_directory(tmp_path: Path) -> None:
    """save_session should create parent directory if it doesn't exist."""
    cfg = _cfg_with_sessions_dir(tmp_path / "new_sessions" / "nested")
    
    # Load (which should create the session)
    state = sessions.load_session("test", cfg, new_session=True)
    state.messages.append({"role": "user", "content": "test"})
    
    # Save should work even if directory structure doesn't exist
    sessions.save_session(state, cfg)
    
    # Verify the session was saved
    assert state.session_file.exists()
    assert state.meta_file.exists()
