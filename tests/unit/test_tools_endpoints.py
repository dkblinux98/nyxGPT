"""Unit tests for the /api/v1/tools/{ls,cat,grep} and /api/v1/rag/config endpoints.

These exercise src/nyxgpt/app.py's tool_ls/tool_cat/tool_grep/_tools_error_status
route handlers against the real `nyxgpt.tools_fs` implementation (only
`get_tools_root` is patched, to confine each test to a tmp_path sandbox
instead of the real home directory), plus the rag_config route handler.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import _tools_error_status, app

pytestmark = pytest.mark.unit


# ----------------------------
# _tools_error_status
# ----------------------------


def test_tools_error_status_returns_403_for_root_escape_message():
    assert _tools_error_status("path escapes allowed root: /etc") == 403


def test_tools_error_status_returns_400_for_other_messages():
    assert _tools_error_status("No such path: /nonexistent") == 400


# ----------------------------
# POST /api/v1/tools/ls
# ----------------------------


def test_tool_ls_success_lists_directory(tmp_path: Path):
    (tmp_path / "file1.txt").write_text("content1")
    (tmp_path / "subdir").mkdir()

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post("/api/v1/tools/ls", json={"path": str(tmp_path)})

    assert response.status_code == 200
    body = response.json()
    assert "file1.txt" in body["output"]
    assert "subdir/" in body["output"]


def test_tool_ls_outside_root_returns_403(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with patch("nyxgpt.app.get_tools_root", return_value=root):
        client = TestClient(app)
        response = client.post("/api/v1/tools/ls", json={"path": str(outside)})

    assert response.status_code == 403
    assert "escapes allowed root" in response.json()["error"]["message"]


def test_tool_ls_nonexistent_path_returns_400(tmp_path: Path):
    nonexistent = tmp_path / "nonexistent"

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post("/api/v1/tools/ls", json={"path": str(nonexistent)})

    assert response.status_code == 400
    assert "No such path" in response.json()["error"]["message"]


# ----------------------------
# POST /api/v1/tools/cat
# ----------------------------


def test_tool_cat_success_reads_file(tmp_path: Path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post("/api/v1/tools/cat", json={"path": str(test_file)})

    assert response.status_code == 200
    assert response.json()["output"].strip() == "hello world"


def test_tool_cat_with_head_limit(tmp_path: Path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4")

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post("/api/v1/tools/cat", json={"path": str(test_file), "head": 2})

    assert response.status_code == 200
    lines = response.json()["output"].strip().split("\n")
    assert lines == ["Line 1", "Line 2"]


def test_tool_cat_outside_root_returns_403(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("top secret")

    with patch("nyxgpt.app.get_tools_root", return_value=root):
        client = TestClient(app)
        response = client.post("/api/v1/tools/cat", json={"path": str(outside_file)})

    assert response.status_code == 403
    assert "escapes allowed root" in response.json()["error"]["message"]


def test_tool_cat_nonexistent_file_returns_400(tmp_path: Path):
    nonexistent = tmp_path / "nonexistent.txt"

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post("/api/v1/tools/cat", json={"path": str(nonexistent)})

    assert response.status_code == 400
    assert "No such file" in response.json()["error"]["message"]


# ----------------------------
# POST /api/v1/tools/grep
# ----------------------------


def test_tool_grep_success_finds_pattern(tmp_path: Path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1: hello\nLine 2: world")

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post(
            "/api/v1/tools/grep", json={"pattern": "hello", "path": str(test_file)}
        )

    assert response.status_code == 200
    assert "hello" in response.json()["output"]


def test_tool_grep_respects_max_matches(tmp_path: Path):
    test_file = tmp_path / "test.txt"
    content = "\n".join(f"match line {i}" for i in range(20))
    test_file.write_text(content)

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post(
            "/api/v1/tools/grep",
            json={"pattern": "match", "path": str(test_file), "max": 3},
        )

    assert response.status_code == 200
    lines = [line for line in response.json()["output"].split("\n") if line.strip()]
    assert len(lines) == 3


def test_tool_grep_outside_root_returns_403(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("match")

    with patch("nyxgpt.app.get_tools_root", return_value=root):
        client = TestClient(app)
        response = client.post(
            "/api/v1/tools/grep", json={"pattern": "match", "path": str(outside)}
        )

    assert response.status_code == 403
    assert "escapes allowed root" in response.json()["error"]["message"]


def test_tool_grep_invalid_regex_returns_400(tmp_path: Path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post(
            "/api/v1/tools/grep", json={"pattern": "[invalid(", "path": str(test_file)}
        )

    assert response.status_code == 400
    assert "Invalid regex" in response.json()["error"]["message"]


def test_tool_grep_no_matches_returns_400(tmp_path: Path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("nothing interesting here")

    with patch("nyxgpt.app.get_tools_root", return_value=tmp_path):
        client = TestClient(app)
        response = client.post(
            "/api/v1/tools/grep", json={"pattern": "nonexistent", "path": str(test_file)}
        )

    assert response.status_code == 400


# ----------------------------
# GET /api/v1/rag/config
# ----------------------------


def test_rag_config_returns_default_thresholds():
    client = TestClient(app)
    response = client.get("/api/v1/rag/config")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "min_score": 0.0,
        "good_score_threshold": 0.7,
        "medium_score_threshold": 0.4,
    }


def test_rag_config_reflects_custom_configured_thresholds():
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["rag"] = {
        "min_score": "0.25",
        "good_score_threshold": "0.85",
        "medium_score_threshold": "0.5",
    }

    with patch("nyxgpt.app._req_cfg", return_value=cfg):
        client = TestClient(app)
        response = client.get("/api/v1/rag/config")

    assert response.status_code == 200
    assert response.json() == {
        "min_score": 0.25,
        "good_score_threshold": 0.85,
        "medium_score_threshold": 0.5,
    }
