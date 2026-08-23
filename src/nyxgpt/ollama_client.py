"""HTTP client for talking to a local Ollama (or llama-server-compatible) instance.

Wraps the raw `/api/chat` and delete endpoints with retry-with-backoff on
connection errors and translation of upstream failures (timeouts, dropped
connections, 5xx responses) into `ModelRuntimeError` with a user-facing
message, versus generic `RuntimeError` for non-actionable failures.
"""

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
    """Build a user-facing message for a strong OOM/crash signal (timeout or dropped connection).

    Args:
        detail: Short technical detail appended in parentheses, e.g. the
            underlying exception text.

    Returns:
        Formatted message suitable for raising as `ModelRuntimeError`.
    """
    return (
        "Model failed to run — it may require more memory than is available "
        f"on this host ({detail})"
    )


def _model_runtime_message_generic(detail: str) -> str:
    """Build a user-facing message for a generic upstream 5xx error.

    Used for generic upstream 5xx responses, where memory pressure is a
    common but not certain cause (unlike a timeout or a dropped connection
    mid-stream, which are strong OOM/crash signals).

    Args:
        detail: Short technical detail appended in parentheses, e.g. the
            HTTP status code and response body.

    Returns:
        Formatted message suitable for raising as `ModelRuntimeError`.
    """
    return (
        "Model failed to run — the model runtime returned an error. This can "
        "happen if the host doesn't have enough free memory to load the "
        f"model, but may also be a transient failure ({detail})"
    )


def _is_timeout_error(exc: urllib.error.URLError) -> bool:
    """Return True if `exc` represents a request timeout rather than another connection failure."""
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
    """Send a single JSON POST request and return the parsed JSON response.

    Unlike `post_json_lines`, this does not retry on connection errors -- it
    is used for non-streaming requests where the caller expects a single
    response body.

    Args:
        url: Target URL for the POST request
        payload: JSON payload to send
        timeout_s: Request timeout in seconds

    Returns:
        Parsed JSON response as a dictionary (empty dict if body is empty)

    Raises:
        ModelRuntimeError: If the request times out or the server returns a 5xx
        RuntimeError: If the request otherwise fails or returns a non-5xx HTTP error
    """
    with traced_span("ollama.request", url=url):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read().decode("utf-8")
                logger.debug(
                    "Ollama request completed",
                    extra={
                        "component": "ollama",
                        "url": url,
                        "status": resp.status,
                        "duration_ms": round((time.monotonic() - start) * 1000, 1),
                    },
                )
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            body = e.read().decode("utf-8", errors="replace")
            logger.warning(
                "Ollama request failed",
                extra={
                    "component": "ollama",
                    "url": url,
                    "status": e.code,
                    "duration_ms": duration_ms,
                },
            )
            if e.code >= 500:
                raise ModelRuntimeError(
                    _model_runtime_message_generic(f"Ollama HTTP {e.code}: {body}")
                ) from e
            raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            logger.warning(
                "Ollama request failed",
                extra={
                    "component": "ollama",
                    "url": url,
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )
            if _is_timeout_error(e):
                raise ModelRuntimeError(
                    _model_runtime_message(f"no response within {timeout_s:.0f}s")
                ) from e
            raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e


def get_json(url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """Send a GET request and return the parsed JSON response.

    Args:
        url: Target URL for the GET request
        timeout_s: Request timeout in seconds

    Returns:
        Parsed JSON response as a dictionary (empty dict if body is empty)

    Raises:
        ModelRuntimeError: If the request times out or the server returns a 5xx
        RuntimeError: If the request otherwise fails or returns a non-5xx HTTP error
    """
    with traced_span("ollama.request", url=url):
        req = urllib.request.Request(url, method="GET")

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
            """Open the streaming request once; HTTP errors are converted immediately, connection errors propagate for `_retry_with_backoff`."""
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

        start = time.monotonic()

        # Use retry logic to establish connection
        try:
            resp = _retry_with_backoff(
                _attempt_request,
                max_retries=max_retries,
                on_retry=on_retry,
            )
        except urllib.error.URLError as e:
            logger.warning(
                "Ollama streaming request failed to connect",
                extra={
                    "component": "ollama",
                    "url": url,
                    "error": str(e),
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                },
            )
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
            logger.warning(
                "Ollama streaming request dropped mid-stream",
                extra={
                    "component": "ollama",
                    "url": url,
                    "error": str(e),
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                },
            )
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
    think: bool | None = None,
) -> str:
    """Send a chat request to Ollama and return the assistant reply.

    Args:
        base_url: Ollama base URL
        model: Model name to use
        messages: Chat message history
        timeout_s: Request timeout in seconds
        output_format: Optional JSON schema for structured output (Ollama ``format`` field).
            When provided, the model is constrained to produce JSON matching the schema.
        think: Whether the model may emit chain-of-thought (Ollama ``think`` field).
            ``None`` leaves it unset and the model decides. See ``[nyxgpt] think``.

    Returns:
        Assistant reply text

    Raises:
        RuntimeError: If the request fails or Ollama returns an unexpected response
    """
    url = base_url.rstrip("/") + "/api/chat"
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if output_format is not None:
        payload["format"] = output_format
    if think is not None:
        payload["think"] = think
    data = post_json(url, payload, timeout_s=timeout_s)

    msg = data.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected Ollama response: {data}")
    if not content.strip() and (msg.get("thinking") or "").strip():
        # A reasoning model that spent its whole budget thinking returns HTTP
        # 200 with an empty `content` and a full `thinking`. Returning "" made
        # that indistinguishable from a model with nothing to say: the user saw
        # a blank reply and no error, and every smoke had to discover it by
        # asserting on emptiness. Name it instead (#4028).
        raise RuntimeError(
            f"{model} returned reasoning but no answer "
            f"({len(msg['thinking'])} chars of thinking, empty content). "
            "Set `[nyxgpt] think = false` to stop it reasoning, or raise "
            "`chat_timeout_seconds` so it can finish."
        )
    return content


def ollama_chat_stream_tokens(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: float = 120.0,
    max_retries: int = 3,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    output_format: dict[str, Any] | None = None,
    think: bool | None = None,
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
        think: Whether the model may emit chain-of-thought (Ollama ``think`` field).
            ``None`` leaves it unset and the model decides. See ``[nyxgpt] think``.

    Yields:
        Text chunks from assistant response

    Raises:
        RuntimeError: If connection fails after retries, an HTTP error occurs, or
            the model returned reasoning and no answer (empty ``content`` with a
            non-empty ``thinking``) -- raised on ``done``, before which nothing
            has been yielded.
    """
    url = base_url.rstrip("/") + "/api/chat"
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if output_format is not None:
        payload["format"] = output_format
    if think is not None:
        payload["think"] = think

    start = time.monotonic()
    # The same reasoning-with-no-answer case the non-streaming path names, and
    # this is the path the web UI uses -- so without it the silent blank reply
    # survives exactly where a user would meet it (#4029 review). Raising on
    # `done` is safe: nothing has been yielded in that case, so there is no
    # half-emitted answer to contradict.
    yielded_content = False
    saw_thinking = False
    for obj in post_json_lines(
        url, payload, timeout_s=timeout_s, max_retries=max_retries, on_retry=on_retry
    ):
        msg = obj.get("message") or {}
        part = msg.get("content")
        if isinstance(part, str) and part:
            # `strip()` for the guard, matching the non-streaming path: a reply
            # of pure whitespace is not an answer, and counting it as one let
            # the blank-reply case through here while the other path caught it.
            if part.strip():
                yielded_content = True
            yield part
        thinking_part = msg.get("thinking")
        if isinstance(thinking_part, str) and thinking_part.strip():
            saw_thinking = True
        if obj.get("done") is True:
            if not yielded_content and saw_thinking:
                raise RuntimeError(
                    f"{model} returned reasoning but no answer (streaming). "
                    "Set `[nyxgpt] think = false` to stop it reasoning, or raise "
                    "`chat_timeout_seconds` so it can finish."
                )
            logger.debug(
                "Ollama streaming request completed",
                extra={
                    "component": "ollama",
                    "url": url,
                    "model": model,
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                },
            )
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
    "get_json",
    "post_json",
    "post_json_lines",
    "delete_json",
    "ollama_chat",
    "ollama_chat_stream_tokens",
    "ollama_chat_stream",
]
