"""Unit tests for the nyxGPT MCP server."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from nyxgpt.mcp_server import (
    PROTOCOL_VERSION,
    _dispatch,
    _error,
    _response,
    _tool_chat,
    _tool_list_sessions,
    serve,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------
# JSON-RPC helpers
# ------------------------------------------------------------------


def test_response_structure():
    result = _response(1, {"key": "value"})
    assert result == {"jsonrpc": "2.0", "id": 1, "result": {"key": "value"}}


def test_error_structure():
    result = _error(2, -32601, "Method not found")
    assert result["jsonrpc"] == "2.0"
    assert result["id"] == 2
    assert result["error"]["code"] == -32601
    assert "Method not found" in result["error"]["message"]


# ------------------------------------------------------------------
# Dispatch: initialize
# ------------------------------------------------------------------


def test_dispatch_initialize():
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    result = _dispatch(request)
    assert result is not None
    assert result["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert result["result"]["serverInfo"]["name"] == "nyxgpt"
    assert "tools" in result["result"]["capabilities"]


# ------------------------------------------------------------------
# Dispatch: initialized notification
# ------------------------------------------------------------------


def test_dispatch_initialized_returns_none():
    request = {"jsonrpc": "2.0", "method": "initialized"}
    result = _dispatch(request)
    assert result is None


# ------------------------------------------------------------------
# Dispatch: tools/list
# ------------------------------------------------------------------


def test_dispatch_tools_list():
    request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    result = _dispatch(request)
    assert result is not None
    tools = result["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "chat" in tool_names
    assert "list_sessions" in tool_names


def test_tools_have_input_schema():
    request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
    result = _dispatch(request)
    assert result is not None
    for tool in result["result"]["tools"]:
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


# ------------------------------------------------------------------
# Dispatch: unknown method
# ------------------------------------------------------------------


def test_dispatch_unknown_method_with_id_returns_error():
    request = {"jsonrpc": "2.0", "id": 9, "method": "unknown/method"}
    result = _dispatch(request)
    assert result is not None
    assert "error" in result
    assert result["error"]["code"] == -32601


def test_dispatch_unknown_notification_returns_none():
    # Notifications have no id
    request = {"jsonrpc": "2.0", "method": "unknown/notification"}
    result = _dispatch(request)
    assert result is None


# ------------------------------------------------------------------
# tools/call: chat tool
# ------------------------------------------------------------------


def test_tool_chat_missing_prompt_returns_error():
    result = _tool_chat(1, {})
    assert "error" in result
    assert result["error"]["code"] == -32602


def test_tool_chat_empty_prompt_returns_error():
    result = _tool_chat(1, {"prompt": ""})
    assert "error" in result


@patch("nyxgpt.mcp_server._tool_chat")
def test_dispatch_tools_call_chat(mock_tool_chat):
    mock_tool_chat.return_value = {"jsonrpc": "2.0", "id": 5, "result": {"content": []}}
    request = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "chat", "arguments": {"prompt": "hello"}},
    }
    _dispatch(request)
    mock_tool_chat.assert_called_once_with(5, {"prompt": "hello"})


@patch("nyxgpt.chat.chat")
def test_tool_chat_success(mock_chat):
    mock_result = MagicMock()
    mock_result.reply = "Hello from nyxGPT!"
    mock_chat.return_value = mock_result

    result = _tool_chat(1, {"prompt": "hello", "session": "test"})
    assert "result" in result
    content = result["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "Hello from nyxGPT!"
    mock_chat.assert_called_once_with("hello", session="test", model=None)


@patch("nyxgpt.chat.chat")
def test_tool_chat_with_model(mock_chat):
    mock_result = MagicMock()
    mock_result.reply = "Response"
    mock_chat.return_value = mock_result

    _tool_chat(1, {"prompt": "hi", "model": "llama3.1:8b"})
    mock_chat.assert_called_once_with("hi", session="default", model="llama3.1:8b")


@patch("nyxgpt.chat.chat")
def test_tool_chat_error_returns_rpc_error(mock_chat):
    mock_chat.side_effect = RuntimeError("Ollama unavailable")
    result = _tool_chat(1, {"prompt": "hello"})
    assert "error" in result
    assert result["error"]["code"] == -32603
    assert "Ollama unavailable" in result["error"]["message"]


# ------------------------------------------------------------------
# tools/call: list_sessions tool
# ------------------------------------------------------------------


@patch("nyxgpt.sessions.list_sessions")
@patch("nyxgpt.config.load_config")
def test_tool_list_sessions_success(mock_load_config, mock_list_sessions):
    mock_load_config.return_value = MagicMock()
    mock_list_sessions.return_value = [{"name": "alpha"}, {"name": "beta"}]

    result = _tool_list_sessions(2)
    assert "result" in result
    text = result["result"]["content"][0]["text"]
    assert "alpha" in text
    assert "beta" in text


@patch("nyxgpt.sessions.list_sessions")
@patch("nyxgpt.config.load_config")
def test_tool_list_sessions_empty(mock_load_config, mock_list_sessions):
    mock_load_config.return_value = MagicMock()
    mock_list_sessions.return_value = []

    result = _tool_list_sessions(3)
    assert "result" in result
    text = result["result"]["content"][0]["text"]
    assert "no sessions" in text.lower()


@patch("nyxgpt.sessions.list_sessions")
@patch("nyxgpt.config.load_config")
def test_tool_list_sessions_error(mock_load_config, mock_list_sessions):
    mock_load_config.return_value = MagicMock()
    mock_list_sessions.side_effect = RuntimeError("DB error")

    result = _tool_list_sessions(4)
    assert "error" in result
    assert result["error"]["code"] == -32603


# ------------------------------------------------------------------
# tools/call: unknown tool
# ------------------------------------------------------------------


def test_dispatch_tools_call_unknown_tool():
    request = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {"name": "nonexistent_tool", "arguments": {}},
    }
    result = _dispatch(request)
    assert result is not None
    assert "error" in result
    assert result["error"]["code"] == -32602


# ------------------------------------------------------------------
# serve(): I/O integration
# ------------------------------------------------------------------


def _run_serve(messages: list[dict]) -> list[dict]:
    """Helper: feed *messages* through the serve() loop, collect responses."""
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout)
    stdout.seek(0)
    responses = []
    for line in stdout:
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


def test_serve_initialize():
    responses = _run_serve([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    assert len(responses) == 1
    assert responses[0]["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_serve_ignores_blank_lines():
    stdin = io.StringIO(
        "\n\n"
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n\n"
    )
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout)
    stdout.seek(0)
    lines = [ln.strip() for ln in stdout if ln.strip()]
    assert len(lines) == 1


def test_serve_parse_error_sends_error_response():
    stdin = io.StringIO("not valid json\n")
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout)
    stdout.seek(0)
    lines = [ln.strip() for ln in stdout if ln.strip()]
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert "error" in response
    assert response["error"]["code"] == -32700


def test_serve_initialized_notification_no_response():
    responses = _run_serve([{"jsonrpc": "2.0", "method": "initialized"}])
    assert responses == []
