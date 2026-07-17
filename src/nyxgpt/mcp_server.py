"""Minimal MCP (Model Context Protocol) server for nyxGPT.

Exposes nyxGPT capabilities as tools over the stdio transport so that
MCP-compatible clients (e.g. Claude Desktop) can query local models via Ollama.

Protocol: JSON-RPC 2.0 over stdio (newline-delimited messages).
Spec version: 2024-11-05

Exposed tools
-------------
chat
    Send a message to nyxGPT and receive a reply.
list_sessions
    List all available chat sessions.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

# MCP protocol version this server implements.
PROTOCOL_VERSION = "2024-11-05"

# ------------------------------------------------------------------
# Tool definitions
# ------------------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "chat",
        "description": (
            "Send a message to nyxGPT (backed by a local Ollama model) and receive a reply. "
            "Optionally specify a session name to maintain conversation history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The user message to send.",
                },
                "session": {
                    "type": "string",
                    "description": "Session name for conversation history (default: 'default').",
                    "default": "default",
                },
                "model": {
                    "type": "string",
                    "description": "Ollama model to use (optional; falls back to configured default).",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "list_sessions",
        "description": "List all available nyxGPT chat sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ------------------------------------------------------------------
# JSON-RPC helpers
# ------------------------------------------------------------------


def _response(req_id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response wrapping `result`."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response with the given `code` and `message`."""
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ------------------------------------------------------------------
# Request handlers
# ------------------------------------------------------------------


def _handle_initialize(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    """Handle the MCP `initialize` request, responding with protocol/server info."""
    return _response(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "nyxgpt", "version": "2.0.0"},
        },
    )


def _handle_tools_list(req_id: Any) -> dict[str, Any]:
    """Handle the MCP `tools/list` request, responding with the tool catalog."""
    return _response(req_id, {"tools": _TOOLS})


def _handle_tools_call(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Handle the MCP `tools/call` request by dispatching to the named tool.

    Returns an error response if `params["name"]` doesn't match a known tool.
    """
    tool_name = params.get("name", "")
    arguments: dict[str, Any] = params.get("arguments", {})

    if tool_name == "chat":
        return _tool_chat(req_id, arguments)
    if tool_name == "list_sessions":
        return _tool_list_sessions(req_id)

    return _error(req_id, -32602, f"Unknown tool: {tool_name!r}")


def _tool_chat(req_id: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Implement the `chat` MCP tool: send `arguments["prompt"]` to nyxGPT and return the reply.

    Args:
        req_id: JSON-RPC request id to echo back in the response.
        arguments: Tool arguments; must include `prompt`, may include
            `session` and `model`.

    Returns:
        A JSON-RPC response with the chat reply as text content, or an
        error response if `prompt` is missing/invalid or the chat call fails.
    """
    prompt = arguments.get("prompt")
    if not prompt or not isinstance(prompt, str):
        return _error(req_id, -32602, "'prompt' is required and must be a non-empty string")

    session: str = arguments.get("session") or "default"
    model: str | None = arguments.get("model") or None

    try:
        from nyxgpt.chat import chat

        result = chat(prompt, session=session, model=model)
        return _response(req_id, {"content": [{"type": "text", "text": result.reply}]})
    except Exception as exc:
        logger.exception("chat tool error")
        return _error(req_id, -32603, f"chat failed: {exc}")


def _tool_list_sessions(req_id: Any) -> dict[str, Any]:
    """Implement the `list_sessions` MCP tool: return the names of available chat sessions.

    Returns:
        A JSON-RPC response with a newline-separated list of session names
        (or a placeholder if there are none), or an error response on failure.
    """
    try:
        from nyxgpt.config import load_config
        from nyxgpt.sessions import list_sessions

        cfg = load_config(None)
        session_list = list_sessions(cfg)
        names = [s.get("name", "") for s in session_list if s.get("name")]
        text = "\n".join(names) if names else "(no sessions)"
        return _response(req_id, {"content": [{"type": "text", "text": text}]})
    except Exception as exc:
        logger.exception("list_sessions tool error")
        return _error(req_id, -32603, f"list_sessions failed: {exc}")


# ------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------


def _dispatch(request: dict[str, Any]) -> dict[str, Any] | None:
    """Route a JSON-RPC request to the appropriate handler.

    Returns ``None`` for notifications (no response required).
    """
    method: str = request.get("method", "")
    req_id: Any = request.get("id")
    params: dict[str, Any] = request.get("params") or {}

    if method == "initialize":
        return _handle_initialize(req_id, params)

    if method == "initialized":
        # Notification — no response
        return None

    if method == "tools/list":
        return _handle_tools_list(req_id)

    if method == "tools/call":
        return _handle_tools_call(req_id, params)

    # Unknown method — return error only if it's a request (has an id)
    if req_id is not None:
        return _error(req_id, -32601, f"Method not found: {method!r}")

    return None


# ------------------------------------------------------------------
# Server entry-point
# ------------------------------------------------------------------


def serve(
    stdin=None,
    stdout=None,
) -> None:
    """Run the MCP server, reading JSON-RPC requests from *stdin* and
    writing responses to *stdout* (defaults to ``sys.stdin``/``sys.stdout``).

    Each message must be a single JSON object on its own line.
    """
    _in = stdin or sys.stdin
    _out = stdout or sys.stdout

    logger.info("nyxGPT MCP server started (protocol %s)", PROTOCOL_VERSION)

    for raw_line in _in:
        line = raw_line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            # Send a parse-error response; we have no id to echo back.
            response = _error(None, -32700, f"Parse error: {exc}")
            print(json.dumps(response), file=_out, flush=True)
            continue

        dispatch_result: dict[str, Any] | None
        try:
            dispatch_result = _dispatch(request)
        except Exception as exc:
            logger.exception("Unhandled error dispatching %r", request.get("method"))
            dispatch_result = _error(request.get("id"), -32603, f"Internal error: {exc}")

        if dispatch_result is not None:
            print(json.dumps(dispatch_result), file=_out, flush=True)
