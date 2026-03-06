from __future__ import annotations

from pathlib import Path

import pytest

from nyxgpt import sessions
from nyxgpt.cli import cli

pytestmark = pytest.mark.unit


def test_sessions_export_markdown_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test exporting a session to markdown format (stdout)."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create a test session
    session_file = sessions.session_file_for("test-session", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    metadata = {
        "title": "Test Session",
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T12:05:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Run export command
    exit_code = cli(
        [
            "sessions",
            "export",
            "test-session",
            "--sessions-dir",
            str(sessions_dir),
            "--format",
            "markdown",
        ]
    )

    assert exit_code == 0

    captured = capsys.readouterr()
    assert "# Test Session" in captured.out
    assert "## User" in captured.out
    assert "Hello" in captured.out
    assert "## Assistant" in captured.out
    assert "Hi there!" in captured.out


def test_sessions_export_json_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test exporting a session to JSON format (stdout)."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-json", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "Test message"},
    ]
    metadata = {
        "title": "JSON Test",
        "created_at": "2024-01-01T12:00:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(
        [
            "sessions",
            "export",
            "test-json",
            "--sessions-dir",
            str(sessions_dir),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0

    captured = capsys.readouterr()
    assert '"name": "test-json"' in captured.out
    assert '"title": "JSON Test"' in captured.out
    assert '"Test message"' in captured.out


def test_sessions_export_html_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test exporting a session to HTML format (stdout)."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-html", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "HTML test"},
    ]
    metadata = {
        "title": "HTML Session",
        "created_at": "2024-01-01T12:00:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(
        [
            "sessions",
            "export",
            "test-html",
            "--sessions-dir",
            str(sessions_dir),
            "--format",
            "html",
        ]
    )

    assert exit_code == 0

    captured = capsys.readouterr()
    assert "<!DOCTYPE html>" in captured.out
    assert "<title>HTML Session</title>" in captured.out
    assert "HTML test" in captured.out


def test_sessions_export_to_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test exporting a session to a file."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-file", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "File export test"},
    ]
    metadata = {
        "title": "File Export",
        "created_at": "2024-01-01T12:00:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    output_file = tmp_path / "export.md"

    exit_code = cli(
        [
            "sessions",
            "export",
            "test-file",
            "--sessions-dir",
            str(sessions_dir),
            "--format",
            "markdown",
            "--output",
            str(output_file),
        ]
    )

    assert exit_code == 0

    # Verify file was created
    assert output_file.exists()
    content = output_file.read_text()
    assert "# File Export" in content
    assert "File export test" in content

    # Verify confirmation message
    captured = capsys.readouterr()
    assert "Exported session 'test-file'" in captured.out
    assert str(output_file) in captured.out


def test_sessions_export_nonexistent_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test export fails gracefully for nonexistent session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    exit_code = cli(["sessions", "export", "nonexistent", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 1

    captured = capsys.readouterr()
    assert "No such session" in captured.err


def test_sessions_export_missing_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Test export fails when session name is not provided."""
    exit_code = cli(["sessions", "export"])

    assert exit_code == 2

    captured = capsys.readouterr()
    assert "session name required" in captured.err


def test_sessions_export_default_format_is_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test that default export format is markdown."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-default", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Default format test"}]
    metadata = {"title": "Default Format", "created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Don't specify --format
    exit_code = cli(["sessions", "export", "test-default", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0

    captured = capsys.readouterr()
    # Should be markdown format by default
    assert "# Default Format" in captured.out
    assert "## User" in captured.out


def test_sessions_export_file_write_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test export fails gracefully when file write fails."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-write-error", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"title": "Write Error Test", "created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Try to write to a read-only directory
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o444)  # Read-only

    output_file = readonly_dir / "export.md"

    exit_code = cli(
        [
            "sessions",
            "export",
            "test-write-error",
            "--sessions-dir",
            str(sessions_dir),
            "--output",
            str(output_file),
        ]
    )

    # Should return exit code 1 for write error
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "ERROR: Failed to write to" in captured.err
    assert str(output_file) in captured.err

    # Cleanup
    readonly_dir.chmod(0o755)


def test_sessions_stats_displays_message_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test stats command displays message counts."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-stats", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm good!"},
    ]
    metadata = {
        "title": "Stats Test Session",
        "summary": "A test session for statistics",
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T13:00:00",
        "model": "llama3.1:8b",
        "rag_enabled": True,
        "pinned": True,
        "tags": ["test", "stats"],
        "token_estimate": 100,
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "stats", "test-stats", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0

    captured = capsys.readouterr()
    # Check header
    assert "Session Statistics: test-stats" in captured.out

    # Check title and summary
    assert "Title: Stats Test Session" in captured.out
    assert "Summary: A test session for statistics" in captured.out

    # Check message counts
    assert "Message Counts:" in captured.out
    assert "Total messages: 5" in captured.out
    assert "User messages: 2" in captured.out
    assert "Assistant messages: 2" in captured.out
    assert "System messages: 1" in captured.out

    # Check token estimate
    assert "Token Estimate:" in captured.out
    assert "Approximate tokens: 100" in captured.out

    # Check timestamps
    assert "Session Age & Activity:" in captured.out
    assert "Created: 2024-01-01T12:00:00" in captured.out
    assert "Last updated: 2024-01-01T13:00:00" in captured.out

    # Check configuration
    assert "Configuration:" in captured.out
    assert "Model: llama3.1:8b" in captured.out
    assert "RAG: Enabled" in captured.out
    assert "Pinned: Yes" in captured.out
    assert "Tags: test, stats" in captured.out


def test_sessions_stats_handles_minimal_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test stats command with minimal metadata."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("minimal-stats", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "Test"},
    ]
    # Minimal metadata
    metadata = {
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T12:00:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "stats", "minimal-stats", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0

    captured = capsys.readouterr()
    # Should display even with minimal metadata
    assert "Session Statistics: minimal-stats" in captured.out
    assert "Total messages: 1" in captured.out
    assert "User messages: 1" in captured.out
    assert "Model: Unknown" in captured.out
    assert "RAG: Disabled" in captured.out
    assert "Pinned: No" in captured.out


def test_sessions_stats_nonexistent_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test stats fails gracefully for nonexistent session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    exit_code = cli(["sessions", "stats", "nonexistent", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 1

    captured = capsys.readouterr()
    assert "No such session: nonexistent" in captured.err


def test_sessions_stats_missing_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Test stats fails when session name is not provided."""
    exit_code = cli(["sessions", "stats"])

    assert exit_code == 2

    captured = capsys.readouterr()
    assert "session name is required" in captured.err


def test_sessions_stats_calculates_token_estimate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test stats calculates token estimate if not in metadata."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("token-test", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "This is a test message with some content"},
        {"role": "assistant", "content": "This is another message with more content"},
    ]
    # No token_estimate in metadata
    metadata = {
        "created_at": "2024-01-01T12:00:00",
        "updated_at": "2024-01-01T12:00:00",
    }

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "stats", "token-test", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0

    captured = capsys.readouterr()
    # Should calculate and display token estimate
    assert "Token Estimate:" in captured.out
    assert "Approximate tokens:" in captured.out
    # Token count should be greater than 0
    assert "Approximate tokens: 0" not in captured.out


# Session list command tests
def test_sessions_list_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test listing sessions when none exist."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    exit_code = cli(["sessions", "list", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert f"No sessions found in {sessions_dir}" in captured.out


def test_sessions_list_with_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test listing sessions displays session info."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create multiple test sessions
    for name in ["session1", "session2", "session3"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": f"Test message for {name}"}]
        metadata = {
            "title": f"Title for {name}",
            "created_at": "2024-01-01T12:00:00",
            "updated_at": "2024-01-01T12:00:00",
            "tags": ["test", name],
            "pinned": name == "session1",
        }

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "list", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    # Verify all sessions are listed
    assert "session1" in captured.out
    assert "session2" in captured.out
    assert "session3" in captured.out
    # Verify pinned session has the pin indicator
    assert "📌 session1" in captured.out
    # Verify titles are shown
    assert "Title for session1" in captured.out


# Session show command tests
def test_sessions_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test showing session details."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-show", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"title": "Test Session", "created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "show", "test-show", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Session: test-show" in captured.out
    assert "Messages: 1" in captured.out
    assert '"title": "Test Session"' in captured.out


def test_sessions_show_nonexistent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test show fails for nonexistent session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    exit_code = cli(["sessions", "show", "nonexistent", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No such session: nonexistent" in captured.out


def test_sessions_show_missing_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Test show fails when name is missing."""
    exit_code = cli(["sessions", "show"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "session name is required" in captured.err


# Session delete command tests
def test_sessions_delete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test deleting a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-delete", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "delete", "test-delete", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Deleted session: test-delete" in captured.out
    # Verify session file is gone
    assert not session_file.exists()


def test_sessions_delete_nonexistent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test delete fails for nonexistent session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    exit_code = cli(["sessions", "delete", "nonexistent", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No such session: nonexistent" in captured.out


def test_sessions_delete_missing_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Test delete fails when name is missing."""
    exit_code = cli(["sessions", "delete"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "session name is required" in captured.err


# Session rename command tests
def test_sessions_rename(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test renaming a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("old-name", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(
        [
            "sessions",
            "rename",
            "old-name",
            "new-name",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Renamed session: old-name -> new-name" in captured.out
    # Verify old name is gone and new name exists
    assert not session_file.exists()
    new_file = sessions.session_file_for("new-name", sessions_dir)
    assert new_file.exists()


def test_sessions_rename_missing_names(capsys: pytest.CaptureFixture[str]) -> None:
    """Test rename fails when names are missing."""
    exit_code = cli(["sessions", "rename", "old-name"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "old and new names required" in captured.err


# Session pin/unpin command tests
def test_sessions_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test pinning a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-pin", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"created_at": "2024-01-01T12:00:00", "pinned": False}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "pin", "test-pin", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Pinned session: test-pin" in captured.out


def test_sessions_unpin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test unpinning a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-unpin", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"created_at": "2024-01-01T12:00:00", "pinned": True}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(["sessions", "unpin", "test-unpin", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Unpinned session: test-unpin" in captured.out


def test_sessions_pin_missing_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Test pin fails when name is missing."""
    exit_code = cli(["sessions", "pin"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "session name is required" in captured.err


# Session tag-add/tag-rm command tests
def test_sessions_tag_add(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test adding tags to a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-tag", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"created_at": "2024-01-01T12:00:00", "tags": ["existing"]}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(
        [
            "sessions",
            "tag-add",
            "test-tag",
            "new-tag",
            "another-tag",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Updated tags for session: test-tag" in captured.out


def test_sessions_tag_rm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test removing tags from a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-tag-rm", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"created_at": "2024-01-01T12:00:00", "tags": ["tag1", "tag2", "tag3"]}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Need at least 2 tags after session name due to argparse structure (new_name, *extras)
    exit_code = cli(
        [
            "sessions",
            "tag-rm",
            "test-tag-rm",
            "tag2",
            "tag3",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Updated tags for session: test-tag-rm" in captured.out


def test_sessions_tag_add_missing_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Test tag-add fails when name is missing."""
    exit_code = cli(["sessions", "tag-add"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "session name is required" in captured.err


def test_sessions_tag_add_missing_tags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test tag-add fails when tags are missing."""
    exit_code = cli(["sessions", "tag-add", "test-session"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one tag is required" in captured.err


# Session title command tests
def test_sessions_title(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test setting session title."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-title", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(
        [
            "sessions",
            "title",
            "test-title",
            "New Title",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Set title for session: test-title" in captured.out


def test_sessions_title_missing_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test title fails when args are missing."""
    exit_code = cli(["sessions", "title", "test-session"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "session name and title required" in captured.err


# Session search command tests
def test_sessions_search(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test searching sessions."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-search", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [
        {"role": "user", "content": "This is a searchable message"},
        {"role": "assistant", "content": "Here is the response"},
    ]
    metadata = {"title": "Search Test", "created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    # Mock input to avoid pagination prompt
    monkeypatch.setattr("builtins.input", lambda _: "q")

    exit_code = cli(["sessions", "search", "searchable", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Found" in captured.out
    assert "searchable" in captured.out.lower()


def test_sessions_search_no_results(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test search with no results."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    exit_code = cli(["sessions", "search", "nonexistent", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No results found" in captured.out


def test_sessions_search_missing_query(capsys: pytest.CaptureFixture[str]) -> None:
    """Test search fails when query is missing."""
    exit_code = cli(["sessions", "search"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "search query is required" in captured.err


# Session merge command tests
def test_sessions_merge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test merging sessions."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create two sessions to merge
    for name in ["merge1", "merge2"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": f"Message from {name}"}]
        metadata = {"created_at": "2024-01-01T12:00:00"}

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    exit_code = cli(
        [
            "sessions",
            "merge",
            "merged-output",
            "merge1",
            "merge2",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    # Verify success message
    assert "merged" in captured.out.lower() or "Merged" in captured.out


def test_sessions_merge_missing_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test merge fails when args are missing."""
    exit_code = cli(["sessions", "merge", "output"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one input session" in captured.err


# Batch delete command tests
def test_batch_delete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch deleting sessions."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create multiple sessions
    for name in ["batch1", "batch2", "batch3"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": "Test"}]
        metadata = {"created_at": "2024-01-01T12:00:00"}

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    # Need at least 3 args after action because argparse fills name, new_name, then extras
    # With 3 args, only the last one (batch3) is in extras
    exit_code = cli(
        [
            "sessions",
            "batch-delete",
            "batch1",
            "batch2",
            "batch3",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Deleted 1 session(s)" in captured.out


def test_batch_delete_missing_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch-delete fails when args are missing."""
    exit_code = cli(["sessions", "batch-delete"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one session name is required" in captured.err


# Batch tag operations tests
def test_batch_tag_add(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch adding tags."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create sessions
    for name in ["tag1", "tag2", "tag3"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": "Test"}]
        metadata = {"created_at": "2024-01-01T12:00:00", "tags": []}

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    # Tags are passed as space-separated string in 'name', then session names
    # Need at least 2 session names (new_name gets 1st, extras gets rest)
    exit_code = cli(
        [
            "sessions",
            "batch-tag-add",
            "newtag anothertag",
            "tag1",
            "tag2",
            "tag3",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Added tags to 2 session(s)" in captured.out


def test_batch_tag_rm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch removing tags."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create sessions with tags
    for name in ["tagr1", "tagr2", "tagr3"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": "Test"}]
        metadata = {"created_at": "2024-01-01T12:00:00", "tags": ["oldtag", "keep"]}

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    # Tags are passed as space-separated string in 'name', then session names
    exit_code = cli(
        [
            "sessions",
            "batch-tag-rm",
            "oldtag",
            "tagr1",
            "tagr2",
            "tagr3",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Removed tags from 2 session(s)" in captured.out


def test_batch_tag_add_missing_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch-tag-add fails when args are missing."""
    exit_code = cli(["sessions", "batch-tag-add"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one tag is required" in captured.err


# Batch export tests
def test_batch_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch exporting sessions."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    output_dir = tmp_path / "exports"
    output_dir.mkdir()

    # Create sessions
    for name in ["exp1", "exp2", "exp3"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": "Test"}]
        metadata = {"title": f"Export {name}", "created_at": "2024-01-01T12:00:00"}

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    # Need at least 3 session names (only last one ends up in extras)
    exit_code = cli(
        [
            "sessions",
            "batch-export",
            "exp1",
            "exp2",
            "exp3",
            "--sessions-dir",
            str(sessions_dir),
            "--output",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Exported 1 session(s)" in captured.out


def test_batch_export_missing_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch-export fails when output directory is missing."""
    # Need at least 3 positional args for extras to have values
    exit_code = cli(["sessions", "batch-export", "s1", "s2", "s3", "--sessions-dir", str(tmp_path)])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "--output directory is required" in captured.err


# Batch pin/unpin tests
def test_batch_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch pinning sessions."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create sessions
    for name in ["pin1", "pin2", "pin3"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": "Test"}]
        metadata = {"created_at": "2024-01-01T12:00:00", "pinned": False}

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    # Need at least 3 session names (only last one ends up in extras)
    exit_code = cli(
        [
            "sessions",
            "batch-pin",
            "pin1",
            "pin2",
            "pin3",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Pinned 1 session(s)" in captured.out


def test_batch_unpin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch unpinning sessions."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create sessions
    for name in ["unpin1", "unpin2", "unpin3"]:
        session_file = sessions.session_file_for(name, sessions_dir)
        meta_file = sessions.meta_file_for(session_file)

        messages = [{"role": "user", "content": "Test"}]
        metadata = {"created_at": "2024-01-01T12:00:00", "pinned": True}

        sessions.save_session_messages(session_file, messages)
        sessions.save_session_meta(meta_file, metadata)

    # Need at least 3 session names (only last one ends up in extras)
    exit_code = cli(
        [
            "sessions",
            "batch-unpin",
            "unpin1",
            "unpin2",
            "unpin3",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Unpinned 1 session(s)" in captured.out


def test_batch_pin_missing_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test batch-pin fails when args are missing."""
    exit_code = cli(["sessions", "batch-pin"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one session name is required" in captured.err


# Info command tests
def test_info_command(capsys: pytest.CaptureFixture[str]) -> None:
    """Test info command displays configuration."""
    exit_code = cli(["info"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "nyxGPT OK" in captured.out
    assert "Ollama base_url:" in captured.out
    assert "Default model:" in captured.out


# Models command tests (with mocking for Ollama API)
def test_models_list(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Test models list command."""
    # Mock the models.list_models function
    import nyxgpt.models as models_mod

    def mock_list_models():
        return [
            {
                "name": "llama3.1:8b",
                "size": 4800000000,
                "modified_at": "2024-01-01T12:00:00",
            },
            {
                "name": "mistral:latest",
                "size": 3900000000,
                "modified_at": "2024-01-02T12:00:00",
            },
        ]

    monkeypatch.setattr(models_mod, "list_models", mock_list_models)

    exit_code = cli(["models", "list"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "llama3.1:8b" in captured.out
    assert "mistral:latest" in captured.out


def test_models_list_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test models list when no models exist."""
    import nyxgpt.models as models_mod

    monkeypatch.setattr(models_mod, "list_models", lambda: [])

    exit_code = cli(["models", "list"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No models found" in captured.out


def test_models_pull(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Test models pull command."""
    import nyxgpt.models as models_mod

    def mock_pull_model(name, progress_callback=None):
        if progress_callback:
            progress_callback("Downloading", 50.0)
            progress_callback("Complete", 100.0)

    monkeypatch.setattr(models_mod, "pull_model", mock_pull_model)

    exit_code = cli(["models", "pull", "llama3.1:8b"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Successfully pulled model" in captured.out


def test_models_delete_with_force(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test models delete command with force flag."""
    import nyxgpt.models as models_mod

    monkeypatch.setattr(models_mod, "delete_model", lambda name: None)

    exit_code = cli(["models", "delete", "llama3.1:8b", "--force"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Deleted model: llama3.1:8b" in captured.out


def test_models_show(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Test models show command."""
    import nyxgpt.models as models_mod

    def mock_show_model(name):
        return {
            "modelfile": "FROM llama3.1",
            "parameters": "temperature 0.7",
            "template": "{{.System}}\n{{.Prompt}}",
        }

    monkeypatch.setattr(models_mod, "show_model", mock_show_model)

    exit_code = cli(["models", "show", "llama3.1:8b"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Model: llama3.1:8b" in captured.out
    assert "Modelfile:" in captured.out


# --- Document attachment CLI tests ---


def test_sessions_documents_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test 'sessions list-attachments' when no documents are attached."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_file = sessions.session_file_for("my-session", sessions_dir)
    sessions.save_session_messages(session_file, [])

    exit_code = cli(
        ["sessions", "list-attachments", "my-session", "--sessions-dir", str(sessions_dir)]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No documents attached" in captured.out


def test_sessions_attach_and_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test attaching a document and then listing it."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_file = sessions.session_file_for("my-session", sessions_dir)
    sessions.save_session_messages(session_file, [])

    exit_code = cli(
        ["sessions", "attach", "my-session", "doc-abc", "--sessions-dir", str(sessions_dir)]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "doc-abc" in captured.out

    # Now list documents
    exit_code = cli(
        ["sessions", "list-attachments", "my-session", "--sessions-dir", str(sessions_dir)]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "doc-abc" in captured.out


def test_sessions_attach_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test that attaching the same document twice is idempotent."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_file = sessions.session_file_for("my-session", sessions_dir)
    sessions.save_session_messages(session_file, [])

    cli(["sessions", "attach", "my-session", "doc-abc", "--sessions-dir", str(sessions_dir)])
    cli(["sessions", "attach", "my-session", "doc-abc", "--sessions-dir", str(sessions_dir)])

    mf = sessions.meta_file_for(session_file)
    meta = sessions.load_session_meta(mf)
    raw = meta.get("attached_doc_ids", [])
    attached = raw if isinstance(raw, list) else []
    assert attached.count("doc-abc") == 1


def test_sessions_detach(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test detaching a document from a session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_file = sessions.session_file_for("my-session", sessions_dir)
    sessions.save_session_messages(session_file, [])

    cli(["sessions", "attach", "my-session", "doc-abc", "--sessions-dir", str(sessions_dir)])
    cli(["sessions", "attach", "my-session", "doc-xyz", "--sessions-dir", str(sessions_dir)])

    exit_code = cli(
        ["sessions", "detach", "my-session", "doc-abc", "--sessions-dir", str(sessions_dir)]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "doc-abc" in captured.out

    mf = sessions.meta_file_for(session_file)
    meta = sessions.load_session_meta(mf)
    raw = meta.get("attached_doc_ids", [])
    attached = raw if isinstance(raw, list) else []
    assert "doc-abc" not in attached
    assert "doc-xyz" in attached


def test_sessions_attach_missing_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test that attach requires both session name and doc_id."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    exit_code = cli(["sessions", "attach", "my-session", "--sessions-dir", str(sessions_dir)])
    assert exit_code == 2


def test_sessions_documents_missing_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test that list-attachments requires a session name."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    exit_code = cli(["sessions", "list-attachments", "--sessions-dir", str(sessions_dir)])
    assert exit_code == 2


def test_sessions_attach_force_include_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test that --force-include flag is accepted and noted in attach output."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    exit_code = cli(
        [
            "sessions",
            "attach",
            "my-session",
            "doc-abc",
            "--force-include",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "doc-abc" in captured.out
    assert "force-include: enabled" in captured.out


# --- --rag-mode CLI flag tests ---


def test_chat_rag_mode_flag_forwarded_to_chat_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--rag-mode flag must be forwarded to chat_stream as rag_enabled=True."""
    import nyxgpt.cli as cli_mod

    calls: list[dict] = []

    def fake_chat_stream(prompt, *, rag_enabled=None, **kwargs):
        calls.append({"rag_enabled": rag_enabled})
        return iter(["Hello"])

    monkeypatch.setattr(cli_mod, "chat_stream", fake_chat_stream)

    exit_code = cli(["chat", "Hello", "--session", "default", "--rag-mode"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["rag_enabled"] is True


def test_chat_no_rag_mode_flag_passes_none_to_chat_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --rag-mode, rag_enabled must be None (not False) in chat_stream."""
    import nyxgpt.cli as cli_mod

    calls: list[dict] = []

    def fake_chat_stream(prompt, *, rag_enabled=None, **kwargs):
        calls.append({"rag_enabled": rag_enabled})
        return iter(["Hello"])

    monkeypatch.setattr(cli_mod, "chat_stream", fake_chat_stream)

    exit_code = cli(["chat", "Hello", "--session", "default"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["rag_enabled"] is None


def test_chat_rag_mode_flag_forwarded_to_chat_no_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--rag-mode flag must be forwarded to chat (non-streaming) as rag_enabled=True."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.chat import ChatResult

    calls: list[dict] = []

    def fake_chat(prompt, *, rag_enabled=None, **kwargs):
        calls.append({"rag_enabled": rag_enabled})
        return ChatResult(session="default", model="test", reply="Hi", rag_used=False, rag_chunks=0)

    monkeypatch.setattr(cli_mod, "chat", fake_chat)

    exit_code = cli(["chat", "Hello", "--session", "default", "--no-stream", "--rag-mode"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["rag_enabled"] is True


def test_chat_no_rag_mode_flag_passes_none_to_chat_no_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --rag-mode, rag_enabled must be None in chat (non-streaming)."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.chat import ChatResult

    calls: list[dict] = []

    def fake_chat(prompt, *, rag_enabled=None, **kwargs):
        calls.append({"rag_enabled": rag_enabled})
        return ChatResult(session="default", model="test", reply="Hi", rag_used=False, rag_chunks=0)

    monkeypatch.setattr(cli_mod, "chat", fake_chat)

    exit_code = cli(["chat", "Hello", "--session", "default", "--no-stream"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["rag_enabled"] is None
