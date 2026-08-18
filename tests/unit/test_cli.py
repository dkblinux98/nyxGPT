from __future__ import annotations

import os
from pathlib import Path

import pytest

from nyxgpt import sessions
from nyxgpt.cli import cli
from nyxgpt.version import running_version

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
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test export fails gracefully when file write fails.

    Uses a monkeypatched ``Path.write_text`` rather than chmod-ing a
    directory read-only: root/container test runners bypass POSIX
    permission bits, which made this test flaky depending on the CI user.
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("test-write-error", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)

    messages = [{"role": "user", "content": "Test"}]
    metadata = {"title": "Write Error Test", "created_at": "2024-01-01T12:00:00"}

    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, metadata)

    output_file = tmp_path / "export.md"

    def _raise_oserror(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("Read-only file system")

    monkeypatch.setattr(Path, "write_text", _raise_oserror)

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


def test_batch_export_missing_session_names(capsys: pytest.CaptureFixture[str]) -> None:
    """batch-export with no session names at all fails before the --output check."""
    exit_code = cli(["sessions", "batch-export"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one session name is required" in captured.err


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


def test_canary_deploy_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test `nyxgpt canary deploy` reports success and exit code 0."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.canary import CanaryResult

    calls: list[dict] = []

    def fake_deploy(*, namespace, component="api"):
        calls.append({"namespace": namespace, "component": component})
        return CanaryResult(True, "Deployed nyxgpt-api:1.2.3-abcd123 to nyxgpt-api-canary")

    monkeypatch.setattr(cli_mod.canary_mod, "deploy", fake_deploy)

    exit_code = cli(["canary", "deploy", "--namespace", "test-ns"])

    assert exit_code == 0
    assert calls == [{"namespace": "test-ns", "component": "api"}]
    assert "[OK] Deployed nyxgpt-api:1.2.3-abcd123 to nyxgpt-api-canary" in capsys.readouterr().out


def test_canary_deploy_failure_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test `nyxgpt canary deploy` surfaces a failure with a nonzero exit code."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.canary import CanaryResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "deploy",
        lambda **kwargs: CanaryResult(False, "Failed to build/load nyxgpt-api:1.2.3-abcd123"),
    )

    exit_code = cli(["canary", "deploy", "--namespace", "test-ns"])

    assert exit_code == 2
    assert "[FAIL] Failed to build/load" in capsys.readouterr().out


def test_ops_observability_dispatches_to_ops_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test `nyxgpt ops observability` parses and dispatches without a raw docker command."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.ops_mod,
        "_sync_packaged_resources",
        lambda: [OpsResult(True, "Synced packaged ops resources")],
    )
    monkeypatch.setattr(
        cli_mod.ops_mod,
        "_reconcile_grafana_provisioning",
        lambda: [OpsResult(True, "Observability stack up")],
    )

    exit_code = cli(["ops", "observability"])

    assert exit_code == 0
    assert "[OK] Observability stack up" in capsys.readouterr().out


def test_ops_migrate_volumes_dispatches_to_ops_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test `nyxgpt ops migrate-volumes` parses and dispatches (issue #3346)."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.ops_mod,
        "migrate_legacy_volumes",
        lambda: [OpsResult(True, "Migrated cassandra data")],
    )

    exit_code = cli(["ops", "migrate-volumes"])

    assert exit_code == 0
    assert "[OK] Migrated cassandra data" in capsys.readouterr().out


def test_ops_port_forward_dispatches_to_ops_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nyxgpt ops port-forward` parses and dispatches, with --port passed through."""
    import nyxgpt.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod.ops_mod, "port_forward", lambda args: calls.append(args.port) or 0)

    exit_code = cli(["ops", "port-forward", "--port", "3001"])

    assert exit_code == 0
    assert calls == [3001]


def test_ops_port_forward_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nyxgpt ops port-forward` still lands on the web Service at port 3000
    when --port/--target are omitted.

    The CLI default is now `None`, not `3000` (#3787): each target has its own
    canonical local port -- Grafana 3001, Jaeger 16686, ... -- so "the operator
    did not choose one" has to be distinguishable from "the operator asked for
    3000". `ops._port_forward_plan` is what resolves it."""
    import nyxgpt.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod.ops_mod, "port_forward", lambda args: calls.append(args) or 0)

    exit_code = cli(["ops", "port-forward"])

    assert exit_code == 0
    assert [(a.target, a.port) for a in calls] == [("web", None)]
    from nyxgpt import ops as ops_mod

    assert ops_mod._port_forward_plan(calls[0]) == [("web", "nyxgpt-web", 3000, 3000)]


def test_ops_install_skip_observability_flag_parses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test `nyxgpt ops install --skip-observability` skips the observability step."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    # Pin to the macOS (Homebrew/launchd) native dispatch branch so the
    # low-level function names patched below are the ones `install()`'s
    # steps actually call -- see test_ops.py's `_force_macos_native_path`
    # fixture for the same reasoning (#3508).
    monkeypatch.setattr(cli_mod.ops_mod.platform, "system", lambda: "Darwin")

    ok = [OpsResult(True, "ok")]
    for step in (
        "_sync_packaged_resources",
        "migrate_legacy_volumes",
        "_reconcile_phantom_compose_app_containers",
        "_ensure_web_deps",
        "_ensure_mcp_deps",
        "_ensure_cassandra_container",
        "_install_cassandra_launchagent",
        "_install_ollama_launchagent",
        "_install_ollama_env_launchagent",
        "_install_homebrew_api",
        "_install_homebrew_web",
        "_ensure_ollama_service",
        "_ensure_required_models",
        "_cleanup_stale_log_symlinks",
    ):
        monkeypatch.setattr(cli_mod.ops_mod, step, lambda: ok)

    called = []
    monkeypatch.setattr(
        cli_mod.ops_mod, "_reconcile_grafana_provisioning", lambda: called.append(True)
    )
    monkeypatch.setattr(cli_mod.ops_mod, "_provision_glitchtip", lambda: called.append(True))

    exit_code = cli(["ops", "install", "--skip-observability"])

    assert exit_code == 0
    assert called == []


def test_ops_observability_quiet_flag_suppresses_step_announcement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`nyxgpt ops observability --quiet` parses and drops the live "[n/m]
    step..." progress announcement, keeping only the terse OK/FAIL line
    (#3558)."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.ops_mod, "_sync_packaged_resources", lambda: [OpsResult(True, "synced")]
    )
    monkeypatch.setattr(
        cli_mod.ops_mod,
        "_reconcile_grafana_provisioning",
        lambda: [OpsResult(True, "Observability stack up")],
    )

    exit_code = cli(["ops", "observability", "--quiet"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[OK] Observability stack up" in out
    assert "[2/2]" not in out


def test_ops_observability_default_verbose_shows_step_announcement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`nyxgpt ops observability` (no `--quiet`) shows the live "[n/m]
    step..." progress announcement by default (#3558)."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.ops_mod, "_sync_packaged_resources", lambda: [OpsResult(True, "synced")]
    )
    monkeypatch.setattr(
        cli_mod.ops_mod,
        "_reconcile_grafana_provisioning",
        lambda: [OpsResult(True, "Observability stack up")],
    )

    exit_code = cli(["ops", "observability"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[1/2] sync packaged resources..." in out
    assert "[2/2] reconcile observability stack..." in out
    assert "[OK] Observability stack up" in out


def test_up_dispatches_to_ops_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nyxgpt up` (#3504) dispatches to `ops_mod.up`, not a reimplementation."""
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "up", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["up"])

    assert exit_code == 0
    assert len(calls) == 1


def test_up_passes_through_install_mode_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """`up`'s mode flags (--skip-observability etc.) must reach `ops_mod.up` unchanged --
    it's a thin alias for `ops install`, not a fork with its own flag set."""
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "up", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["up", "--skip-observability", "--local", "--timeout", "5", "--no-wait"])

    assert exit_code == 0
    args = calls[0]
    assert args.skip_observability is True
    assert args.local is True
    assert args.timeout == 5.0
    assert args.no_wait is True


def test_up_returns_nonzero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(cli_mod.ops_mod, "up", lambda args: 2)

    exit_code = cli(["up"])

    assert exit_code == 2


def test_down_dispatches_to_ops_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nyxgpt down` (#3504) dispatches to the exact same `ops_mod.down` that
    `nyxgpt ops down` uses -- a thin alias, not a forked implementation."""
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "down", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["down"])

    assert exit_code == 0
    assert len(calls) == 1


def test_down_passes_through_scope_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "down", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["down", "--app-only"])

    assert exit_code == 0
    assert calls[0].app_only is True


def test_down_returns_nonzero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(cli_mod.ops_mod, "down", lambda args: 2)

    exit_code = cli(["down"])

    assert exit_code == 2


@pytest.mark.parametrize(
    ("ops_cmd", "argv"),
    [
        ("install", ["ops", "install", "--quiet"]),
        ("down", ["ops", "down", "--quiet"]),
        ("restart", ["ops", "restart", "--quiet"]),
        ("stop", ["ops", "stop", "--quiet"]),
        ("observability", ["ops", "observability", "--quiet"]),
        ("env_sync", ["ops", "env-sync", "--quiet"]),
        ("glitchtip_init", ["ops", "glitchtip-init", "--quiet"]),
    ],
)
def test_ops_quiet_flag_parses_and_reaches_args_for_every_long_running_command(
    monkeypatch: pytest.MonkeyPatch, ops_cmd: str, argv: list[str]
) -> None:
    """`--quiet` must parse for every long-running `ops` command #3558 lists
    (install/down/restart/stop/observability/env-sync/glitchtip-init) and
    reach that command's `args.quiet` -- a parser without the flag would
    raise `SystemExit` before the stub below is ever called."""
    import nyxgpt.cli as cli_mod

    seen_args = []
    monkeypatch.setattr(cli_mod.ops_mod, ops_cmd, lambda args: seen_args.append(args) or 0)

    exit_code = cli(argv)

    assert exit_code == 0
    assert len(seen_args) == 1
    assert seen_args[0].quiet is True


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["ops", "portability"], {"strict": False, "json": False}),
        (["ops", "portability", "--strict"], {"strict": True, "json": False}),
        (["ops", "portability", "--json"], {"strict": False, "json": True}),
    ],
)
def test_ops_portability_parses_and_dispatches(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: dict[str, bool]
) -> None:
    """`nyxgpt ops portability` (P6-16, #3516) reaches nyxgpt.portability with its flags."""
    import nyxgpt.cli as cli_mod

    seen_args = []
    monkeypatch.setattr(
        cli_mod.portability_mod, "portability", lambda args: seen_args.append(args) or 0
    )

    exit_code = cli(argv)

    assert exit_code == 0
    assert len(seen_args) == 1
    assert seen_args[0].strict is expected["strict"]
    assert seen_args[0].json is expected["json"]


def test_ops_portability_propagates_the_strict_gate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(cli_mod.portability_mod, "portability", lambda args: 1)

    assert cli(["ops", "portability", "--strict"]) == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["release", "rc"], {"publish": False, "rc_number": None, "branch": None}),
        (["release", "rc", "--publish"], {"publish": True, "rc_number": None, "branch": None}),
        (
            ["release", "rc", "--publish", "--rc-number", "4", "--branch", "v3.0.0"],
            {"publish": True, "rc_number": 4, "branch": "v3.0.0"},
        ),
    ],
)
def test_release_rc_parses_and_dispatches(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: dict
) -> None:
    """`nyxgpt release rc` (#3727) reaches nyxgpt.release_candidate with its flags."""
    import nyxgpt.cli as cli_mod

    seen_args = []
    monkeypatch.setattr(
        cli_mod.release_candidate_mod, "release_rc", lambda args: seen_args.append(args) or 0
    )

    exit_code = cli(argv)

    assert exit_code == 0
    assert len(seen_args) == 1
    assert seen_args[0].publish is expected["publish"]
    assert seen_args[0].rc_number == expected["rc_number"]
    assert seen_args[0].branch == expected["branch"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["release", "publish"], {"channel": "rc", "number": None, "publish": False}),
        (
            ["release", "publish", "--channel", "rc", "--publish"],
            {"channel": "rc", "number": None, "publish": True},
        ),
        (
            ["release", "publish", "--channel", "rc", "--number", "4"],
            {"channel": "rc", "number": 4, "publish": False},
        ),
    ],
)
def test_release_publish_parses_every_channel(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: dict
) -> None:
    """`nyxgpt release publish` (#3727) is the wrapper for the one publish pipeline."""
    import nyxgpt.cli as cli_mod

    seen_args = []
    monkeypatch.setattr(
        cli_mod.release_candidate_mod, "release_publish", lambda args: seen_args.append(args) or 0
    )

    assert cli(argv) == 0
    assert len(seen_args) == 1
    assert seen_args[0].channel == expected["channel"]
    assert seen_args[0].number == expected["number"]
    assert seen_args[0].publish is expected["publish"]


@pytest.mark.parametrize("channel", ["stable", "dev"])
def test_release_publish_refuses_an_undispatchable_channel_at_the_parser(channel: str) -> None:
    """A release is published by the ceremony, which delegates to the same
    workflow; the nightly `dev` channel no longer exists at all (#3735)."""
    with pytest.raises(SystemExit):
        cli(["release", "publish", "--channel", channel])


def test_release_rc_propagates_a_refused_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked plan (wrong branch, unreachable PyPI) has to surface as a non-zero exit."""
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(cli_mod.release_candidate_mod, "release_rc", lambda args: 1)

    assert cli(["release", "rc", "--publish"]) == 1


# --- Interactive chat mode tests ---


def _queue_inputs(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> None:
    """Make builtins.input() return each value in turn, then raise EOFError."""
    it = iter(values)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


def test_chat_interactive_immediate_eof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Interactive chat exits cleanly when input immediately hits EOF."""
    _queue_inputs(monkeypatch, [])

    exit_code = cli(["chat"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "nyxGPT chat" in captured.out
    assert "Type /exit to quit." in captured.out


def test_chat_interactive_empty_line_then_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Blank lines are skipped; /exit ends the interactive loop."""
    _queue_inputs(monkeypatch, ["", "/exit"])

    exit_code = cli(["chat"])

    assert exit_code == 0


def test_chat_interactive_quit_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """/quit also ends the interactive loop."""
    _queue_inputs(monkeypatch, ["/quit"])

    exit_code = cli(["chat"])

    assert exit_code == 0


def test_chat_interactive_streaming_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real (non-command) line is sent via chat_stream and echoed to stdout."""
    import nyxgpt.cli as cli_mod

    _queue_inputs(monkeypatch, ["hello", "/exit"])

    def fake_chat_stream(prompt, **kwargs):
        assert prompt == "hello"
        return iter(["Hi", " there"])

    monkeypatch.setattr(cli_mod, "chat_stream", fake_chat_stream)

    exit_code = cli(["chat"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Hi there" in captured.out


def test_chat_interactive_no_stream_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real line is sent via chat (non-streaming) and the reply is printed."""
    import nyxgpt.cli as cli_mod
    from nyxgpt.chat import ChatResult

    _queue_inputs(monkeypatch, ["hello", "/exit"])

    def fake_chat(prompt, **kwargs):
        assert prompt == "hello"
        return ChatResult(
            session="default", model="m", reply="Hi there", rag_used=False, rag_chunks=0
        )

    monkeypatch.setattr(cli_mod, "chat", fake_chat)

    exit_code = cli(["chat", "--no-stream"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Hi there" in captured.out


def test_chat_interactive_keyboard_interrupt_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A KeyboardInterrupt raised mid-turn is swallowed and the loop continues."""
    import nyxgpt.cli as cli_mod

    _queue_inputs(monkeypatch, ["hello", "/exit"])

    def fake_chat(prompt, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "chat", fake_chat)

    exit_code = cli(["chat", "--no-stream"])

    assert exit_code == 0


def test_chat_interactive_exception_prints_error_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unexpected exception mid-turn is reported to stderr and the loop continues."""
    import nyxgpt.cli as cli_mod

    _queue_inputs(monkeypatch, ["hello", "/exit"])

    def fake_chat(prompt, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(cli_mod, "chat", fake_chat)

    exit_code = cli(["chat", "--no-stream"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "ERROR: boom" in captured.err


# --- _list_sessions_in_dir edge cases ---


def test_sessions_list_nonexistent_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Listing sessions in a directory that doesn't exist returns an empty list, not an error."""
    sessions_dir = tmp_path / "does-not-exist"

    exit_code = cli(["sessions", "list", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert f"No sessions found in {sessions_dir}" in captured.out


def test_sessions_list_skips_unreadable_session_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session file that raises while being read is silently skipped, not fatal to `list`."""
    import nyxgpt.cli as cli_mod

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    good_file = sessions.session_file_for("good-session", sessions_dir)
    sessions.save_session_messages(good_file, [{"role": "user", "content": "hi"}])

    bad_file = sessions.session_file_for("bad-session", sessions_dir)
    sessions.save_session_messages(bad_file, [{"role": "user", "content": "hi"}])

    orig_load = sessions.load_session_messages

    def flaky_load(path: Path) -> list[dict[str, str]]:
        if path == bad_file:
            raise RuntimeError("simulated read failure")
        return orig_load(path)

    monkeypatch.setattr(cli_mod.sessions, "load_session_messages", flaky_load)

    exit_code = cli(["sessions", "list", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "good-session" in captured.out
    assert "bad-session" not in captured.out


def test_sessions_list_shows_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The `list` action prints a session's summary line when present."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("with-summary", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])
    sessions.save_session_meta(meta_file, {"summary": "A brief summary of the chat"})

    exit_code = cli(["sessions", "list", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "summary: A brief summary of the chat" in captured.out


def test_sessions_show_no_metadata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`show` prints "(no metadata)" when the session has no metadata file."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_file = sessions.session_file_for("no-meta", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    exit_code = cli(["sessions", "show", "no-meta", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "(no metadata)" in captured.out


# --- Failure paths for single-session mutation actions ---


def test_sessions_rename_nonexistent_fails(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "rename", "nope", "new-name"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No such session" in captured.out


def test_sessions_pin_nonexistent_fails(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "pin", "nope"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No such session" in captured.out


def test_sessions_tag_add_nonexistent_fails(capsys: pytest.CaptureFixture[str]) -> None:
    # "nope" -> name, "unused" -> new_name (ignored by tag-add), "sometag" -> extras (the tag)
    exit_code = cli(["sessions", "tag-add", "nope", "unused", "sometag"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No such session" in captured.out


def test_sessions_title_nonexistent_fails(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "title", "nope", "New Title"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No such session" in captured.out


# --- summarize action ---


def test_sessions_summarize_missing_name(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "summarize"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "session name required" in captured.err


def test_sessions_summarize_nonexistent_fails(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "summarize", "nope"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No such session" in captured.out


def test_sessions_summarize_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful summarize prints a confirmation; the LLM call itself is mocked
    (sessions.summarize_session's own LLM-calling logic is covered by sessions.py's
    own test suite -- this test only needs to exercise the cli.py dispatch)."""
    import nyxgpt.cli as cli_mod

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("to-summarize", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    monkeypatch.setattr(cli_mod.sessions, "summarize_session", lambda name, d: (True, "OK"))

    exit_code = cli(["sessions", "summarize", "to-summarize", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Summarized session: to-summarize" in captured.out


# --- Session search pagination ---


def test_sessions_search_pagination_continue_then_finish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With >10 matches, pressing Enter at the prompt advances to the next page."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    messages = [{"role": "user", "content": f"needle number {i}"} for i in range(12)]
    session_file = sessions.session_file_for("many-matches", sessions_dir)
    sessions.save_session_messages(session_file, messages)

    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    exit_code = cli(["sessions", "search", "needle", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Found 12 result(s)" in captured.out


def test_sessions_search_pagination_quit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With >10 matches, typing 'q' at the prompt stops early."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    messages = [{"role": "user", "content": f"needle number {i}"} for i in range(12)]
    session_file = sessions.session_file_for("many-matches", sessions_dir)
    sessions.save_session_messages(session_file, messages)

    monkeypatch.setattr("builtins.input", lambda _prompt="": "q")

    exit_code = cli(["sessions", "search", "needle", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Stopped." in captured.out
    assert "not shown" in captured.out


def test_sessions_search_pagination_keyboard_interrupt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C/EOF at the pagination prompt stops cleanly."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    messages = [{"role": "user", "content": f"needle number {i}"} for i in range(12)]
    session_file = sessions.session_file_for("many-matches", sessions_dir)
    sessions.save_session_messages(session_file, messages)

    def raise_interrupt(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raise_interrupt)

    exit_code = cli(["sessions", "search", "needle", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Stopped." in captured.out


# --- merge failure paths ---


def test_sessions_merge_missing_output_name(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "merge"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "output session name is required" in captured.err


def test_sessions_merge_nonexistent_input_fails(capsys: pytest.CaptureFixture[str]) -> None:
    # "merged-out" -> name (output), "unused" -> new_name (ignored by merge),
    # "no-such-session" -> extras (the input session names)
    exit_code = cli(["sessions", "merge", "merged-out", "unused", "no-such-session"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err


# --- batch operation failure paths ---


def test_batch_delete_partial_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("real-one", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    # First two positionals are consumed by argparse as name/new_name (unused by
    # batch-delete); everything from the third onward lands in `extras`, which is
    # what batch-delete actually operates on.
    exit_code = cli(
        [
            "sessions",
            "batch-delete",
            "unused1",
            "unused2",
            "real-one",
            "ghost-one",
            "ghost-two",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Deleted 1 session(s)" in captured.out
    assert "Failed to delete" in captured.err


def test_batch_tag_add_missing_session_names(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "batch-tag-add", "onlytag"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one session name is required" in captured.err


def test_batch_tag_add_partial_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("real-tag", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    # "newtag" -> name (the tags string); "unused" -> new_name (ignored);
    # everything after -> extras (session names)
    exit_code = cli(
        [
            "sessions",
            "batch-tag-add",
            "newtag",
            "unused",
            "real-tag",
            "ghost-tag",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Added tags to 1 session(s)" in captured.out
    assert "Failed to update" in captured.err


def test_batch_export_partial_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    output_dir = tmp_path / "exports"
    output_dir.mkdir()
    session_file = sessions.session_file_for("real-export", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    exit_code = cli(
        [
            "sessions",
            "batch-export",
            "unused1",
            "unused2",
            "real-export",
            "ghost-export",
            "another-ghost",
            "--sessions-dir",
            str(sessions_dir),
            "--output",
            str(output_dir),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Exported 1 session(s)" in captured.out
    assert "Failed to export" in captured.err


def test_batch_pin_partial_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("real-pin", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    exit_code = cli(
        [
            "sessions",
            "batch-pin",
            "unused1",
            "unused2",
            "real-pin",
            "ghost-pin",
            "another-ghost",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Pinned 1 session(s)" in captured.out
    assert "Failed to update" in captured.err


def test_batch_unpin_partial_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("real-unpin", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    exit_code = cli(
        [
            "sessions",
            "batch-unpin",
            "unused1",
            "unused2",
            "real-unpin",
            "ghost-unpin",
            "another-ghost",
            "--sessions-dir",
            str(sessions_dir),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Unpinned 1 session(s)" in captured.out
    assert "Failed to update" in captured.err


# --- batch-update-meta ---


def test_batch_update_meta_missing_session_names(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "batch-update-meta"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one session name is required" in captured.err


def test_batch_update_meta_missing_fields(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "batch-update-meta", "s1", "s2", "s3"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "at least one metadata field is required" in captured.err


def test_batch_update_meta_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("meta-target", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    exit_code = cli(
        [
            "sessions",
            "batch-update-meta",
            "unused1",
            "unused2",
            "meta-target",
            "ghost-meta",
            "--sessions-dir",
            str(sessions_dir),
            "--model",
            "llama3.1:8b",
            "--rag-enabled",
            "true",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Updated metadata for 1 session(s)" in captured.out
    assert "Failed to update" in captured.err


def test_batch_update_meta_full_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When every named session updates cleanly, batch-update-meta returns 0."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for name in ("meta-a", "meta-b"):
        f = sessions.session_file_for(name, sessions_dir)
        sessions.save_session_messages(f, [{"role": "user", "content": "hi"}])

    exit_code = cli(
        [
            "sessions",
            "batch-update-meta",
            "unused1",
            "unused2",
            "meta-a",
            "meta-b",
            "--sessions-dir",
            str(sessions_dir),
            "--model",
            "llama3.1:8b",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Updated metadata for 2 session(s)" in captured.out
    assert captured.err == ""


# --- detach failure path ---


def test_sessions_detach_missing_args(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli(["sessions", "detach", "only-name"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "session name and doc_id are required" in captured.err


# --- unknown sessions action (unreachable through argparse choices, so we call
# cmd_sessions directly to exercise the defensive fallback branch) ---


def test_cmd_sessions_unknown_action_direct(capsys: pytest.CaptureFixture[str]) -> None:
    from nyxgpt.cli import cmd_sessions

    exit_code = cmd_sessions(
        action="bogus-action",
        name=None,
        new_name=None,
        extras=[],
        sessions_dir=None,
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Unknown sessions action: bogus-action" in captured.err


# --- unsupported export format (unreachable through argparse choices) ---


def test_cmd_sessions_export_unsupported_format_direct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from nyxgpt.cli import cmd_sessions

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("fmt-test", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    exit_code = cmd_sessions(
        action="export",
        name="fmt-test",
        new_name=None,
        extras=[],
        sessions_dir=sessions_dir,
        format="yaml",
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "unsupported format: yaml" in captured.err


# --- session stats: format_age branches ---


def test_sessions_stats_unknown_timestamps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stats with no created_at/updated_at in metadata falls back to "Unknown"."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("no-timestamps", sessions_dir)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])
    # No metadata saved at all -> created_at/updated_at default to "Unknown"

    exit_code = cli(["sessions", "stats", "no-timestamps", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Created: Unknown" in captured.out
    assert "Age: Unknown" in captured.out


def test_sessions_stats_invalid_timestamp_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-ISO timestamp string is echoed back as-is instead of crashing."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("bad-timestamp", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])
    sessions.save_session_meta(
        meta_file, {"created_at": "not-a-real-timestamp", "updated_at": "not-a-real-timestamp"}
    )

    exit_code = cli(["sessions", "stats", "bad-timestamp", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Age: not-a-real-timestamp" in captured.out


def test_sessions_stats_recent_activity_buckets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercise the minutes/hours/seconds formatting buckets in format_age."""
    from datetime import UTC, datetime, timedelta

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("recent", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    now = datetime.now(UTC)
    created = (now - timedelta(hours=3, minutes=15)).isoformat()
    updated = (now - timedelta(minutes=5)).isoformat()
    sessions.save_session_meta(meta_file, {"created_at": created, "updated_at": updated})

    exit_code = cli(["sessions", "stats", "recent", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "hour(s)" in captured.out
    assert "minute(s)" in captured.out


def test_sessions_stats_just_now_activity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A timestamp from right now falls into the "< 1 minute" bucket."""
    from datetime import UTC, datetime

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for("just-now", sessions_dir)
    meta_file = sessions.meta_file_for(session_file)
    sessions.save_session_messages(session_file, [{"role": "user", "content": "hi"}])

    now = datetime.now(UTC).isoformat()
    sessions.save_session_meta(meta_file, {"created_at": now, "updated_at": now})

    exit_code = cli(["sessions", "stats", "just-now", "--sessions-dir", str(sessions_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "< 1 minute" in captured.out


# --- models command error branches ---


def test_models_list_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    def raise_error():
        raise RuntimeError("ollama down")

    monkeypatch.setattr(models_mod, "list_models", raise_error)

    exit_code = cli(["models", "list"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ERROR: Failed to list models" in captured.err


def test_models_pull_value_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    def raise_value_error(name, progress_callback=None):
        raise ValueError("invalid model name")

    monkeypatch.setattr(models_mod, "pull_model", raise_value_error)

    exit_code = cli(["models", "pull", "bogus-model"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "ERROR: invalid model name" in captured.err


def test_models_pull_generic_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    def raise_error(name, progress_callback=None):
        raise RuntimeError("network error")

    monkeypatch.setattr(models_mod, "pull_model", raise_error)

    exit_code = cli(["models", "pull", "some-model"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ERROR: Failed to pull model" in captured.err


def test_models_delete_prompt_confirm_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    monkeypatch.setattr(models_mod, "delete_model", lambda name: None)

    exit_code = cli(["models", "delete", "some-model"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Deleted model: some-model" in captured.out


def test_models_delete_prompt_declined(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    exit_code = cli(["models", "delete", "some-model"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Cancelled" in captured.out


def test_models_delete_prompt_eof_cancels(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raise_eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    exit_code = cli(["models", "delete", "some-model"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Cancelled" in captured.out


def test_models_delete_value_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    def raise_value_error(name):
        raise ValueError("unknown model")

    monkeypatch.setattr(models_mod, "delete_model", raise_value_error)

    exit_code = cli(["models", "delete", "some-model", "--force"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "ERROR: unknown model" in captured.err


def test_models_delete_generic_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    def raise_error(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(models_mod, "delete_model", raise_error)

    exit_code = cli(["models", "delete", "some-model", "--force"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ERROR: Failed to delete model" in captured.err


def test_models_show_other_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    monkeypatch.setattr(
        models_mod,
        "show_model",
        lambda name: {"modelfile": "FROM x", "size": 123, "family": "llama"},
    )

    exit_code = cli(["models", "show", "some-model"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Other info:" in captured.out
    assert '"size": 123' in captured.out


def test_models_show_value_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    def raise_value_error(name):
        raise ValueError("not found")

    monkeypatch.setattr(models_mod, "show_model", raise_value_error)

    exit_code = cli(["models", "show", "some-model"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "ERROR: not found" in captured.err


def test_models_show_generic_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.models as models_mod

    def raise_error(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(models_mod, "show_model", raise_error)

    exit_code = cli(["models", "show", "some-model"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ERROR: Failed to get model info" in captured.err


# --- mcp command ---


def test_cmd_mcp_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.mcp_server as mcp_server_mod

    monkeypatch.setattr(mcp_server_mod, "serve", lambda: None)

    exit_code = cli(["mcp"])

    assert exit_code == 0


def test_cmd_mcp_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.mcp_server as mcp_server_mod

    def raise_interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_server_mod, "serve", raise_interrupt)

    exit_code = cli(["mcp"])

    assert exit_code == 0


def test_cmd_mcp_generic_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.mcp_server as mcp_server_mod

    def raise_error():
        raise RuntimeError("mcp boom")

    monkeypatch.setattr(mcp_server_mod, "serve", raise_error)

    exit_code = cli(["mcp"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ERROR: MCP server failed: mcp boom" in captured.err


# --- wizard dispatch ---


def test_cli_wizard_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[Path | None] = []

    def fake_run_wizard(output_path=None):
        calls.append(output_path)
        return 0

    monkeypatch.setattr(cli_mod, "run_wizard", fake_run_wizard)

    output = tmp_path / "config.ini"
    exit_code = cli(["wizard", "--output", str(output)])

    assert exit_code == 0
    assert calls == [output]


# --- secrets setup dispatch ---


def test_cli_secrets_setup_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[tuple[Path | None, bool]] = []

    def fake_run_secrets_setup(cfg_path=None, reconfigure=False):
        calls.append((cfg_path, reconfigure))
        return 0

    monkeypatch.setattr(cli_mod, "run_secrets_setup", fake_run_secrets_setup)

    output = tmp_path / "config.ini"
    exit_code = cli(["secrets", "setup", "--config", str(output), "--reconfigure"])

    assert exit_code == 0
    assert calls == [(output, True)]


def test_cli_secrets_setup_dispatch_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[tuple[Path | None, bool]] = []

    def fake_run_secrets_setup(cfg_path=None, reconfigure=False):
        calls.append((cfg_path, reconfigure))
        return 0

    monkeypatch.setattr(cli_mod, "run_secrets_setup", fake_run_secrets_setup)

    exit_code = cli(["secrets", "setup"])

    assert exit_code == 0
    assert calls == [(None, False)]


# --- ops command dispatch (status/doctor/restart/env-sync/logs) ---


def test_ops_status_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "status", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "status"])

    assert exit_code == 0
    assert len(calls) == 1


def test_ops_doctor_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "doctor", lambda args: (calls.append(args), 1)[1])

    exit_code = cli(["ops", "doctor"])

    assert exit_code == 1
    assert len(calls) == 1


def test_ops_restart_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "restart", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "restart", "api"])

    assert exit_code == 0
    assert len(calls) == 1


def test_ops_restart_observability_target_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "restart", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "restart", "observability"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].target == "observability"


def test_ops_env_sync_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "env_sync", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "env-sync"])

    assert exit_code == 0
    assert len(calls) == 1


def test_ops_secrets_sync_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "secrets_sync", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "secrets-sync", "--dry-run"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].dry_run is True


def test_ops_logs_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "logs", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "logs", "ollama"])

    assert exit_code == 0
    assert len(calls) == 1


def test_ops_glitchtip_init_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "glitchtip_init", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "glitchtip-init"])

    assert exit_code == 0
    assert len(calls) == 1


def test_ops_credentials_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nyxgpt ops credentials` (#3718) reaches ops with its flags parsed --
    the wrapped replacement for `ssh` + `cat`ing the secrets by hand."""
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "credentials", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "credentials", "--service", "grafana", "--json"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].service == "grafana"  # type: ignore[attr-defined]
    assert calls[0].json is True  # type: ignore[attr-defined]


def test_ops_credentials_defaults_to_every_service(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "credentials", lambda args: (calls.append(args), 0)[1])

    assert cli(["ops", "credentials"]) == 0
    assert calls[0].service == "all"  # type: ignore[attr-defined]
    assert calls[0].json is False  # type: ignore[attr-defined]


def test_cloud_credentials_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nyxgpt cloud credentials` (#3718) routes to cloud_deploy's command
    handler, so an operator never needs a manual ssh into the instance."""
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(
        cli_mod.cloud_deploy_mod, "deploy_command", lambda args: (calls.append(args), 0)[1]
    )

    exit_code = cli(["cloud", "credentials", "--service", "glitchtip"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].cloud_cmd == "credentials"  # type: ignore[attr-defined]
    assert calls[0].service == "glitchtip"  # type: ignore[attr-defined]


def test_ops_alert_test_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.ops_mod, "alert_test", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["ops", "alert-test"])

    assert exit_code == 0
    assert len(calls) == 1


# --- cloud command dispatch ---


def test_cloud_allow_ip_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.cloud_mod, "allow_ip", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(["cloud", "allow-ip"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].ip is None
    assert calls[0].security_group_id is None
    assert calls[0].region is None


def test_cloud_allow_ip_dispatch_with_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(cli_mod.cloud_mod, "allow_ip", lambda args: (calls.append(args), 0)[1])

    exit_code = cli(
        [
            "cloud",
            "allow-ip",
            "--ip",
            "203.0.113.5",
            "--security-group-id",
            "sg-abc123",
            "--region",
            "us-east-1",
        ]
    )

    assert exit_code == 0
    assert calls[0].ip == "203.0.113.5"
    assert calls[0].security_group_id == "sg-abc123"
    assert calls[0].region == "us-east-1"


def test_cloud_user_data_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(
        cli_mod.cloud_provision_mod, "user_data", lambda args: (calls.append(args), 0)[1]
    )

    exit_code = cli(["cloud", "user-data", "--os", "linux"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].os == "linux"
    assert calls[0].version is None
    assert calls[0].output is None


def test_cloud_user_data_dispatch_with_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(
        cli_mod.cloud_provision_mod, "user_data", lambda args: (calls.append(args), 0)[1]
    )

    exit_code = cli(
        ["cloud", "user-data", "--os", "macos", "--version", "3.0.0", "--output", "/tmp/out.sh"]
    )

    assert exit_code == 0
    assert calls[0].os == "macos"
    assert calls[0].version == "3.0.0"
    assert calls[0].output == "/tmp/out.sh"


def test_cloud_user_data_rejects_unknown_os() -> None:
    with pytest.raises(SystemExit):
        cli(["cloud", "user-data", "--os", "windows"])


def test_cloud_credentials_setup_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[Path | None] = []

    def fake_run_aws_credentials_setup(cfg_path=None):
        calls.append(cfg_path)
        return 0

    monkeypatch.setattr(cli_mod, "run_aws_credentials_setup", fake_run_aws_credentials_setup)

    output = tmp_path / "config.ini"
    exit_code = cli(["cloud", "credentials-setup", "--config", str(output)])

    assert exit_code == 0
    assert calls == [output]


def test_cloud_credentials_setup_dispatch_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[Path | None] = []

    def fake_run_aws_credentials_setup(cfg_path=None):
        calls.append(cfg_path)
        return 0

    monkeypatch.setattr(cli_mod, "run_aws_credentials_setup", fake_run_aws_credentials_setup)

    exit_code = cli(["cloud", "credentials-setup"])

    assert exit_code == 0
    assert calls == [None]


# --- canary command dispatch ---


def test_canary_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "status",
        lambda namespace, component="api": {
            "namespace": namespace,
            "active": True,
            "weight_percent": 20,
            "stable": {"state": "healthy", "message": "stable ok", "version": "1.0.0"},
            "canary": {"state": "unhealthy", "message": "canary not ready", "version": ""},
            "metrics": {
                "total_requests": 100,
                "error_rate_percent": 1.5,
                "p95_latency_ms": 42.0,
            },
            "history": ["start at 10%"],
            "mode": "kubernetes",
            "mode_supported": True,
            "mode_message": None,
        },
    )

    exit_code = cli(["canary", "status", "--namespace", "test-ns"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "in progress at 20%" in out
    assert "namespace=test-ns" in out
    assert "stable: healthy - stable ok, version=1.0.0" in out
    assert "canary: unhealthy" in out
    assert "100 requests" in out
    assert "start at 10%" in out


def test_canary_status_notes_mode_when_unsupported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "status",
        lambda namespace, component="api": {
            "namespace": namespace,
            "active": False,
            "weight_percent": 0,
            "stable": {"state": "not_deployed", "message": "no cluster", "version": ""},
            "canary": {"state": "not_deployed", "message": "no cluster", "version": ""},
            "metrics": {"total_requests": 0, "error_rate_percent": 0.0, "p95_latency_ms": 0.0},
            "history": [],
            "mode": "terraform",
            "mode_supported": False,
            "mode_message": "Canary deployment is provided by Kubernetes mode; "
            "this process is currently running in terraform mode.",
        },
    )

    exit_code = cli(["canary", "status", "--namespace", "test-ns"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "note: Canary deployment is provided by Kubernetes mode" in out


def test_canary_start(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    calls: list[dict] = []

    def fake_start(*, namespace, weight_percent, total_replicas, component="api"):
        calls.append(
            {
                "namespace": namespace,
                "weight_percent": weight_percent,
                "total_replicas": total_replicas,
                "component": component,
            }
        )
        return OpsResult(True, "Started canary rollout at 15%")

    monkeypatch.setattr(cli_mod.canary_mod, "start", fake_start)

    exit_code = cli(["canary", "start", "--weight", "15", "--namespace", "test-ns"])

    assert exit_code == 0
    assert calls[0]["weight_percent"] == 15
    assert "[OK] Started canary rollout at 15%" in capsys.readouterr().out


def test_canary_evaluate_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "evaluate",
        lambda namespace, **kwargs: OpsResult(True, "Canary healthy, promoting eligible"),
    )

    exit_code = cli(["canary", "evaluate", "--namespace", "test-ns"])

    assert exit_code == 0
    assert "[OK] Canary healthy" in capsys.readouterr().out


def test_canary_evaluate_fail_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "evaluate",
        lambda namespace, **kwargs: OpsResult(False, "Error rate too high, rolled back"),
    )

    exit_code = cli(["canary", "evaluate", "--namespace", "test-ns"])

    assert exit_code == 2
    assert "[FAIL] Error rate too high" in capsys.readouterr().out


def test_canary_promote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    calls: list[dict] = []

    def fake_promote(*, namespace, step_percent, total_replicas, component="api"):
        calls.append({"step_percent": step_percent})
        return OpsResult(True, "Promoted canary to 30%")

    monkeypatch.setattr(cli_mod.canary_mod, "promote", fake_promote)

    exit_code = cli(["canary", "promote", "--step", "10", "--namespace", "test-ns"])

    assert exit_code == 0
    assert calls[0]["step_percent"] == 10
    assert "[OK] Promoted canary to 30%" in capsys.readouterr().out


def test_canary_rollback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "rollback",
        lambda *, namespace, total_replicas, component="api": OpsResult(True, "Rolled back canary"),
    )

    exit_code = cli(["canary", "rollback", "--namespace", "test-ns"])

    assert exit_code == 0
    assert "[OK] Rolled back canary" in capsys.readouterr().out


def test_canary_status_uses_config_namespace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --namespace, the namespace is resolved via get_canary_namespace(config)."""
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_canary_namespace", lambda cfg: "canary-config-ns")
    monkeypatch.setattr(
        cli_mod.canary_mod,
        "status",
        lambda namespace, component="api": {
            "namespace": namespace,
            "active": False,
            "weight_percent": 0,
            "stable": {"state": "healthy", "message": "ok", "version": "1.0.0"},
            "canary": {"state": "healthy", "message": "ok", "version": "1.0.0"},
            "metrics": {"total_requests": 0, "error_rate_percent": 0.0, "p95_latency_ms": 0.0},
            "history": [],
            "mode": "kubernetes",
            "mode_supported": True,
            "mode_message": None,
        },
    )

    exit_code = cli(["canary", "status"])

    assert exit_code == 0
    assert "namespace=canary-config-ns" in capsys.readouterr().out


def test_canary_start_prints_details_when_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "start",
        lambda **kwargs: OpsResult(False, "Already in progress", "at 15%"),
    )

    exit_code = cli(["canary", "start", "--namespace", "test-ns"])

    assert exit_code == 2
    out = capsys.readouterr().out
    assert "[FAIL] Already in progress" in out
    assert "at 15%" in out


def test_canary_evaluate_prints_details_when_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "evaluate",
        lambda namespace, **kwargs: OpsResult(False, "Rolled back", "error_rate=9.0%"),
    )

    exit_code = cli(["canary", "evaluate", "--namespace", "test-ns"])

    assert exit_code == 2
    out = capsys.readouterr().out
    assert "[FAIL] Rolled back" in out
    assert "error_rate=9.0%" in out


def test_canary_promote_prints_details_when_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "promote",
        lambda **kwargs: OpsResult(True, "Promoted", "now at 40%"),
    )

    exit_code = cli(["canary", "promote", "--namespace", "test-ns"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[OK] Promoted" in out
    assert "now at 40%" in out


def test_canary_rollback_prints_details_when_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod
    from nyxgpt.ops import OpsResult

    monkeypatch.setattr(
        cli_mod.canary_mod,
        "rollback",
        lambda *, namespace, total_replicas, component="api": OpsResult(
            True, "Rolled back", "stable at 100%"
        ),
    )

    exit_code = cli(["canary", "rollback", "--namespace", "test-ns"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[OK] Rolled back" in out
    assert "stable at 100%" in out


# --- self-heal command dispatch ---


def test_self_heal_status_with_components(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.self_heal_mod,
        "status",
        lambda: {
            "enabled": True,
            "components": [
                {
                    "service": "api",
                    "healthy": True,
                    "state": "running",
                    "health": "healthy",
                    "desired": True,
                },
                {
                    "service": "ollama",
                    "healthy": False,
                    "state": "running",
                    "health": "unhealthy",
                    "desired": True,
                },
            ],
            "events": [
                {"service": "ollama", "action": "restart", "ok": True, "reason": "unhealthy"},
            ],
        },
    )

    exit_code = cli(["self-heal", "status"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Self-heal watchdog: enabled" in out
    assert "api: state=running health=healthy" in out
    assert "ollama: state=running health=unhealthy" in out
    assert "Recent heal events:" in out


def test_self_heal_status_marks_intentionally_stopped_component_as_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.self_heal_mod,
        "status",
        lambda: {
            "enabled": True,
            "components": [
                {
                    "service": "api",
                    "healthy": False,
                    "state": "stopped",
                    "health": "",
                    "desired": False,
                },
            ],
            "events": [],
        },
    )

    exit_code = cli(["self-heal", "status"])

    assert exit_code == 0
    out = capsys.readouterr().out
    # An intentionally-stopped component isn't actionable, so it shouldn't
    # be flagged the same way as a genuine unhealthy component (#3406).
    assert "[OK] api" in out
    assert "(disabled -- not auto-healed)" in out


def test_self_heal_status_no_components(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.self_heal_mod,
        "status",
        lambda: {"enabled": False, "components": [], "events": []},
    )

    exit_code = cli(["self-heal", "status"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No components found" in out


def test_self_heal_status_reports_undetermined_components_as_such(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#3812: a component the probe could not query prints `??` with the
    reason, not the `!!` that means "this is broken"."""
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.self_heal_mod,
        "status",
        lambda: {
            "enabled": True,
            "compose_probe_available": False,
            "compose_probe_reason": "`docker compose ps` exited 125: permission denied",
            "components": [
                {
                    "service": "grafana",
                    "state": "unknown",
                    "health": "",
                    "healthy": False,
                    "desired": True,
                    "known": False,
                }
            ],
            "unhealthy_count": 0,
            "unknown_count": 1,
            "events": [],
        },
    )

    exit_code = cli(["self-heal", "status"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "CANNOT DETERMINE from here -- `docker compose ps` exited 125" in out
    assert "[??] grafana" in out
    assert "state could not be determined" in out
    assert "[!!]" not in out


def test_self_heal_enable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[bool] = []
    monkeypatch.setattr(
        cli_mod.self_heal_mod, "set_enabled", lambda enabled: (calls.append(enabled), True)[1]
    )

    exit_code = cli(["self-heal", "enable"])

    assert exit_code == 0
    assert calls == [True]
    assert "Self-heal watchdog: enabled" in capsys.readouterr().out


def test_self_heal_disable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[bool] = []
    monkeypatch.setattr(
        cli_mod.self_heal_mod, "set_enabled", lambda enabled: (calls.append(enabled), False)[1]
    )

    exit_code = cli(["self-heal", "disable"])

    assert exit_code == 0
    assert calls == [False]
    assert "Self-heal watchdog: disabled" in capsys.readouterr().out


def test_self_heal_heal_all(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.self_heal_mod,
        "heal_now",
        lambda service=None: {
            "healed": [{"service": "ollama", "ok": True, "message": "restarted"}],
        },
    )

    exit_code = cli(["self-heal", "heal"])

    assert exit_code == 0
    assert "[OK] ollama: restarted" in capsys.readouterr().out


def test_self_heal_heal_nothing_to_heal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(cli_mod.self_heal_mod, "heal_now", lambda service=None: {"healed": []})

    exit_code = cli(["self-heal", "heal"])

    assert exit_code == 0
    assert "Nothing to heal" in capsys.readouterr().out


def test_self_heal_heal_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.self_heal_mod,
        "heal_now",
        lambda service=None: {"error": "Unknown or not-running component: bogus"},
    )

    exit_code = cli(["self-heal", "heal", "--service", "bogus"])

    assert exit_code == 2
    assert "[FAIL] Unknown or not-running component: bogus" in capsys.readouterr().out


# --- logging setup failure is non-fatal ---


def test_cli_logging_setup_failure_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nyxgpt.cli as cli_mod

    def raise_error(cfg, console=True):
        raise RuntimeError("logging misconfigured")

    monkeypatch.setattr(cli_mod, "configure_logging", raise_error)

    exit_code = cli(["info"])

    assert exit_code == 0
    assert "nyxGPT OK" in capsys.readouterr().out


# --- unknown top-level command (unreachable through real argparse parsing since
# every valid subcommand is enumerated in cli()'s if/elif chain; we bypass
# argparse's own validation to exercise the defensive help+exit-2 fallback) ---


def test_cli_unknown_command_prints_help_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    def fake_parse_args(self, argv=None, namespace=None):
        return argparse.Namespace(command="bogus-command", config=None)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)

    exit_code = cli([])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower()


# --- correlation id minted per CLI invocation (#3390 <-> subprocess env, #3430) ---


def test_cli_ops_dispatch_mints_correlation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from nyxgpt import ops as ops_mod

    monkeypatch.delenv("NYXGPT_CORRELATION_ID", raising=False)
    monkeypatch.setattr(ops_mod, "status", lambda args: 0)

    exit_code = cli(["ops", "status"])

    assert exit_code == 0
    assert os.environ.get("NYXGPT_CORRELATION_ID")


def test_cli_ops_dispatch_does_not_override_existing_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correlation id set by a wrapper script (or a nested nyxgpt-in-nyxgpt
    invocation) must survive -- os.environ.setdefault, not a blind assign."""
    from nyxgpt import ops as ops_mod

    monkeypatch.setenv("NYXGPT_CORRELATION_ID", "already-set")
    monkeypatch.setattr(ops_mod, "status", lambda args: 0)

    cli(["ops", "status"])

    assert os.environ["NYXGPT_CORRELATION_ID"] == "already-set"


def test_cli_canary_dispatch_mints_correlation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from nyxgpt import cli as cli_module

    monkeypatch.delenv("NYXGPT_CORRELATION_ID", raising=False)
    monkeypatch.setattr(cli_module, "cmd_canary_status", lambda *a, **k: 0)

    exit_code = cli(["canary", "status"])

    assert exit_code == 0
    assert os.environ.get("NYXGPT_CORRELATION_ID")


def test_cli_version_flag_prints_running_version(capsys: pytest.CaptureFixture[str]) -> None:
    """`nyxgpt --version` must exit 0 and print the installed version.

    Step 1 of the owner acceptance sequence and the macOS brew smoke job both
    run this on a machine with no checkout; it exited 2 with "unrecognized
    arguments" until #3753.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"nyxgpt {running_version()}"


def test_cli_version_is_read_from_package_metadata_not_a_literal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed version must track the installed distribution.

    Pins the wiring, not a value: a hardcoded string would keep the test above
    green while drifting from the released artifact (the #3716 failure mode).
    """
    from nyxgpt import cli as cli_module

    monkeypatch.setattr(cli_module, "running_version", lambda: "9.9.9-from-metadata")

    with pytest.raises(SystemExit):
        cli(["--version"])

    assert capsys.readouterr().out.strip() == "nyxgpt 9.9.9-from-metadata"


# --- cloud smoke: the AWS smoke and its containerized sibling (#3784) ---


def test_cloud_smoke_container_dispatches_to_the_artifact_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--container` runs the local Amazon Linux 2023 smoke, never AWS.

    The split matters more than most dispatch wiring: the same command name
    either builds a throwaway container or provisions billed infrastructure.
    """
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(
        cli_mod.cloud_artifact_smoke_mod,
        "container_smoke_command",
        lambda args: (calls.append(args), 0)[1],
    )
    monkeypatch.setattr(
        cli_mod.cloud_smoke_mod,
        "smoke_command",
        lambda args: pytest.fail("--container must never reach the AWS smoke"),
    )

    exit_code = cli(["cloud", "smoke", "--container", "--version", "3.0.0rc9"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].container is True  # type: ignore[attr-defined]
    assert calls[0].version == "3.0.0rc9"  # type: ignore[attr-defined]


def test_cloud_smoke_without_container_still_runs_the_aws_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nyxgpt.cli as cli_mod

    calls: list[object] = []
    monkeypatch.setattr(
        cli_mod.cloud_smoke_mod, "smoke_command", lambda args: (calls.append(args), 0)[1]
    )

    exit_code = cli(["cloud", "smoke"])

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].container is False  # type: ignore[attr-defined]


def test_container_only_flags_are_refused_on_the_aws_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--inject` without `--container` must not deploy real infrastructure."""
    import nyxgpt.cli as cli_mod

    monkeypatch.setattr(
        cli_mod.cloud_smoke_mod,
        "smoke_command",
        lambda args: pytest.fail("a container-only flag must not reach the AWS smoke"),
    )

    exit_code = cli(["cloud", "smoke", "--inject", "old-python"])

    assert exit_code == 2
    assert "--inject" in capsys.readouterr().err
