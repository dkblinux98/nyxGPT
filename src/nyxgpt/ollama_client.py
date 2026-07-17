from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from nyxgpt.tracing import traced_span

logger = logging.getLogger(__name__)


class ModelRuntimeError(RuntimeError):
    """The Ollama/llama-server model runtime failed to load or run a model.

    Raised for upstream failures that are actionable by the user (the model
    crashed, ran out of memory, or timed out) as opposed to generic
    connectivity problems (Ollama itself unreachable). Callers surface
    ``str(exc)`` directly to the chat UI, so the message must stay
    user-facing and specific.
    """


def _model_runtime_message(detail: str) -> str:
    return (
        "Model failed to run — it may require more memory than is available "
        f"on this host ({detail})"
    )


def _model_runtime_message_generic(detail: str) -> str:
    # Used for generic upstream 5xx responses, where memory pressure is a
    # common but not certain cause (unlike a timeout or a dropped connection
    # mid-stream, which are strong OOM/crash signals).
    return (
        "Model failed to run — the model runtime returned an error. This can "
        "happen if the host doesn't have enough free memory to load the "
        f"model, but may also be a transient failure ({detail})"
    )


def _is_timeout_error(exc: urllib.error.URLError) -> bool:
    reason = exc.reason
    if isinstance(reason, TimeoutError):
        return True
    return "timed out" in str(reason).lower()


def _is_connection_error(exc: Exception) -> bool:
    """Check if exception is a connection-related error that should trigger retry."""
    if isinstance(exc, urllib.error.URLError):
        # URLError includes connection refused, timeout, DNS failures, etc.
        return True
    return bool(isinstance(exc, ConnectionError | TimeoutError | OSError))


def _retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 10.0,
    backoff_factor: float = 2.0,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> Any:
    """Retry a function with exponential backoff on connection errors.

    Args:
        func: Function to retry (should raise exception on failure)
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds before first retry
        max_delay: Maximum delay in seconds between retries
        backoff_factor: Multiplier for delay after each retry
        on_retry: Optional callback(attempt, delay, error) called before each retry

    Returns:
        Result from successful function call

    Raises:
        Last exception if all retries fail
    """
    delay = initial_delay

    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            # Don't retry on HTTP errors (4xx, 5xx) - these are not connection issues
            if isinstance(e, urllib.error.HTTPError):
                raise

            # Only retry on connection-related errors
            if not _is_connection_error(e):
                raise

            # Don't retry after last attempt
            if attempt >= max_retries:
                logger.warning(f"Failed to connect to Ollama after {max_retries + 1} attempts: {e}")
                raise

            # Calculate delay with exponential backoff
            current_delay = min(delay, max_delay)

            if on_retry:
                on_retry(attempt + 1, current_delay, e)

            logger.info(
                f"Ollama connection failed (attempt {attempt + 1}/{max_retries + 1}), "
                f"retrying in {current_delay:.1f}s: {e}"
            )

            time.sleep(current_delay)
            delay *= backoff_factor

    # Unreachable when max_retries >= 0 (the last loop iteration always
    # returns or raises); only a negative max_retries skips the loop body
    # entirely and falls through here.
    raise RuntimeError("Retry loop ended unexpectedly")


def post_json(url: str, payload: dict[str, Any], timeout_s: float = 120.0) -> dict[str, Any]:
    with traced_span("ollama.request", url=url):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code >= 500:
                raise ModelRuntimeError(
                    _model_runtime_message_generic(f"Ollama HTTP {e.code}: {body}")
                ) from e
            raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            if _is_timeout_error(e):
                raise ModelRuntimeError(
                    _model_runtime_message(f"no response within {timeout_s:.0f}s")
                ) from e
            raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e


def post_json_lines(
    url: str,
    payload: dict[str, Any],
    timeout_s: float = 120.0,
    max_retries: int = 3,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield decoded JSON objects from a newline-delimited JSON HTTP response.

    Args:
        url: Target URL for POST request
        payload: JSON payload to send
        timeout_s: Request timeout in seconds
        max_retries: Maximum number of connection retry attempts
        on_retry: Optional callback(attempt, delay, error) called before each retry

    Yields:
        Decoded JSON objects from response stream

    Raises:
        RuntimeError: If connection fails after retries or HTTP error occurs
    """
    with traced_span("ollama.request.stream", url=url):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _attempt_request():
            try:
                return urllib.request.urlopen(req, timeout=timeout_s)
            except urllib.error.HTTPError as e:
                # HTTP errors should not be retried, convert to RuntimeError
                body = e.read().decode("utf-8", errors="replace")
                if e.code >= 500:
                    raise ModelRuntimeError(
                        _model_runtime_message_generic(f"Ollama HTTP {e.code}: {body}")
                    ) from e
                raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
            # Let URLError propagate for retry logic to catch

        # Use retry logic to establish connection
        try:
            resp = _retry_with_backoff(
                _attempt_request,
                max_retries=max_retries,
                on_retry=on_retry,
            )
        except urllib.error.URLError as e:
            # Convert to RuntimeError after all retries exhausted
            if _is_timeout_error(e):
                raise ModelRuntimeError(
                    _model_runtime_message(f"no response within {timeout_s:.0f}s")
                ) from e
            raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e

        try:
            with resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    yield json.loads(line)
        except (OSError, http.client.IncompleteRead) as e:
            # The model runtime accepted the connection but the process died
            # mid-generation (e.g. OOM-killed) -- the socket drops instead of
            # returning an HTTP error, so this needs its own actionable message.
            raise ModelRuntimeError(
                _model_runtime_message(f"connection dropped while streaming response: {e}")
            ) from e
        finally:
            resp.close()


def ollama_chat(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: float = 120.0,
    output_format: dict[str, Any] | None = None,
) -> str:
    """Send a chat request to Ollama and return the assistant reply.

    Args:
        base_url: Ollama base URL
        model: Model name to use
        messages: Chat message history
        timeout_s: Request timeout in seconds
        output_format: Optional JSON schema for structured output (Ollama ``format`` field).
            When provided, the model is constrained to produce JSON matching the schema.

    Returns:
        Assistant reply text

    Raises:
        RuntimeError: If the request fails or Ollama returns an unexpected response
    """
    url = base_url.rstrip("/") + "/api/chat"
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if output_format is not None:
        payload["format"] = output_format
    data = post_json(url, payload, timeout_s=timeout_s)

    msg = data.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected Ollama response: {data}")
    return content


def ollama_chat_stream_tokens(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: float = 120.0,
    max_retries: int = 3,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    output_format: dict[str, Any] | None = None,
) -> Iterator[str]:
    """Yield incremental assistant text chunks from Ollama (no printing, no buffering).

    Args:
        base_url: Ollama base URL
        model: Model name to use
        messages: Chat message history
        timeout_s: Request timeout in seconds
        max_retries: Maximum number of connection retry attempts
        on_retry: Optional callback(attempt, delay, error) called before each retry
        output_format: Optional JSON schema for structured output (Ollama ``format`` field).
            When provided, the model is constrained to produce JSON matching the schema.

    Yields:
        Text chunks from assistant response

    Raises:
        RuntimeError: If connection fails after retries or HTTP error occurs
    """
    url = base_url.rstrip("/") + "/api/chat"
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if output_format is not None:
        payload["format"] = output_format

    for obj in post_json_lines(
        url, payload, timeout_s=timeout_s, max_retries=max_retries, on_retry=on_retry
    ):
        msg = obj.get("message") or {}
        part = msg.get("content")
        if isinstance(part, str) and part:
            yield part
        if obj.get("done") is True:
            break


def ollama_chat_stream(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: float = 120.0,
) -> str:
    """Stream from Ollama and return the final assistant message content."""
    return "".join(ollama_chat_stream_tokens(base_url, model, messages, timeout_s=timeout_s))


def delete_json(url: str, payload: dict[str, Any], timeout_s: float = 60.0) -> dict[str, Any]:
    """Send DELETE request with JSON payload to Ollama API.

    Args:
        url: Full URL to send DELETE request to
        payload: Dictionary to send as JSON body
        timeout_s: Request timeout in seconds

    Returns:
        Parsed JSON response as dictionary

    Raises:
        RuntimeError: If request fails or Ollama returns error
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="DELETE",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e


__all__ = [
    "ModelRuntimeError",
    "post_json",
    "post_json_lines",
    "delete_json",
    "ollama_chat",
    "ollama_chat_stream_tokens",
    "ollama_chat_stream",
]
