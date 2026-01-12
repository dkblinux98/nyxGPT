"""Tests for Ollama reconnection logic."""
from __future__ import annotations

import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from mygpt.ollama_client import (
    _is_connection_error,
    _retry_with_backoff,
    post_json_lines,
)

pytestmark = pytest.mark.unit


# ============================================================================
# Connection Error Detection Tests
# ============================================================================


def test_is_connection_error_url_error() -> None:
    """Test that URLError is identified as a connection error."""
    error = urllib.error.URLError("Connection refused")
    assert _is_connection_error(error) is True


def test_is_connection_error_connection_error() -> None:
    """Test that ConnectionError is identified as a connection error."""
    error = ConnectionError("Connection reset")
    assert _is_connection_error(error) is True


def test_is_connection_error_timeout_error() -> None:
    """Test that TimeoutError is identified as a connection error."""
    error = TimeoutError("Request timed out")
    assert _is_connection_error(error) is True


def test_is_connection_error_os_error() -> None:
    """Test that OSError is identified as a connection error."""
    error = OSError("Network unreachable")
    assert _is_connection_error(error) is True


def test_is_connection_error_other_exception() -> None:
    """Test that non-connection errors return False."""
    error = ValueError("Invalid input")
    assert _is_connection_error(error) is False


# ============================================================================
# Retry with Backoff Tests
# ============================================================================


def test_retry_with_backoff_succeeds_first_try() -> None:
    """Test that successful function returns immediately without retry."""
    mock_func = MagicMock(return_value="success")
    mock_callback = MagicMock()

    result = _retry_with_backoff(mock_func, on_retry=mock_callback)

    assert result == "success"
    mock_func.assert_called_once()
    mock_callback.assert_not_called()


def test_retry_with_backoff_succeeds_after_retry() -> None:
    """Test that function succeeds after transient connection error."""
    # Fail twice, then succeed
    mock_func = MagicMock(
        side_effect=[
            urllib.error.URLError("Connection refused"),
            urllib.error.URLError("Connection refused"),
            "success",
        ]
    )
    mock_callback = MagicMock()

    with patch("time.sleep"):  # Don't actually sleep in tests
        result = _retry_with_backoff(
            mock_func,
            max_retries=3,
            initial_delay=0.1,
            on_retry=mock_callback,
        )

    assert result == "success"
    assert mock_func.call_count == 3
    assert mock_callback.call_count == 2  # Called before 2nd and 3rd attempts


def test_retry_with_backoff_exhausts_retries() -> None:
    """Test that retry gives up after max attempts."""
    mock_func = MagicMock(side_effect=urllib.error.URLError("Connection refused"))
    mock_callback = MagicMock()

    with pytest.raises(urllib.error.URLError):
        with patch("time.sleep"):
            _retry_with_backoff(
                mock_func,
                max_retries=2,
                initial_delay=0.1,
                on_retry=mock_callback,
            )

    # Should try 3 times total (initial + 2 retries)
    assert mock_func.call_count == 3
    # Callback called before 2nd and 3rd attempts
    assert mock_callback.call_count == 2


def test_retry_with_backoff_exponential_delay() -> None:
    """Test that delay increases exponentially with backoff factor."""
    mock_func = MagicMock(side_effect=urllib.error.URLError("Connection refused"))
    mock_callback = MagicMock()

    with pytest.raises(urllib.error.URLError):
        with patch("time.sleep") as mock_sleep:
            _retry_with_backoff(
                mock_func,
                max_retries=3,
                initial_delay=1.0,
                backoff_factor=2.0,
                on_retry=mock_callback,
            )

    # Callback should receive increasing delays
    # attempt 1: 1.0s, attempt 2: 2.0s, attempt 3: 4.0s
    calls = mock_callback.call_args_list
    assert len(calls) == 3

    # First retry (attempt 1)
    assert calls[0][0][0] == 1  # attempt number
    assert calls[0][0][1] == 1.0  # delay

    # Second retry (attempt 2)
    assert calls[1][0][0] == 2
    assert calls[1][0][1] == 2.0

    # Third retry (attempt 3)
    assert calls[2][0][0] == 3
    assert calls[2][0][1] == 4.0


def test_retry_with_backoff_max_delay() -> None:
    """Test that delay is capped at max_delay."""
    mock_func = MagicMock(side_effect=urllib.error.URLError("Connection refused"))
    mock_callback = MagicMock()

    with pytest.raises(urllib.error.URLError):
        with patch("time.sleep"):
            _retry_with_backoff(
                mock_func,
                max_retries=5,
                initial_delay=1.0,
                max_delay=3.0,  # Cap at 3 seconds
                backoff_factor=2.0,
                on_retry=mock_callback,
            )

    # Check that delays are capped
    calls = mock_callback.call_args_list
    delays = [call[0][1] for call in calls]

    # 1.0, 2.0, 3.0 (capped), 3.0 (capped), 3.0 (capped)
    assert delays == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_retry_with_backoff_no_retry_on_http_error() -> None:
    """Test that HTTP errors (4xx, 5xx) are not retried."""
    mock_response = MagicMock()
    mock_response.read.return_value = b"Not Found"

    http_error = urllib.error.HTTPError(
        url="http://localhost/api",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=mock_response,
    )

    mock_func = MagicMock(side_effect=http_error)
    mock_callback = MagicMock()

    with pytest.raises(urllib.error.HTTPError):
        _retry_with_backoff(mock_func, max_retries=3, on_retry=mock_callback)

    # Should not retry on HTTP errors
    mock_func.assert_called_once()
    mock_callback.assert_not_called()


def test_retry_with_backoff_no_retry_on_other_errors() -> None:
    """Test that non-connection errors are not retried."""
    mock_func = MagicMock(side_effect=ValueError("Invalid input"))
    mock_callback = MagicMock()

    with pytest.raises(ValueError):
        _retry_with_backoff(mock_func, max_retries=3, on_retry=mock_callback)

    # Should not retry on non-connection errors
    mock_func.assert_called_once()
    mock_callback.assert_not_called()


def test_retry_with_backoff_callback_receives_error() -> None:
    """Test that retry callback receives the error that triggered retry."""
    error = urllib.error.URLError("Connection refused")
    mock_func = MagicMock(side_effect=[error, "success"])
    mock_callback = MagicMock()

    with patch("time.sleep"):
        _retry_with_backoff(mock_func, max_retries=2, on_retry=mock_callback)

    # Verify callback was called with the error
    mock_callback.assert_called_once()
    call_args = mock_callback.call_args[0]
    assert call_args[2] is error  # Third argument is the exception


# ============================================================================
# post_json_lines Retry Integration Tests
# ============================================================================


def test_post_json_lines_retry_on_connection_error() -> None:
    """Test that post_json_lines retries on connection errors."""
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.close = MagicMock()
    mock_response.__iter__ = MagicMock(return_value=iter([b'{"status": "ok"}\n']))

    # Fail once, then succeed
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            urllib.error.URLError("Connection refused"),
            mock_response,
        ]

        with patch("time.sleep"):  # Don't actually sleep
            results = list(
                post_json_lines(
                    "http://localhost/api",
                    {"prompt": "test"},
                    max_retries=2,
                )
            )

    assert len(results) == 1
    assert results[0] == {"status": "ok"}
    assert mock_urlopen.call_count == 2  # Initial + 1 retry


def test_post_json_lines_callback_invoked_on_retry() -> None:
    """Test that post_json_lines invokes retry callback."""
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.close = MagicMock()
    mock_response.__iter__ = MagicMock(return_value=iter([b'{"done": true}\n']))

    mock_callback = MagicMock()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            urllib.error.URLError("Connection refused"),
            mock_response,
        ]

        with patch("time.sleep"):
            list(
                post_json_lines(
                    "http://localhost/api",
                    {"prompt": "test"},
                    max_retries=2,
                    on_retry=mock_callback,
                )
            )

    # Verify callback was invoked
    mock_callback.assert_called_once()
    call_args = mock_callback.call_args[0]
    assert call_args[0] == 1  # First retry (attempt 1)
    assert isinstance(call_args[1], float)  # Delay
    assert isinstance(call_args[2], urllib.error.URLError)  # Error


def test_post_json_lines_handles_undefined_resp_in_finally() -> None:
    """Test that finally block handles NameError when resp is never assigned.

    This tests the edge case where _retry_with_backoff raises before returning
    a response object, ensuring the finally block doesn't cause a NameError.
    """
    with patch("urllib.request.urlopen") as mock_urlopen:
        # All attempts fail - resp is never assigned
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with patch("time.sleep"):  # Don't actually sleep
            with pytest.raises(RuntimeError, match="Failed to reach Ollama"):
                # Consume the generator to trigger the finally block
                list(
                    post_json_lines(
                        "http://localhost/api",
                        {"prompt": "test"},
                        max_retries=2,
                    )
                )

    # Test passes if no NameError is raised in the finally block
