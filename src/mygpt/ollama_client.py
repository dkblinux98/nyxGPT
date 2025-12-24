from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable, Iterator


def post_json(url: str, payload: dict[str, Any], timeout_s: float = 120.0) -> dict[str, Any]:
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
        raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e


def post_json_lines(url: str, payload: dict[str, Any], timeout_s: float = 120.0) -> Iterable[dict[str, Any]]:
    """Yield decoded JSON objects from a newline-delimited JSON HTTP response."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                yield json.loads(line)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Ollama at {url}: {e}") from e


def ollama_chat(base_url: str, model: str, messages: list[dict[str, str]], timeout_s: float = 120.0) -> str:
    url = base_url.rstrip("/") + "/api/chat"
    payload = {"model": model, "messages": messages, "stream": False}
    data = post_json(url, payload, timeout_s=timeout_s)

    msg = (data.get("message") or {})
    content = msg.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected Ollama response: {data}")
    return content


def ollama_chat_stream_tokens(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: float = 120.0,
) -> Iterator[str]:
    """Yield incremental assistant text chunks from Ollama (no printing, no buffering)."""
    url = base_url.rstrip("/") + "/api/chat"
    payload = {"model": model, "messages": messages, "stream": True}

    for obj in post_json_lines(url, payload, timeout_s=timeout_s):
        msg = (obj.get("message") or {})
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


__all__ = [
    "post_json",
    "post_json_lines",
    "ollama_chat",
    "ollama_chat_stream_tokens",
    "ollama_chat_stream",
]
