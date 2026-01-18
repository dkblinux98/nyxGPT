from __future__ import annotations


import httpx
import pytest


@pytest.mark.integration
def test_logs_list_files(api_base_url: str) -> None:
    """Test listing log files."""
    r = httpx.get(f"{api_base_url}/api/v1/logs/files", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "files" in data
    assert isinstance(data["files"], list)
    assert "log_dir" in data


@pytest.mark.integration
def test_logs_view_file(api_base_url: str) -> None:
    """Test viewing a log file."""
    # First, get the list of available log files
    r = httpx.get(f"{api_base_url}/api/v1/logs/files", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    files = data.get("files", [])

    # If there are no log files, this test should still pass
    if not files:
        pytest.skip("No log files available for testing")

    # Get the first log file
    first_file = files[0]["name"]

    # View the file
    r = httpx.get(f"{api_base_url}/api/v1/logs/view/{first_file}", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "filename" in data
    assert "lines" in data
    assert "total_lines" in data
    assert "filtered_lines" in data
    assert isinstance(data["lines"], list)
    assert data["filename"] == first_file


@pytest.mark.integration
def test_logs_view_file_with_tail(api_base_url: str) -> None:
    """Test viewing a log file with tail parameter."""
    # First, get the list of available log files
    r = httpx.get(f"{api_base_url}/api/v1/logs/files", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    files = data.get("files", [])

    if not files:
        pytest.skip("No log files available for testing")

    first_file = files[0]["name"]

    # View with tail=10
    r = httpx.get(f"{api_base_url}/api/v1/logs/view/{first_file}?tail=10", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "lines" in data
    assert len(data["lines"]) <= 10


@pytest.mark.integration
def test_logs_view_file_with_level_filter(api_base_url: str) -> None:
    """Test viewing a log file with level filter."""
    r = httpx.get(f"{api_base_url}/api/v1/logs/files", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    files = data.get("files", [])

    if not files:
        pytest.skip("No log files available for testing")

    first_file = files[0]["name"]

    # View with level filter
    r = httpx.get(f"{api_base_url}/api/v1/logs/view/{first_file}?level=ERROR", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert "lines" in data
    # All lines should contain ERROR (if any lines returned)
    for line in data["lines"]:
        assert "ERROR" in line


@pytest.mark.integration
def test_logs_view_file_with_search(api_base_url: str) -> None:
    """Test viewing a log file with search filter."""
    r = httpx.get(f"{api_base_url}/api/v1/logs/files", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    files = data.get("files", [])

    if not files:
        pytest.skip("No log files available for testing")

    first_file = files[0]["name"]

    # View with search filter
    search_term = "mygpt"
    r = httpx.get(
        f"{api_base_url}/api/v1/logs/view/{first_file}?search={search_term}",
        timeout=5.0,
    )
    assert r.status_code == 200
    data = r.json()
    assert "lines" in data
    # All lines should contain the search term (case-insensitive, if any lines returned)
    for line in data["lines"]:
        assert search_term.lower() in line.lower()


@pytest.mark.integration
def test_logs_view_nonexistent_file(api_base_url: str) -> None:
    """Test viewing a nonexistent log file returns 404."""
    r = httpx.get(
        f"{api_base_url}/api/v1/logs/view/nonexistent.log",
        timeout=5.0,
    )
    assert r.status_code == 404


@pytest.mark.integration
def test_logs_view_path_traversal_protection(api_base_url: str) -> None:
    """Test that path traversal attempts are blocked."""
    # Try to access a file outside the log directory
    r = httpx.get(
        f"{api_base_url}/api/v1/logs/view/../../../etc/passwd",
        timeout=5.0,
    )
    # Should either return 403 (Access denied) or 404 (Not found)
    assert r.status_code in [403, 404]


@pytest.mark.integration
def test_logs_stream_file(api_base_url: str) -> None:
    """Test streaming a log file."""
    r = httpx.get(f"{api_base_url}/api/v1/logs/files", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    files = data.get("files", [])

    if not files:
        pytest.skip("No log files available for testing")

    first_file = files[0]["name"]

    # Stream the file
    with httpx.stream(
        "GET",
        f"{api_base_url}/api/v1/logs/stream/{first_file}",
        timeout=httpx.Timeout(10.0, connect=5.0, read=10.0),
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/plain; charset=utf-8"

        # Read at least one chunk
        got_any_chunk = False
        for _text in r.iter_text():
            got_any_chunk = True
            break

    assert got_any_chunk is True


@pytest.mark.integration
def test_logs_stream_with_filters(api_base_url: str) -> None:
    """Test streaming a log file with filters."""
    r = httpx.get(f"{api_base_url}/api/v1/logs/files", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    files = data.get("files", [])

    if not files:
        pytest.skip("No log files available for testing")

    first_file = files[0]["name"]

    # Stream with level filter
    with httpx.stream(
        "GET",
        f"{api_base_url}/api/v1/logs/stream/{first_file}?level=INFO",
        timeout=httpx.Timeout(10.0, connect=5.0, read=10.0),
    ) as r:
        assert r.status_code == 200

        # Read chunks and verify filtering
        chunks = []
        for text in r.iter_text():
            chunks.append(text)
            if len(chunks) >= 5:  # Sample first 5 chunks
                break

        # If we got any chunks, they should contain INFO
        for chunk in chunks:
            if chunk.strip():  # Skip empty chunks
                assert "INFO" in chunk
