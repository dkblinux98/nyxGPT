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
