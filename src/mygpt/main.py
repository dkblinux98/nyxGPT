from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mygpt.config import load_config


def _post_json(url: str, payload: dict[str, Any], timeout_s: float = 120.0) -> dict[str, Any]:
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


# Streaming POST helper: yields JSON objects from newline-delimited JSON HTTP response.
def _post_json_lines(url: str, payload: dict[str, Any], timeout_s: float = 120.0):
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


def _ollama_chat(base_url: str, model: str, messages: list[dict[str, str]]) -> str:
    # Ollama expects /api/chat with messages like: {role: user|assistant|system, content: "..."}
    url = base_url.rstrip("/") + "/api/chat"
    payload = {"model": model, "messages": messages, "stream": False}
    data = _post_json(url, payload)

    msg = (data.get("message") or {})
    content = msg.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected Ollama response: {data}")
    return content


# Streaming chat function: streams tokens from Ollama and returns the final assistant message.
def _ollama_chat_stream(base_url: str, model: str, messages: list[dict[str, str]]) -> str:
    """Stream tokens from Ollama and return the final assistant message content."""
    url = base_url.rstrip("/") + "/api/chat"
    payload = {"model": model, "messages": messages, "stream": True}

    chunks: list[str] = []
    for obj in _post_json_lines(url, payload):
        msg = (obj.get("message") or {})
        part = msg.get("content")
        if isinstance(part, str) and part:
            chunks.append(part)
            print(part, end="", flush=True)
        if obj.get("done") is True:
            break

    # Ensure we end on a newline for a nicer terminal UX.
    if chunks:
        print()

    return "".join(chunks)


def _cmd_info(cfg_path: Path | None) -> int:
    cfg = load_config(cfg_path)
    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    model = cfg.get("mygpt", "default_model", fallback="llama3.1:8b")

    print("myGPT OK")
    print(f"Ollama base_url: {base_url}")
    print(f"Default model: {model}")
    return 0


def _cmd_chat(cfg_path: Path | None, model_override: str | None, system: str | None, prompt: str | None, stream: bool) -> int:
    cfg = load_config(cfg_path)
    base_url = cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")
    model = model_override or cfg.get("mygpt", "default_model", fallback="llama3.1:8b")

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})

    def ask_once(user_text: str) -> str:
        messages.append({"role": "user", "content": user_text})

        if stream:
            reply = _ollama_chat_stream(base_url=base_url, model=model, messages=messages)
        else:
            reply = _ollama_chat(base_url=base_url, model=model, messages=messages)

        messages.append({"role": "assistant", "content": reply})
        return reply

    # Single prompt mode
    if prompt is not None:
        if stream:
            ask_once(prompt)
        else:
            print(ask_once(prompt))
        return 0

    # Interactive mode
    print(f"myGPT chat (model: {model})")
    print("Type /exit to quit.")

    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_text:
            continue
        if user_text.lower() in {"/exit", "/quit"}:
            return 0

        try:
            reply = ask_once(user_text)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            continue

        if not stream:
            print(reply)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mygpt")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.ini (defaults to ~/.myGPT/config.ini)",
    )

    sub = parser.add_subparsers(dest="command")

    # Keep the existing behavior as the default "info" command.
    sub.add_parser("info", help="Show config-derived defaults (base_url, model)")

    chat_p = sub.add_parser("chat", help="Chat with the configured Ollama model")
    chat_p.add_argument("prompt", nargs="?", help="Optional single prompt (otherwise interactive)")
    chat_p.add_argument("--model", dest="model_override", help="Override model for this run")
    chat_p.add_argument("--system", help="Optional system prompt")
    chat_p.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output",
    )

    args = parser.parse_args(argv)

    cmd = args.command or "info"

    if cmd == "info":
        return _cmd_info(args.config)

    if cmd == "chat":
        return _cmd_chat(args.config, args.model_override, args.system, args.prompt, stream=(not args.no_stream))

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
