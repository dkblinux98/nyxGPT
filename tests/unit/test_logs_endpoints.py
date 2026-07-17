"""Unit tests for the /api/v1/logs/* log browsing/viewing/streaming endpoints.

These exercise src/nyxgpt/app.py's logs_list_files, logs_view_file, and
logs_stream_file route handlers against real log files on disk (via
tmp_path), patching only `nyxgpt.app.get_log_dir` to point at the temp
directory. The defensive path-escape / filesystem-error branches (which are
unreachable through the real HTTP routing layer because
`os.path.basename()` always strips any "/" from `filename` before it is
used, and FastAPI's default `str` path converter also rejects raw or
percent-encoded "/" in a single path segment) are covered by mocking
`get_log_dir` to return a `MagicMock` that simulates a resolved path
mismatch / OSError from `Path.resolve()`.
"""

from __future__ import annotations

import builtins
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# GET /api/v1/logs/files
# ---------------------------------------------------------------------------


def test_logs_list_files_returns_empty_when_log_dir_missing(tmp_path):
    missing_dir = tmp_path / "does-not-exist"

    with patch("nyxgpt.app.get_log_dir", return_value=missing_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/files")

    assert response.status_code == 200
    assert response.json() == {"files": []}


def test_logs_list_files_lists_real_log_files_with_metadata(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "nyxgpt.log").write_text("line1\nline2\n")
    (log_dir / "nyxgpt.log.1").write_text("old\n")
    (log_dir / "ignored.txt").write_text("not a log\n")
    (log_dir / "a-subdir.log").mkdir()  # matches the glob but isn't a file

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/files")

    assert response.status_code == 200
    body = response.json()
    assert body["log_dir"] == str(log_dir)

    names = {f["name"] for f in body["files"]}
    # Only real files matching "*.log*" are listed; the directory and the
    # non-matching ".txt" file are excluded.
    assert names == {"nyxgpt.log", "nyxgpt.log.1"}
    for entry in body["files"]:
        assert entry["path"] == str(log_dir / entry["name"])
        assert entry["size"] > 0
        assert entry["modified"] > 0


# ---------------------------------------------------------------------------
# GET /api/v1/logs/view/{filename}
# ---------------------------------------------------------------------------


def test_logs_view_file_returns_full_contents(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text("line-one\nline-two\nline-three\n")

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/app.log")

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "app.log"
    assert body["lines"] == ["line-one\n", "line-two\n", "line-three\n"]
    assert body["total_lines"] == 3
    assert body["filtered_lines"] == 3


def test_logs_view_file_returns_404_for_missing_file(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/missing.log")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "http_error"
    assert body["error"]["message"] == "Log file not found"
    assert "request_id" in body["error"]


def test_logs_view_file_returns_404_for_directory(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "subdir").mkdir()

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/subdir")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Log file not found"


def _mock_log_dir_with_mismatched_resolve() -> tuple[MagicMock, MagicMock]:
    """Build a mocked log_dir/log_file pair that exists and is a file, but
    whose resolved parent does not match the resolved log_dir -- simulating
    a bypass of the `os.path.basename()` sanitization upstream (e.g. a
    future refactor bug), which the resolved-path check defends against."""
    mock_log_dir = MagicMock()
    mock_log_file = MagicMock()
    mock_log_dir.__truediv__.return_value = mock_log_file
    mock_log_file.exists.return_value = True
    mock_log_file.is_file.return_value = True
    mock_log_dir.resolve.return_value = "resolved-log-dir"
    return mock_log_dir, mock_log_file


def test_logs_view_file_returns_403_when_resolved_path_escapes_log_dir():
    mock_log_dir, mock_log_file = _mock_log_dir_with_mismatched_resolve()
    mock_log_file.parent.resolve.return_value = "resolved-elsewhere"

    with patch("nyxgpt.app.get_log_dir", return_value=mock_log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/escape.log")

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Access denied"


def test_logs_view_file_returns_404_when_resolve_raises_oserror():
    mock_log_dir, mock_log_file = _mock_log_dir_with_mismatched_resolve()
    mock_log_file.parent.resolve.side_effect = OSError("simulated resolve failure")

    with patch("nyxgpt.app.get_log_dir", return_value=mock_log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/whatever.log")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Log file not found"


def test_logs_view_file_filters_by_level(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    content = (
        "2026-07-17 10:00:00 INFO nyxgpt Starting up\n"
        "2026-07-17 10:00:01 DEBUG nyxgpt Debug details\n"
        "2026-07-17 10:00:02 ERROR nyxgpt Something failed\n"
    )
    (log_dir / "app.log").write_text(content)

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/app.log", params={"level": "error"})

    body = response.json()
    assert body["total_lines"] == 3
    assert body["filtered_lines"] == 1
    assert "ERROR" in body["lines"][0]


def test_logs_view_file_filters_by_search(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    content = "alpha line\nBETA line\ngamma line with Boom\n"
    (log_dir / "app.log").write_text(content)

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/app.log", params={"search": "boom"})

    body = response.json()
    assert body["total_lines"] == 3
    assert body["filtered_lines"] == 1
    assert "Boom" in body["lines"][0]


def test_logs_view_file_applies_tail_limit(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    content = "".join(f"line-{i}\n" for i in range(10))
    (log_dir / "app.log").write_text(content)

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/app.log", params={"tail": 3})

    body = response.json()
    assert body["total_lines"] == 10
    assert body["filtered_lines"] == 3
    assert body["lines"] == ["line-7\n", "line-8\n", "line-9\n"]


def test_logs_view_file_returns_500_when_read_fails(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    target = log_dir / "app.log"
    target.write_text("line\n")

    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if os.fspath(file) == str(target):
            raise OSError("simulated disk failure")
        return real_open(file, *args, **kwargs)

    with (
        patch("nyxgpt.app.get_log_dir", return_value=log_dir),
        patch("builtins.open", side_effect=fake_open),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/logs/view/app.log")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "http_error"
    assert "Failed to read log file" in body["error"]["message"]
    assert "simulated disk failure" in body["error"]["message"]


# ---------------------------------------------------------------------------
# GET /api/v1/logs/stream/{filename}
# ---------------------------------------------------------------------------


def test_logs_stream_file_returns_404_for_missing_file(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/stream/missing.log")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Log file not found"


def test_logs_stream_file_returns_404_for_directory(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "subdir").mkdir()

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/stream/subdir")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Log file not found"


def test_logs_stream_file_returns_403_when_resolved_path_escapes_log_dir():
    mock_log_dir, mock_log_file = _mock_log_dir_with_mismatched_resolve()
    mock_log_file.parent.resolve.return_value = "resolved-elsewhere"

    with patch("nyxgpt.app.get_log_dir", return_value=mock_log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/stream/escape.log")

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Access denied"


def test_logs_stream_file_returns_404_when_resolve_raises_oserror():
    mock_log_dir, mock_log_file = _mock_log_dir_with_mismatched_resolve()
    mock_log_file.parent.resolve.side_effect = OSError("simulated resolve failure")

    with patch("nyxgpt.app.get_log_dir", return_value=mock_log_dir):
        client = TestClient(app)
        response = client.get("/api/v1/logs/stream/whatever.log")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Log file not found"


def test_logs_stream_file_streams_filtered_lines(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    content = (
        "2026-07-17 10:00:00 INFO nyxgpt Starting up\n"
        "2026-07-17 10:00:01 DEBUG nyxgpt Debug details\n"
        "2026-07-17 10:00:02 ERROR nyxgpt boom happened\n"
        "2026-07-17 10:00:03 INFO nyxgpt boom mentioned again\n"
    )
    (log_dir / "app.log").write_text(content)

    with patch("nyxgpt.app.get_log_dir", return_value=log_dir):
        client = TestClient(app)
        with client.stream(
            "GET",
            "/api/v1/logs/stream/app.log",
            params={"level": "info", "search": "boom"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain")
            assert 'filename="app.log"' in response.headers["content-disposition"]
            assert response.headers["x-content-type-options"] == "nosniff"
            body = "".join(response.iter_text())

    # Only the line matching BOTH the level filter (INFO) and the search
    # filter ("boom") survives -- exercising both `continue` branches (the
    # DEBUG/ERROR lines fail the level check, the "Starting up" INFO line
    # fails the search check) as well as the final `yield`.
    assert body == "2026-07-17 10:00:03 INFO nyxgpt boom mentioned again\n"


def test_logs_stream_file_yields_error_message_when_read_fails(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    target = log_dir / "app.log"
    target.write_text("line\n")

    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if os.fspath(file) == str(target):
            raise OSError("simulated stream failure")
        return real_open(file, *args, **kwargs)

    with (
        patch("nyxgpt.app.get_log_dir", return_value=log_dir),
        patch("builtins.open", side_effect=fake_open),
    ):
        client = TestClient(app)
        with client.stream("GET", "/api/v1/logs/stream/app.log") as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

    assert body == "Error: simulated stream failure\n"
