"""Ollama model management operations.

This module provides functions for managing Ollama models including
listing, pulling, deleting, and inspecting models.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nyxgpt.config import get_ollama_base_url, load_config
from nyxgpt.ollama_client import delete_json, get_json, post_json, post_json_lines


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
) -> dict[str, Any]:
    """Download a model from Ollama library.

    Args:
        name: Model name to download (e.g., 'llama3.1:8b')
        base_url: Ollama base URL. If None, loads from config.
        progress_callback: Optional callback for progress updates.
            Called with (status: str, percentage: float) for each
            progress event. Percentage ranges from 0.0 to 100.0.

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

    if progress_callback:
        # Stream progress events
        last_result = {}
        for event in post_json_lines(url, payload, timeout_s=600.0):
            last_result = event
            status = event.get("status", "")
            # Calculate percentage from completed/total if available
            completed = event.get("completed", 0)
            total = event.get("total", 0)
            percentage = (completed / total * 100) if total > 0 else 0.0
            progress_callback(status, percentage)
        return last_result
    else:
        # Non-streaming pull
        return post_json(url, {**payload, "stream": False}, timeout_s=600.0)


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
    "list_models",
    "pull_model",
    "delete_model",
    "show_model",
    "format_model_size",
]
