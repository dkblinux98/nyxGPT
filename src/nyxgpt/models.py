"""Ollama model management operations.

This module provides functions for managing Ollama models including
listing, pulling, deleting, and inspecting models.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nyxgpt.config import get_ollama_base_url, load_config
from nyxgpt.ollama_client import (
    ModelRuntimeError,
    _retry_with_backoff,
    delete_json,
    get_json,
    post_json,
    post_json_lines,
)

logger = logging.getLogger(__name__)

#: How many times `pull_model` asks Ollama to download a model before giving
#: up, and how long it waits between attempts (5s, 10s, 20s, 40s).
#:
#: A pull is the one Ollama request whose failures are mostly NOT about this
#: host: Ollama fetches the manifest and blobs from the public model registry
#: and relays that failure as its own 5xx. A registry blip therefore failed
#: `nyxgpt ops install` outright -- on 2026-10-23 it reddened the required
#: `terraform-artifact-smoke` check with `503: upstream connect error or
#: disconnect/reset before headers`, having nothing to do with the commit
#: under test, and it would have failed a user's install just as flatly.
#:
#: These numbers are deliberately the ones `k8s/statefulset-ollama.yaml`'s
#: postStart hook already used: that copy of "pull the required models" was
#: the only one of the three that retried, and the others diverging from it
#: is what left this path with a single attempt.
#: `tests/unit/test_model_pull_retry.py` holds all three copies to one rule.
MODEL_PULL_ATTEMPTS = 5
MODEL_PULL_RETRY_INITIAL_DELAY_S = 5.0
MODEL_PULL_RETRY_MAX_DELAY_S = 40.0


def _pull_failure_is_worth_retrying(exc: Exception) -> bool:
    """Return True if a failed pull should be attempted again.

    The signal is the exception TYPE, not text in the message: `post_json` /
    `post_json_lines` raise `ModelRuntimeError` for exactly the upstream
    conditions a retry can clear -- a 5xx answer (which is how Ollama relays a
    registry failure), a timeout, or a connection dropped mid-stream -- and a
    plain `RuntimeError` for an answer that will not change on its own (a 404
    for a model that does not exist, a 400 for a malformed name). Matching a
    substring of the message instead is how the same mistake was made in
    `classify_error` (#4192, criterion 2): `529` in a commit SHA is not an
    overload, and "503" in a model name would not be a registry outage.
    """
    return isinstance(exc, ModelRuntimeError)


def list_models(base_url: str | None = None) -> list[dict[str, Any]]:
    """List all available Ollama models.

    Args:
        base_url: Ollama base URL. If None, loads from config.

    Returns:
        List of model dictionaries with 'name', 'size', 'modified_at', etc.

    Raises:
        RuntimeError: If Ollama API is unreachable or returns error

    Example:
        >>> models = list_models()
        >>> for model in models:
        ...     print(model['name'], format_model_size(model['size']))
        llama3.1:8b 4.7 GB
    """
    if base_url is None:
        cfg = load_config(None)
        base_url = get_ollama_base_url(cfg)

    url = f"{base_url.rstrip('/')}/api/tags"
    data = get_json(url, timeout_s=10.0)
    models: list[dict[str, Any]] = data.get("models", [])
    return models


def pull_model(
    name: str,
    base_url: str | None = None,
    progress_callback: Callable[[str, float], None] | None = None,
    timeout_s: float = 600.0,
    attempts: int = MODEL_PULL_ATTEMPTS,
) -> dict[str, Any]:
    """Download a model from Ollama library.

    A transient upstream failure is retried (`MODEL_PULL_ATTEMPTS` attempts,
    5s doubling to 40s) rather than reported as a missing model: the download
    comes from the public registry, so a failed pull is often a statement
    about the registry and not about this host or this model. Ollama resumes
    from the blobs it already has, so a retry re-downloads only the remainder.
    A definite answer -- 404 for a model that does not exist, 400 for a bad
    name -- is raised on the first attempt, unretried.

    Args:
        name: Model name to download (e.g., 'llama3.1:8b')
        base_url: Ollama base URL. If None, loads from config.
        progress_callback: Optional callback for progress updates.
            Called with (status: str, percentage: float) for each
            progress event. Percentage ranges from 0.0 to 100.0. A retried
            attempt restarts the progress events from the beginning.
        timeout_s: Request timeout in seconds for the pull.
        attempts: Total attempts, including the first. 1 disables retrying;
            tests use it to drive the no-retry behavior this replaced.

    Returns:
        Final pull status dictionary

    Raises:
        RuntimeError: If pull fails or Ollama unreachable
        ValueError: If model name is invalid

    Example:
        >>> def show_progress(status, pct):
        ...     print(f"{status}: {pct:.1f}%")
        >>> result = pull_model("llama3.1:8b", progress_callback=show_progress)
        pulling manifest: 50.0%
        downloading: 75.0%
        >>> print(result.get("status"))
        success
    """
    if not name or not name.strip():
        raise ValueError("Model name cannot be empty")

    if base_url is None:
        cfg = load_config(None)
        base_url = get_ollama_base_url(cfg)

    url = f"{base_url.rstrip('/')}/api/pull"
    payload = {"name": name.strip(), "stream": bool(progress_callback)}

    def _pull_once() -> dict[str, Any]:
        if progress_callback:
            # Stream progress events
            last_result = {}
            for event in post_json_lines(url, payload, timeout_s=timeout_s):
                last_result = event
                status = event.get("status", "")
                # Calculate percentage from completed/total if available
                completed = event.get("completed", 0)
                total = event.get("total", 0)
                percentage = (completed / total * 100) if total > 0 else 0.0
                progress_callback(status, percentage)
            return last_result
        # Non-streaming pull
        return post_json(url, {**payload, "stream": False}, timeout_s=timeout_s)

    def _log_retry(attempt: int, delay: float, error: Exception) -> None:
        logger.warning(
            "Pull of model '%s' failed (attempt %d/%d), retrying in %.0fs: %s",
            name.strip(),
            attempt,
            max(attempts, 1),
            delay,
            error,
            extra={"component": "models", "action": "pull", "model": name.strip()},
        )

    result: dict[str, Any] = _retry_with_backoff(
        _pull_once,
        max_retries=max(attempts, 1) - 1,
        initial_delay=MODEL_PULL_RETRY_INITIAL_DELAY_S,
        max_delay=MODEL_PULL_RETRY_MAX_DELAY_S,
        backoff_factor=2.0,
        on_retry=_log_retry,
        should_retry=_pull_failure_is_worth_retrying,
    )
    return result


def delete_model(name: str, base_url: str | None = None) -> None:
    """Delete a model from Ollama.

    Args:
        name: Model name to delete (e.g., 'llama3.1:8b')
        base_url: Ollama base URL. If None, loads from config.

    Raises:
        RuntimeError: If deletion fails or Ollama unreachable
        ValueError: If model name is invalid

    Example:
        >>> delete_model("mistral:7b")
    """
    if not name or not name.strip():
        raise ValueError("Model name cannot be empty")

    if base_url is None:
        cfg = load_config(None)
        base_url = get_ollama_base_url(cfg)

    url = f"{base_url.rstrip('/')}/api/delete"
    delete_json(url, {"name": name.strip()}, timeout_s=30.0)


def show_model(name: str, base_url: str | None = None) -> dict[str, Any]:
    """Get detailed information about a model.

    Args:
        name: Model name to inspect (e.g., 'llama3.1:8b')
        base_url: Ollama base URL. If None, loads from config.

    Returns:
        Dictionary with model details including:
            - modelfile: The Modelfile content
            - parameters: Model parameters
            - template: Prompt template
            - size: Model size in bytes
            - modified_at: Last modified timestamp

    Raises:
        RuntimeError: If model not found or Ollama unreachable
        ValueError: If model name is invalid

    Example:
        >>> info = show_model("llama3.1:8b")
        >>> print(info.get("parameters"))
        >>> print(f"Size: {format_model_size(info.get('size', 0))}")
    """
    if not name or not name.strip():
        raise ValueError("Model name cannot be empty")

    if base_url is None:
        cfg = load_config(None)
        base_url = get_ollama_base_url(cfg)

    url = f"{base_url.rstrip('/')}/api/show"
    return post_json(url, {"name": name.strip()}, timeout_s=10.0)


def format_model_size(size_bytes: int) -> str:
    """Format size in bytes to human-readable string.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted string (e.g., '4.7 GB', '1.2 MB')

    Example:
        >>> format_model_size(4700000000)
        '4.4 GB'
        >>> format_model_size(1536)
        '1.5 KB'
    """
    if size_bytes < 0:
        return "0 B"

    size_float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_float < 1024.0:
            return f"{size_float:.1f} {unit}"
        size_float /= 1024.0

    return f"{size_float:.1f} PB"


__all__ = [
    "MODEL_PULL_ATTEMPTS",
    "MODEL_PULL_RETRY_INITIAL_DELAY_S",
    "MODEL_PULL_RETRY_MAX_DELAY_S",
    "list_models",
    "pull_model",
    "delete_model",
    "show_model",
    "format_model_size",
]
