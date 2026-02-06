from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

import pytest

from nyxgpt.chat import _minimize_system_prompt, chat

pytestmark = pytest.mark.unit


def test_minimize_whitespace():
    """Test that extra whitespace is normalized."""
    prompt = "You are   a  helpful   assistant.\n\n\nAnswer   carefully."
    result = _minimize_system_prompt(prompt)
    assert result == "a helpful assistant. Answer carefully."


def test_minimize_please():
    """Test removal of 'Please' prefix."""
    prompt = "Please respond to the user's questions."
    result = _minimize_system_prompt(prompt)
    assert result == "respond to the user's questions."


def test_minimize_you_are():
    """Test removal of 'You are' prefix."""
    prompt = "You are a helpful AI assistant."
    result = _minimize_system_prompt(prompt)
    assert result == "a helpful AI assistant."


def test_minimize_you_should():
    """Test removal of 'You should' prefix."""
    prompt = "You should always be polite."
    result = _minimize_system_prompt(prompt)
    assert result == "always be polite."


def test_minimize_i_want_you_to():
    """Test removal of 'I want you to' prefix."""
    prompt = "I want you to act as a coding assistant."
    result = _minimize_system_prompt(prompt)
    assert result == "act as a coding assistant."


def test_minimize_your_task_is_to():
    """Test removal of 'Your task is to' prefix."""
    prompt = "Your task is to help users debug their code."
    result = _minimize_system_prompt(prompt)
    assert result == "help users debug their code."


def test_minimize_make_sure_to():
    """Test removal of 'Make sure to' prefix."""
    prompt = "Make sure to verify all answers."
    result = _minimize_system_prompt(prompt)
    assert result == "verify all answers."


def test_minimize_be_sure_to():
    """Test removal of 'Be sure to' prefix."""
    prompt = "Be sure to check for errors."
    result = _minimize_system_prompt(prompt)
    assert result == "check for errors."


def test_minimize_in_order_to():
    """Test condensing 'in order to' to 'to'."""
    prompt = "Use the context in order to answer questions."
    result = _minimize_system_prompt(prompt)
    assert result == "Use the context to answer questions."


def test_minimize_as_well_as():
    """Test condensing 'as well as' to 'and'."""
    prompt = "Be accurate as well as helpful."
    result = _minimize_system_prompt(prompt)
    assert result == "Be accurate and helpful."


def test_minimize_complex_prompt():
    """Test minimization on a realistic complex prompt."""
    prompt = """
    You are a helpful AI assistant.

    Please respond to user queries carefully.

    Make sure to be accurate as well as concise.
    You should use proper formatting in order to improve readability.
    """
    result = _minimize_system_prompt(prompt)

    # Should remove redundant patterns and collapse whitespace
    assert "You are" not in result
    assert "Please" not in result
    assert "Make sure to" not in result
    assert "You should" not in result
    assert "in order to" not in result
    assert "as well as" not in result
    assert "\n" not in result

    # Should preserve core meaning
    assert "AI assistant" in result
    assert "respond" in result
    assert "accurate" in result
    assert "concise" in result


def test_minimize_empty_prompt():
    """Test that empty prompts are handled gracefully."""
    assert _minimize_system_prompt("") == ""
    assert _minimize_system_prompt("   ") == "   "


def test_minimize_preserves_case_in_content():
    """Test that case is preserved in non-pattern content."""
    prompt = "Please explain Python and JavaScript."
    result = _minimize_system_prompt(prompt)
    # "Please" should be removed, but case of Python/JavaScript preserved
    assert "Python" in result
    assert "JavaScript" in result


def test_chat_with_minimize_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that chat works normally when minimization is disabled."""
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
        "system_prompt": "You are a helpful assistant.",
        "system_prompt_minimize": "false",  # Disabled
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["rag"] = {"enable_chat_context": "false"}

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "response"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("test", config_path=None, system="You are a helpful assistant.")
    assert result.reply == "response"

    # System message should NOT be minimized
    system_msg = sent["messages"][0]
    assert system_msg["role"] == "system"
    assert system_msg["content"] == "You are a helpful assistant."


def test_chat_with_minimize_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that chat applies minimization when enabled."""
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
        "system_prompt": "You are a helpful assistant.",
        "system_prompt_minimize": "true",  # Enabled
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["rag"] = {"enable_chat_context": "false"}

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "response"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("test", config_path=None, system="You are a helpful assistant.")
    assert result.reply == "response"

    # System message SHOULD be minimized
    system_msg = sent["messages"][0]
    assert system_msg["role"] == "system"
    # "You are" should be removed
    assert "You are" not in system_msg["content"]
    assert "a helpful assistant" in system_msg["content"]


def test_minimize_multiple_patterns():
    """Test that multiple patterns can be applied to the same prompt."""
    prompt = "Please make sure to respond carefully in order to help the user."
    result = _minimize_system_prompt(prompt)

    # Multiple patterns should be removed
    assert "Please" not in result
    assert "make sure to" not in result
    assert "in order to" not in result

    # Core content should remain
    assert "respond" in result
    assert "carefully" in result
    assert "help" in result
    assert "user" in result
