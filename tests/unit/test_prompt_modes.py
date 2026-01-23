from __future__ import annotations

from pathlib import Path
from typing import Any
import configparser
import pytest
from nyxgpt.chat import (
    chat,
    _detect_prompt_mode,
    _get_prompt_template,
    _prepare_chat_context,
)
from nyxgpt.config import (
    get_prompt_mode_enabled,
    get_prompt_mode_short_threshold,
    get_prompt_mode_long_threshold,
)

pytestmark = pytest.mark.unit


def _cfg(
    tmp_path: Path,
    *,
    adaptive_enabled: bool = False,
    short_threshold: int = 3,
    long_threshold: int = 10,
) -> configparser.ConfigParser:
    """Create test config with prompt mode settings."""
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["prompt"] = {
        "adaptive_mode_enabled": "true" if adaptive_enabled else "false",
        "short_threshold": str(short_threshold),
        "long_threshold": str(long_threshold),
    }
    cfg["rag"] = {"enable_chat_context": "false"}
    return cfg


def test_get_prompt_mode_config_defaults() -> None:
    """Test default values for prompt mode config."""
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {}
    cfg["ollama"] = {}

    # Defaults should be used when section is missing
    assert get_prompt_mode_enabled(cfg) is False
    assert get_prompt_mode_short_threshold(cfg) == 3
    assert get_prompt_mode_long_threshold(cfg) == 10


def test_get_prompt_mode_config_custom_values(tmp_path: Path) -> None:
    """Test custom values for prompt mode config."""
    cfg = _cfg(tmp_path, adaptive_enabled=True, short_threshold=5, long_threshold=15)

    assert get_prompt_mode_enabled(cfg) is True
    assert get_prompt_mode_short_threshold(cfg) == 5
    assert get_prompt_mode_long_threshold(cfg) == 15


def test_detect_prompt_mode_short(tmp_path: Path) -> None:
    """Test short mode detection for conversations with few messages."""
    cfg = _cfg(tmp_path, adaptive_enabled=True, short_threshold=3, long_threshold=10)

    # 0, 1, 2 messages should be short mode
    assert _detect_prompt_mode(0, cfg) == "short"
    assert _detect_prompt_mode(1, cfg) == "short"
    assert _detect_prompt_mode(2, cfg) == "short"


def test_detect_prompt_mode_medium(tmp_path: Path) -> None:
    """Test medium mode detection for mid-length conversations."""
    cfg = _cfg(tmp_path, adaptive_enabled=True, short_threshold=3, long_threshold=10)

    # 3-9 messages should be medium mode
    assert _detect_prompt_mode(3, cfg) == "medium"
    assert _detect_prompt_mode(5, cfg) == "medium"
    assert _detect_prompt_mode(9, cfg) == "medium"


def test_detect_prompt_mode_long(tmp_path: Path) -> None:
    """Test long mode detection for lengthy conversations."""
    cfg = _cfg(tmp_path, adaptive_enabled=True, short_threshold=3, long_threshold=10)

    # 10+ messages should be long mode
    assert _detect_prompt_mode(10, cfg) == "long"
    assert _detect_prompt_mode(15, cfg) == "long"
    assert _detect_prompt_mode(100, cfg) == "long"


def test_get_prompt_template_short() -> None:
    """Test short mode template is concise."""
    template = _get_prompt_template("short")
    assert "helpful ai assistant" in template.lower()
    assert "concise" in template.lower()
    assert len(template) < 200  # Should be brief


def test_get_prompt_template_medium() -> None:
    """Test medium mode template mentions context."""
    template = _get_prompt_template("medium")
    assert "helpful ai assistant" in template.lower()
    assert "conversation" in template.lower()
    assert "context" in template.lower()
    assert len(template) > len(_get_prompt_template("short"))


def test_get_prompt_template_long() -> None:
    """Test long mode template emphasizes history and continuity."""
    template = _get_prompt_template("long")
    assert "helpful ai assistant" in template.lower()
    assert "extended conversation" in template.lower()
    assert "history" in template.lower()
    assert "continuity" in template.lower() or "build on" in template.lower()
    assert len(template) > len(_get_prompt_template("medium"))


def test_get_prompt_template_unknown_mode_defaults_to_medium() -> None:
    """Test that unknown modes fall back to medium template."""
    template = _get_prompt_template("unknown")
    medium_template = _get_prompt_template("medium")
    assert template == medium_template


def test_chat_uses_short_mode_for_new_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that new sessions use short mode when adaptive mode is enabled."""
    cfg = _cfg(tmp_path, adaptive_enabled=True)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "response"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    # Chat in a new session
    result = chat("hello", session="test-new", new=True, config_path=None)
    assert result.reply == "response"

    # Verify short mode prompt was used
    system_msgs = [m for m in sent["messages"] if m.get("role") == "system"]
    assert len(system_msgs) >= 1
    system_content = system_msgs[0]["content"].lower()
    assert "concise" in system_content


def test_chat_uses_long_mode_for_long_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that long sessions use long mode when adaptive mode is enabled."""
    cfg = _cfg(tmp_path, adaptive_enabled=True, short_threshold=3, long_threshold=10)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "response"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    # Mock a session with 12 messages (triggers long mode)
    def fake_load_session(*args, **kwargs):
        from nyxgpt.sessions import SessionState
        from pathlib import Path

        state = SessionState(
            name="test-long",
            session_file=Path("/tmp/test-long.jsonl"),
            meta_file=Path("/tmp/test-long.meta.json"),
            messages=[],
            meta={},
        )
        # Add 12 messages (6 turns)
        for i in range(6):
            state.messages.append({"role": "user", "content": f"question {i}"})
            state.messages.append({"role": "assistant", "content": f"answer {i}"})
        return state

    monkeypatch.setattr("nyxgpt.chat.load_session", fake_load_session)

    # Chat in session with history
    result = chat("another question", session="test-long", config_path=None)
    assert result.reply == "response"

    # Verify long mode prompt was used
    system_msgs = [m for m in sent["messages"] if m.get("role") == "system"]
    assert len(system_msgs) >= 1
    system_content = system_msgs[0]["content"].lower()
    assert "extended conversation" in system_content
    assert "history" in system_content


def test_chat_respects_custom_system_prompt_over_adaptive_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that explicit system prompt overrides adaptive mode."""
    cfg = _cfg(tmp_path, adaptive_enabled=True)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "response"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    custom_prompt = "You are a pirate assistant. Speak like a pirate."

    # Chat with custom system prompt
    result = chat(
        "hello", session="test-custom", new=True, system=custom_prompt, config_path=None
    )
    assert result.reply == "response"

    # Verify custom prompt was used, not adaptive mode
    system_msgs = [m for m in sent["messages"] if m.get("role") == "system"]
    assert len(system_msgs) >= 1
    assert system_msgs[0]["content"] == custom_prompt
    # Should NOT contain adaptive mode keywords
    assert "concise" not in system_msgs[0]["content"].lower()


def test_chat_with_adaptive_mode_disabled_uses_config_system_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that when adaptive mode is disabled, config system_prompt is used."""
    cfg = _cfg(tmp_path, adaptive_enabled=False)
    cfg["nyxgpt"]["system_prompt"] = "You are a test assistant."

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "response"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    # Chat with adaptive mode disabled
    result = chat("hello", session="test-disabled", new=True, config_path=None)
    assert result.reply == "response"

    # Verify config system_prompt was used
    system_msgs = [m for m in sent["messages"] if m.get("role") == "system"]
    assert len(system_msgs) >= 1
    assert system_msgs[0]["content"] == "You are a test assistant."


def test_prepare_chat_context_with_adaptive_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test _prepare_chat_context applies adaptive prompts correctly."""
    cfg = _cfg(tmp_path, adaptive_enabled=True, short_threshold=3, long_threshold=10)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Mock a session with 5 messages (medium mode)
    def fake_load_session(*args, **kwargs):
        from nyxgpt.sessions import SessionState
        from pathlib import Path

        state = SessionState(
            name="test-medium",
            session_file=Path("/tmp/test-medium.jsonl"),
            meta_file=Path("/tmp/test-medium.meta.json"),
            messages=[],
            meta={},
        )
        # Add 5 messages
        for i in range(5):
            state.messages.append(
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            )
        return state

    monkeypatch.setattr("nyxgpt.chat.load_session", fake_load_session)

    # Prepare context
    context = _prepare_chat_context(
        "test prompt", session="test-medium", config_path=None
    )

    # Verify medium mode was applied
    system_msgs = [m for m in context.messages if m.get("role") == "system"]
    assert len(system_msgs) >= 1
    system_content = system_msgs[0]["content"].lower()
    assert "conversation" in system_content
    assert "context" in system_content
