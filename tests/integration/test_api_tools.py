"""Integration tests for /api/v1/tools/* endpoints.

Tests verify the end-to-end flow of filesystem tools API endpoints:
- POST /api/v1/tools/ls (list directory)
- POST /api/v1/tools/cat (read file)
- POST /api/v1/tools/grep (search in file/directory)

Related: src/mygpt/app.py (lines 1367-1389), src/mygpt/tools_fs.py
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


@pytest.mark.integration
def test_tools_ls_endpoint_lists_directory(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/ls lists directory contents."""
    # Create test directory with files
    test_dir = tmp_path / "test_ls"
    test_dir.mkdir()
    (test_dir / "file1.txt").write_text("content1")
    (test_dir / "file2.txt").write_text("content2")
    (test_dir / "subdir").mkdir()

    # Call API endpoint
    payload = {"path": str(test_dir)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/ls",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    output = data["output"]
    assert "file1.txt" in output
    assert "file2.txt" in output
    assert "subdir/" in output


@pytest.mark.integration
def test_tools_ls_endpoint_single_file(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/ls with single file path."""
    test_file = tmp_path / "single.txt"
    test_file.write_text("content")

    payload = {"path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/ls",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    assert str(test_file) in data["output"]


@pytest.mark.integration
def test_tools_ls_endpoint_nonexistent_path(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/ls returns 400 for nonexistent path."""
    nonexistent = tmp_path / "nonexistent"

    payload = {"path": str(nonexistent)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/ls",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 400
    data = r.json()
    assert "error" in data
    assert "message" in data["error"]
    assert "No such path" in data["error"]["message"]


@pytest.mark.integration
def test_tools_ls_endpoint_response_format(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/ls returns correct response format."""
    test_dir = tmp_path / "format_test"
    test_dir.mkdir()

    payload = {"path": str(test_dir)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/ls",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    # Verify response matches ToolTextResponse model
    assert isinstance(data, dict)
    assert "output" in data
    assert isinstance(data["output"], str)


@pytest.mark.integration
def test_tools_cat_endpoint_reads_file(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/cat reads file content."""
    test_file = tmp_path / "test.txt"
    content = "Line 1\nLine 2\nLine 3"
    test_file.write_text(content)

    payload = {"path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/cat",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    assert content in data["output"]


@pytest.mark.integration
def test_tools_cat_endpoint_with_head_limit(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/cat with head parameter."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5")

    payload = {"path": str(test_file), "head": 3}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/cat",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    output = data["output"].strip()
    lines = output.split("\n")
    assert len(lines) == 3
    assert "Line 1" in output
    assert "Line 3" in output
    assert "Line 4" not in output


@pytest.mark.integration
def test_tools_cat_endpoint_with_tail_limit(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/cat with tail parameter."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5")

    payload = {"path": str(test_file), "tail": 2}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/cat",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    output = data["output"].strip()
    lines = output.split("\n")
    assert len(lines) == 2
    assert "Line 4" in output
    assert "Line 5" in output
    assert "Line 1" not in output


@pytest.mark.integration
def test_tools_cat_endpoint_nonexistent_file(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/cat returns 400 for nonexistent file."""
    nonexistent = tmp_path / "nonexistent.txt"

    payload = {"path": str(nonexistent)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/cat",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 400
    data = r.json()
    assert "error" in data
    assert "message" in data["error"]
    assert "No such file" in data["error"]["message"]


@pytest.mark.integration
def test_tools_cat_endpoint_response_format(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/cat returns correct response format."""
    test_file = tmp_path / "format.txt"
    test_file.write_text("content")

    payload = {"path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/cat",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    # Verify response matches ToolTextResponse model
    assert isinstance(data, dict)
    assert "output" in data
    assert isinstance(data["output"], str)


@pytest.mark.integration
def test_tools_grep_endpoint_finds_pattern(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/grep finds pattern in file."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1: hello\nLine 2: world\nLine 3: hello again")

    payload = {"pattern": "hello", "path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/grep",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    output = data["output"]
    assert "hello" in output
    assert f"{test_file}:1:" in output
    assert f"{test_file}:3:" in output


@pytest.mark.integration
def test_tools_grep_endpoint_regex_pattern(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/grep with regex pattern."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("test123\ntest456\nabc789")

    payload = {"pattern": r"test\d+", "path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/grep",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    output = data["output"]
    assert "test123" in output
    assert "test456" in output
    assert "abc789" not in output


@pytest.mark.integration
def test_tools_grep_endpoint_directory_search(
    api_base_url: str, tmp_path: Path
) -> None:
    """Test POST /api/v1/tools/grep searches in directory."""
    test_dir = tmp_path / "search_dir"
    test_dir.mkdir()
    (test_dir / "file1.txt").write_text("match in file1")
    (test_dir / "file2.txt").write_text("no pattern here")

    payload = {"pattern": "match", "path": str(test_dir)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/grep",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    output = data["output"]
    assert "file1.txt" in output
    assert "match in file1" in output


@pytest.mark.integration
def test_tools_grep_endpoint_max_matches(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/grep respects max parameter."""
    test_file = tmp_path / "test.txt"
    # Create many lines with matches
    content = "\n".join([f"match line {i}" for i in range(100)])
    test_file.write_text(content)

    payload = {"pattern": "match", "path": str(test_file), "max": 5}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/grep",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    assert "output" in data
    output = data["output"]
    lines = [line for line in output.split("\n") if line.strip()]
    # Should be limited to 5 matches
    assert len(lines) <= 5


@pytest.mark.integration
def test_tools_grep_endpoint_no_matches(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/grep returns 400 when no matches found."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3")

    payload = {"pattern": "nonexistent", "path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/grep",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 400


@pytest.mark.integration
def test_tools_grep_endpoint_invalid_regex(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/grep returns 400 for invalid regex."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    payload = {"pattern": "[invalid(", "path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/grep",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 400
    data = r.json()
    assert "error" in data
    assert "message" in data["error"]
    assert "Invalid regex" in data["error"]["message"]


@pytest.mark.integration
def test_tools_grep_endpoint_response_format(api_base_url: str, tmp_path: Path) -> None:
    """Test POST /api/v1/tools/grep returns correct response format."""
    test_file = tmp_path / "format.txt"
    test_file.write_text("match this line")

    payload = {"pattern": "match", "path": str(test_file)}
    r = httpx.post(
        f"{api_base_url}/api/v1/tools/grep",
        json=payload,
        timeout=5.0,
    )

    assert r.status_code == 200
    data = r.json()
    # Verify response matches ToolTextResponse model
    assert isinstance(data, dict)
    assert "output" in data
    assert isinstance(data["output"], str)
