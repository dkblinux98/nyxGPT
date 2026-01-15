from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from mygpt import sessions
from mygpt.cli import cli


def test_sessions_export_markdown_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    exit_code = cli(["sessions", "export", "test-session", "--sessions-dir", str(sessions_dir), "--format", "markdown"])

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

    exit_code = cli(["sessions", "export", "test-json", "--sessions-dir", str(sessions_dir), "--format", "json"])

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

    exit_code = cli(["sessions", "export", "test-html", "--sessions-dir", str(sessions_dir), "--format", "html"])

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

    exit_code = cli([
        "sessions", "export", "test-file",
        "--sessions-dir", str(sessions_dir),
        "--format", "markdown",
        "--output", str(output_file)
    ])

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


def test_sessions_export_nonexistent_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_sessions_export_default_format_is_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_sessions_export_file_write_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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

    exit_code = cli([
        "sessions", "export", "test-write-error",
        "--sessions-dir", str(sessions_dir),
        "--output", str(output_file)
    ])

    # Should return exit code 1 for write error
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "ERROR: Failed to write to" in captured.err
    assert str(output_file) in captured.err

    # Cleanup
    readonly_dir.chmod(0o755)


def test_sessions_stats_displays_message_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_sessions_stats_handles_minimal_metadata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_sessions_stats_nonexistent_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_sessions_stats_calculates_token_estimate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    assert not "Approximate tokens: 0" in captured.out
