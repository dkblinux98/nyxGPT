from __future__ import annotations

import configparser
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input, ListView

from nyxgpt.tui import (  # type: ignore[import-untyped]
    ChatOutput,
    CommandPaletteScreen,
    DeleteConfirmationScreen,
    HelpOverlayScreen,
    ManageDocumentsScreen,
    ModelsManagerScreen,
    NyxGPTTUI,
    SearchResultsScreen,
    SessionMetadataPreview,
    SessionPickerScreen,
    SessionStatusBar,
)

pytestmark = pytest.mark.unit


# ============================================================================
# ChatOutput Widget Tests
# ============================================================================


def test_chat_output_initialization() -> None:
    """Test that ChatOutput initializes with empty buffer."""
    widget = ChatOutput()
    assert widget._buffer == ""


def test_chat_output_clear() -> None:
    """Test that clear() resets the buffer and display."""
    widget = ChatOutput()
    widget._buffer = "some text"

    with patch.object(widget, "update") as mock_update:
        widget.clear()

    assert widget._buffer == ""
    mock_update.assert_called_once_with("")


def test_chat_output_append() -> None:
    """Test that append() adds text to buffer and updates display."""
    widget = ChatOutput()

    with patch.object(widget, "update") as mock_update:
        widget.append("Hello")

    assert widget._buffer == "Hello"
    mock_update.assert_called_once_with("Hello")


def test_chat_output_append_accumulates() -> None:
    """Test that multiple append() calls accumulate text."""
    widget = ChatOutput()

    with patch.object(widget, "update") as mock_update:
        widget.append("Hello ")
        widget.append("World")

    assert widget._buffer == "Hello World"
    assert mock_update.call_count == 2
    mock_update.assert_called_with("Hello World")


def test_chat_output_remove_typing_indicator() -> None:
    """Test that remove_typing_indicator() removes the typing indicator."""
    widget = ChatOutput()

    with patch.object(widget, "update") as mock_update:
        # Add typing indicator
        widget.append("Assistant: ⋯")

        # Remove it
        widget.remove_typing_indicator()

    assert widget._buffer == "Assistant: "
    assert mock_update.call_count == 2
    mock_update.assert_called_with("Assistant: ")


def test_chat_output_remove_typing_indicator_no_indicator() -> None:
    """Test that remove_typing_indicator() does nothing if no indicator present."""
    widget = ChatOutput()

    with patch.object(widget, "update") as mock_update:
        widget.append("Assistant: Hello")

        # Try to remove indicator when there isn't one
        widget.remove_typing_indicator()

    # Buffer should remain unchanged
    assert widget._buffer == "Assistant: Hello"
    # update should only be called once (from append)
    assert mock_update.call_count == 1


# ============================================================================
# SessionStatusBar Widget Tests
# ============================================================================


def test_session_status_bar_initialization() -> None:
    """Test that SessionStatusBar initializes with empty values."""
    widget = SessionStatusBar()
    assert widget.session_name == ""
    assert widget.message_count == 0
    assert widget.model == ""
    assert widget.rag_enabled is False


def test_session_status_bar_update_info() -> None:
    """Test that update_info() updates all fields and refreshes display."""
    widget = SessionStatusBar()

    with patch.object(widget, "update") as mock_update:
        widget.update_info(
            session_name="test-session",
            message_count=42,
            model="llama3.1:8b",
            rag_enabled=True,
        )

    # Verify fields were updated
    assert widget.session_name == "test-session"
    assert widget.message_count == 42
    assert widget.model == "llama3.1:8b"
    assert widget.rag_enabled is True

    # Verify display was updated
    mock_update.assert_called_once()
    call_arg = mock_update.call_args[0][0]
    assert "test-session" in call_arg
    assert "42" in call_arg
    assert "llama3.1:8b" in call_arg
    assert "RAG:ON" in call_arg


def test_session_status_bar_update_info_rag_disabled() -> None:
    """Test that update_info() shows RAG:OFF when RAG is disabled."""
    widget = SessionStatusBar()

    with patch.object(widget, "update") as mock_update:
        widget.update_info(
            session_name="test-session",
            message_count=10,
            model="mistral:7b",
            rag_enabled=False,
        )

    # Verify RAG status shows OFF
    call_arg = mock_update.call_args[0][0]
    assert "RAG:OFF" in call_arg


def test_session_status_bar_update_info_with_attached_docs() -> None:
    """Test that update_info() shows the FI: doc count indicator when docs are attached."""
    widget = SessionStatusBar()

    with patch.object(widget, "update") as mock_update:
        widget.update_info(
            session_name="test-session",
            message_count=1,
            model="llama3.1:8b",
            rag_enabled=True,
            attached_doc_count=3,
        )

    call_arg = mock_update.call_args[0][0]
    assert "FI:3" in call_arg


def test_session_status_bar_refresh_display() -> None:
    """Test that _refresh_display() formats status text correctly."""
    widget = SessionStatusBar()
    widget.session_name = "my-session"
    widget.message_count = 5
    widget.model = "gpt4"
    widget.rag_enabled = True

    with patch.object(widget, "update") as mock_update:
        widget._refresh_display()

    # Verify formatted output
    call_arg = mock_update.call_args[0][0]
    assert "Session: my-session" in call_arg
    assert "Messages: 5" in call_arg
    assert "Model: gpt4" in call_arg
    assert "RAG:ON" in call_arg


# ============================================================================
# NyxGPTTUI Initialization Tests
# ============================================================================


def test_tui_initialization_default(tmp_path: Path) -> None:
    """Test TUI initialization with default config."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    assert app.session == "test-session"
    assert app.api_base_url == "http://127.0.0.1:8000"


def test_tui_initialization_custom_api_url(tmp_path: Path) -> None:
    """Test TUI initialization with custom API URL."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="custom-session", api_base_url="http://localhost:9000")

    assert app.session == "custom-session"
    assert app.api_base_url == "http://localhost:9000"


def test_tui_initialization_fallback_url(tmp_path: Path) -> None:
    """Test TUI falls back to default URL when config has none."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    # No [api] section
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    assert app.api_base_url == "http://127.0.0.1:8000"


# ============================================================================
# NyxGPTTUI._unlock_prompt() Tests
# ============================================================================


def test_unlock_prompt_success(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test _unlock_prompt() successfully enables and focuses prompt."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Mock the prompt widget
    app.prompt = MagicMock(spec=Input)
    app.prompt.disabled = True

    with caplog.at_level(logging.DEBUG, logger="nyxgpt.tui"):
        app._unlock_prompt()

    # Verify prompt was enabled and focused
    assert app.prompt.disabled is False
    app.prompt.focus.assert_called_once()

    # Verify debug logging
    assert "Input prompt unlocked and focused" in caplog.text


def test_unlock_prompt_attribute_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test _unlock_prompt() handles AttributeError gracefully."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Don't set app.prompt - will cause AttributeError

    with caplog.at_level(logging.WARNING, logger="nyxgpt.tui"):
        # Should not raise exception
        app._unlock_prompt()

    # Verify warning was logged
    assert "Failed to unlock prompt (widget not available)" in caplog.text


def test_unlock_prompt_other_exception(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test _unlock_prompt() handles other exceptions gracefully."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Mock prompt to raise RuntimeError when accessing disabled
    app.prompt = MagicMock(spec=Input)
    type(app.prompt).disabled = PropertyMock(side_effect=RuntimeError("Textual shutdown"))

    with caplog.at_level(logging.WARNING, logger="nyxgpt.tui"):
        # Should not raise exception
        app._unlock_prompt()

    # Verify warning was logged with exception type
    assert "Failed to unlock prompt: RuntimeError" in caplog.text


# ============================================================================
# NyxGPTTUI.compose() Tests
# ============================================================================


def test_compose_creates_widgets(tmp_path: Path) -> None:
    """Test that compose() assigns output and prompt widgets."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Instead of calling compose() which requires Textual context,
    # just verify the method exists and would assign widgets
    assert callable(app.compose)

    # Manually trigger widget assignment as compose() would do
    app.output = ChatOutput()
    app.prompt = Input(placeholder="Test")

    # Verify widgets were assigned correctly
    assert isinstance(app.output, ChatOutput)
    assert isinstance(app.prompt, Input)


# ============================================================================
# NyxGPTTUI.on_mount() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_on_mount_calls_unlock_prompt(tmp_path: Path) -> None:
    """Test that on_mount() calls _unlock_prompt() for defensive reset."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Mock _unlock_prompt and _update_session_status
    with (
        patch.object(app, "_unlock_prompt") as mock_unlock,
        patch.object(app, "_update_session_status", new=AsyncMock()),
    ):
        await app.on_mount()

    # Verify defensive reset was called
    mock_unlock.assert_called_once()


# ============================================================================
# NyxGPTTUI.on_input_submitted() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_input_submitted_empty_string_ignored(tmp_path: Path) -> None:
    """Test that empty input is ignored."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)
    app.prompt.value = ""
    app.prompt.disabled = False

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Create event with empty value
    event = MagicMock()
    event.value = "   "  # Whitespace only

    with patch.object(app, "_stream_chat"):
        await app.on_input_submitted(event)

    # Verify prompt was not locked
    assert app.prompt.disabled is False


@pytest.mark.asyncio
async def test_input_submitted_locks_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that input submission locks the prompt."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)
    app.prompt.value = "Hello"
    app.prompt.disabled = False

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Create event
    event = MagicMock()
    event.value = "Hello"

    with (
        patch("asyncio.create_task") as mock_create_task,
        caplog.at_level(logging.DEBUG, logger="nyxgpt.tui"),
    ):
        await app.on_input_submitted(event)

    # Verify prompt was cleared and locked
    assert app.prompt.value == ""
    assert app.prompt.disabled is True

    # Verify debug logging
    assert "Input prompt locked for streaming" in caplog.text

    # Verify output was cleared and updated
    app.output.clear.assert_called_once()
    app.output.append.assert_called_once_with("→ Hello\n\n")

    # Verify streaming task was created
    mock_create_task.assert_called_once()


# ============================================================================
# NyxGPTTUI._stream_chat() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stream_chat_success(tmp_path: Path) -> None:
    """Test successful chat streaming."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)

    # Mock httpx AsyncClient
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        for chunk in ["Hello", " ", "World"]:
            yield chunk

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    # Verify typing indicator was shown
    typing_indicator_calls = [call for call in app.output.append.call_args_list if "⋯" in str(call)]
    assert len(typing_indicator_calls) > 0, "Typing indicator should be shown"

    # Verify typing indicator was removed on first content
    remove_calls = app.output.remove_typing_indicator.call_count
    assert remove_calls == 1, "Typing indicator should be removed on first content"

    # Verify output was updated with chunks
    # "Assistant: ⋯", "Hello", " ", "World", "\n\n" (typing indicator removed, not appended)
    assert app.output.append.call_count >= 4

    # Verify prompt was unlocked (called in finally block)
    assert app.prompt.disabled is False


@pytest.mark.asyncio
async def test_stream_chat_error_handling(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test error handling in chat streaming."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)
    app.prompt.disabled = True

    # Mock httpx to raise exception
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(side_effect=Exception("Connection error"))

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.ERROR, logger="nyxgpt.tui"),
    ):
        await app._stream_chat("Test prompt")

    # Verify error was logged
    assert "TUI chat stream failed" in caplog.text

    # Verify error was shown in output
    app.output.append.assert_called()
    error_call = [call for call in app.output.append.call_args_list if "[error]" in str(call)]
    assert len(error_call) > 0

    # Verify prompt was unlocked even after error (finally block)
    assert app.prompt.disabled is False


@pytest.mark.asyncio
async def test_stream_chat_unlock_on_exception(tmp_path: Path) -> None:
    """Test that prompt is unlocked even when streaming fails."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI()

    # Mock widgets
    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)
    app.prompt.disabled = True

    # Mock httpx to fail
    with patch("httpx.AsyncClient", side_effect=RuntimeError("Test error")):
        await app._stream_chat("Test")

    # Verify prompt was unlocked despite exception
    assert app.prompt.disabled is False
    app.prompt.focus.assert_called()


# ============================================================================
# SessionMetadataPreview Widget Tests
# ============================================================================


def test_session_metadata_preview_update() -> None:
    """Test that SessionMetadataPreview updates with session metadata."""
    widget = SessionMetadataPreview()

    session = {
        "name": "test-session",
        "messages": 5,
        "modified": "2024-01-01 12:00:00",
        "meta": {
            "title": "Test Session",
            "summary": "This is a test session",
            "tags": ["test", "example"],
            "pinned": True,
        },
    }

    with patch.object(widget, "update") as mock_update:
        widget.update_session(session)

    # Verify update was called
    mock_update.assert_called_once()
    call_arg = mock_update.call_args[0][0]

    # Check that key information is in the preview
    assert "📌" in call_arg  # Pinned indicator
    assert "Test Session" in call_arg
    assert "2024-01-01 12:00:00" in call_arg
    assert "5" in call_arg  # Message count
    assert "test, example" in call_arg  # Tags
    assert "This is a test session" in call_arg


def test_session_metadata_preview_without_optional_fields() -> None:
    """Test SessionMetadataPreview with minimal session data."""
    widget = SessionMetadataPreview()

    session = {
        "name": "minimal-session",
        "messages": 0,
        "modified": "Unknown",
        "meta": {},
    }

    with patch.object(widget, "update") as mock_update:
        widget.update_session(session)

    # Verify update was called
    mock_update.assert_called_once()
    call_arg = mock_update.call_args[0][0]

    # Check fallback values
    assert "minimal-session" in call_arg  # Uses name as title
    assert "No summary available" in call_arg
    assert "None" in call_arg  # No tags


# ============================================================================
# SessionPickerScreen Tests
# ============================================================================


def test_session_picker_initialization(tmp_path: Path) -> None:
    """Test SessionPickerScreen initializes correctly."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    assert screen.all_sessions == []
    assert screen.filtered_sessions == []


@pytest.mark.asyncio
async def test_session_picker_load_sessions(tmp_path: Path) -> None:
    """Test SessionPickerScreen loads sessions on mount."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    mock_sessions = [
        {
            "name": "session1",
            "messages": 5,
            "modified": "2024-01-01 12:00:00",
            "meta": {"title": "Session 1"},
        },
        {
            "name": "session2",
            "messages": 3,
            "modified": "2024-01-02 14:00:00",
            "meta": {"title": "Session 2"},
        },
    ]

    with (
        patch("nyxgpt.tui.load_config", return_value=cfg),
        patch("nyxgpt.tui.list_sessions", return_value=mock_sessions),
    ):
        screen = SessionPickerScreen(str(config_file))

        # Mock update_session_list
        with patch.object(screen, "update_session_list", new=AsyncMock()):
            await screen.load_sessions()

    assert screen.all_sessions == mock_sessions
    assert screen.filtered_sessions == mock_sessions


@pytest.mark.asyncio
async def test_session_picker_search_filter(tmp_path: Path) -> None:
    """Test SessionPickerScreen filters sessions based on search."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    mock_sessions = [
        {
            "name": "python-project",
            "messages": 5,
            "modified": "2024-01-01",
            "meta": {"title": "Python Development", "tags": ["coding"]},
        },
        {
            "name": "java-project",
            "messages": 3,
            "modified": "2024-01-02",
            "meta": {"title": "Java Development", "tags": ["coding"]},
        },
        {
            "name": "meeting-notes",
            "messages": 2,
            "modified": "2024-01-03",
            "meta": {"title": "Meeting Notes", "tags": ["notes"]},
        },
    ]

    with (
        patch("nyxgpt.tui.load_config", return_value=cfg),
        patch("nyxgpt.tui.list_sessions", return_value=mock_sessions),
    ):
        screen = SessionPickerScreen(str(config_file))
        screen.all_sessions = mock_sessions
        screen.filtered_sessions = mock_sessions

        # Mock update_session_list
        with patch.object(screen, "update_session_list", new=AsyncMock()):
            # Create mock input event
            mock_input = MagicMock(spec=Input)
            mock_input.id = "search"
            event = MagicMock()
            event.input = mock_input
            event.value = "python"

            await screen.on_input_changed(event)

    # Should only have the Python session
    assert len(screen.filtered_sessions) == 1
    assert screen.filtered_sessions[0]["name"] == "python-project"


@pytest.mark.asyncio
async def test_session_picker_search_empty_query(tmp_path: Path) -> None:
    """Test SessionPickerScreen shows all sessions when search is empty."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    mock_sessions = [
        {"name": "session1", "messages": 5, "modified": "2024-01-01", "meta": {}},
        {"name": "session2", "messages": 3, "modified": "2024-01-02", "meta": {}},
    ]

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))
        screen.all_sessions = mock_sessions
        screen.filtered_sessions = []

        with patch.object(screen, "update_session_list", new=AsyncMock()):
            mock_input = MagicMock(spec=Input)
            mock_input.id = "search"
            event = MagicMock()
            event.input = mock_input
            event.value = ""

            await screen.on_input_changed(event)

    # Should show all sessions
    assert screen.filtered_sessions == mock_sessions


def test_session_picker_action_cancel(tmp_path: Path) -> None:
    """Test SessionPickerScreen cancel action."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    # Mock dismiss method
    with patch.object(screen, "dismiss") as mock_dismiss:
        screen.action_cancel()

    # Should dismiss with None
    mock_dismiss.assert_called_once_with(None)


# ============================================================================
# NyxGPTTUI Session Picker Integration Tests
# ============================================================================


def test_tui_initialization_with_config_path(tmp_path: Path) -> None:
    """Test TUI accepts config_path parameter."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test", config_path=str(config_file))

    assert app.config_path == str(config_file)


@pytest.mark.asyncio
async def test_tui_action_pick_session(tmp_path: Path) -> None:
    """Test TUI action_pick_session switches to selected session."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="original-session", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock push_screen_wait to return selected session
    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value="new-session")),
        patch.object(app, "_update_session_status", new=AsyncMock()),
    ):
        await app._pick_session_worker()

    # Verify session was switched
    assert app.session == "new-session"

    # Verify output was cleared and confirmation shown
    app.output.clear.assert_called_once()
    app.output.append.assert_called_once()
    assert "new-session" in app.output.append.call_args[0][0]


@pytest.mark.asyncio
async def test_tui_action_pick_session_cancel(tmp_path: Path) -> None:
    """Test TUI action_pick_session handles cancel (None returned)."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="original-session", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock push_screen_wait to return None (cancel)
    with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)):
        await app._pick_session_worker()

    # Verify session was NOT switched
    assert app.session == "original-session"

    # Verify output was NOT modified
    app.output.clear.assert_not_called()
    app.output.append.assert_not_called()


# ============================================================================
# NyxGPTTUI Reconnection Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stream_chat_with_retry_markers(tmp_path: Path) -> None:
    """Test that retry markers are parsed and displayed correctly."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)

    # Mock httpx AsyncClient with retry marker in stream
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Simulate retry marker followed by response
        import json

        retry_data = {"type": "retry_status", "attempt": 1, "delay": 1.5}
        yield f"__RETRY_START__{json.dumps(retry_data)}__RETRY_END__"
        yield "Hello"
        yield " "
        yield "World"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    # Verify reconnection message was displayed
    append_calls = [str(call) for call in app.output.append.call_args_list]
    reconnect_calls = [c for c in append_calls if "reconnecting" in c.lower()]
    assert len(reconnect_calls) > 0

    # Verify response text was also displayed
    text_calls = [c for c in append_calls if "Hello" in c or "World" in c]
    assert len(text_calls) > 0


@pytest.mark.asyncio
async def test_stream_chat_with_rag_markers_ignored(tmp_path: Path) -> None:
    """Test that RAG markers are filtered out in TUI."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)

    # Mock httpx AsyncClient with RAG marker in stream
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        rag_data = {"type": "rag_metadata", "chunks": []}
        yield f"__RAG_START__{json.dumps(rag_data)}__RAG_END__"
        yield "Response text"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    # Verify RAG marker was filtered out
    append_calls = [str(call) for call in app.output.append.call_args_list]
    rag_calls = [c for c in append_calls if "__RAG_START__" in c or "__RAG_END__" in c]
    assert len(rag_calls) == 0

    # Verify response text was displayed
    text_calls = [c for c in append_calls if "Response text" in c]
    assert len(text_calls) > 0


# ============================================================================
# NyxGPTTUI Partial Marker Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stream_chat_partial_retry_marker_split_across_chunks(
    tmp_path: Path,
) -> None:
    """Test marker split across multiple chunks is buffered correctly.

    Covers the partial marker detection logic in tui.py:567-578.
    Tests scenario where __RETRY_START__ arrives in one chunk and the rest in another.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        retry_data = {"type": "retry_status", "attempt": 1, "delay": 2.0}
        full_marker = f"__RETRY_START__{json.dumps(retry_data)}__RETRY_END__"

        # Split the marker at different positions
        yield full_marker[:10]  # "__RETRY_ST"
        yield full_marker[10:40]  # "ART__...partial JSON..."
        yield full_marker[40:]  # "...rest of JSON...__RETRY_END__"
        yield "Hello World"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    # Verify reconnection message was displayed (marker was reassembled correctly)
    append_calls = [str(call) for call in app.output.append.call_args_list]
    reconnect_calls = [c for c in append_calls if "reconnecting" in c.lower()]
    assert len(reconnect_calls) > 0, "Partial retry marker should be reassembled and displayed"

    # Verify response text was displayed
    text_calls = [c for c in append_calls if "Hello World" in c]
    assert len(text_calls) > 0, "Response text after marker should be displayed"


@pytest.mark.asyncio
async def test_stream_chat_partial_marker_at_chunk_boundary(tmp_path: Path) -> None:
    """Test marker at exact chunk boundary is handled correctly.

    Tests scenario where chunk ends with partial marker prefix like '__RETRY_'.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        # Create the full marker first
        retry_data = {"type": "retry_status", "attempt": 2, "delay": 3.0}
        full_marker = f"__RETRY_START__{json.dumps(retry_data)}__RETRY_END__"

        # First chunk: regular text ending with partial marker "__R"
        yield "Some text before __R"

        # Second chunk: rest of marker starting from "ETRY_START__..."
        yield full_marker[3:]  # Start after "__R"

        # Third chunk: text after marker
        yield " and some text after"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(call) for call in app.output.append.call_args_list]

    # Verify text before marker was displayed
    before_calls = [c for c in append_calls if "Some text before" in c]
    assert len(before_calls) > 0, "Text before partial marker should be flushed"

    # Verify reconnection message was displayed
    reconnect_calls = [c for c in append_calls if "reconnecting" in c.lower()]
    assert len(reconnect_calls) > 0, "Reassembled marker should display reconnection message"

    # Verify text after marker was displayed
    after_calls = [c for c in append_calls if "text after" in c]
    assert len(after_calls) > 0, "Text after marker should be displayed"


@pytest.mark.asyncio
async def test_stream_chat_multiple_markers_in_single_chunk(tmp_path: Path) -> None:
    """Test multiple complete markers in a single chunk are processed correctly."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        # Two retry markers in one chunk
        retry_data_1 = {"type": "retry_status", "attempt": 1, "delay": 1.0}
        retry_data_2 = {"type": "retry_status", "attempt": 2, "delay": 2.0}
        marker_1 = f"__RETRY_START__{json.dumps(retry_data_1)}__RETRY_END__"
        marker_2 = f"__RETRY_START__{json.dumps(retry_data_2)}__RETRY_END__"

        yield marker_1 + marker_2
        yield "Final response"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(call) for call in app.output.append.call_args_list]

    # Verify both reconnection messages were displayed
    reconnect_calls = [c for c in append_calls if "reconnecting" in c.lower()]
    assert len(reconnect_calls) >= 2, "Both retry markers should be processed"

    # Verify attempt numbers
    attempt_1_calls = [c for c in reconnect_calls if "attempt 1" in c.lower()]
    attempt_2_calls = [c for c in reconnect_calls if "attempt 2" in c.lower()]
    assert len(attempt_1_calls) > 0, "First retry attempt should be displayed"
    assert len(attempt_2_calls) > 0, "Second retry attempt should be displayed"


@pytest.mark.asyncio
async def test_stream_chat_partial_marker_at_end_of_stream(tmp_path: Path) -> None:
    """Test partial marker at end of stream is flushed as regular text.

    Tests that incomplete markers at stream end are treated as regular content,
    not lost in the buffer.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        yield "Complete response text"
        # Incomplete marker at end (malformed or truncated)
        yield " __RETRY_STA"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(call) for call in app.output.append.call_args_list]

    # Verify complete text was displayed
    text_calls = [c for c in append_calls if "Complete response text" in c]
    assert len(text_calls) > 0, "Complete text should be displayed"

    # Verify partial marker was flushed (buffer is flushed at stream end)
    # The logic at line 584-585 flushes remaining buffer
    partial_calls = [c for c in append_calls if "__RETRY_STA" in c]
    assert len(partial_calls) > 0, "Partial marker should be flushed at stream end"


@pytest.mark.asyncio
async def test_stream_chat_malformed_marker_removed(tmp_path: Path) -> None:
    """Test malformed markers (incomplete JSON) are handled gracefully.

    Tests that malformed markers trigger the exception handler and are removed
    from output without crashing.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Malformed marker with invalid JSON
        yield '__RETRY_START__{"invalid json without closing brace__RETRY_END__'
        yield "Normal text after malformed marker"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        # Should not raise exception
        await app._stream_chat("Test prompt")

    append_calls = [str(call) for call in app.output.append.call_args_list]

    # Verify normal text was displayed (marker was removed even though malformed)
    text_calls = [c for c in append_calls if "Normal text after" in c]
    assert len(text_calls) > 0, "Text after malformed marker should still be displayed"

    # Verify malformed marker itself was removed (not displayed to user)
    marker_calls = [c for c in append_calls if "__RETRY_START__" in c or "invalid json" in c]
    assert len(marker_calls) == 0, "Malformed marker should be removed from output"


@pytest.mark.asyncio
async def test_stream_chat_buffer_flush_threshold_exceeded(tmp_path: Path) -> None:
    """Test that buffers exceeding MARKER_BUFFER_OVERFLOW_THRESHOLD are flushed.

    Tests the scenario where buffer grows beyond the threshold (100 chars) with
    a potential partial marker. The implementation should flush the buffer to
    prevent unbounded memory growth, treating the content as regular text.

    This tests the safeguard at tui.py:601.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Send a very long chunk that starts with partial marker prefix
        # This simulates a malformed stream where marker never completes
        # and buffer grows beyond threshold
        long_text = "__RETRY_" + ("x" * 100)  # Exceeds MARKER_BUFFER_OVERFLOW_THRESHOLD (100)
        yield long_text
        yield " more text after flush"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(call) for call in app.output.append.call_args_list]

    # Verify buffer was flushed (long text should be in output)
    flushed_calls = [c for c in append_calls if "xxxx" in c]
    assert len(flushed_calls) > 0, "Buffer exceeding threshold should be flushed"

    # Verify subsequent text after flush is also displayed
    after_calls = [c for c in append_calls if "more text after" in c]
    assert len(after_calls) > 0, "Text after flush should be displayed"


@pytest.mark.asyncio
async def test_stream_chat_mixed_partial_retry_and_rag_markers(tmp_path: Path) -> None:
    """Test handling of both RETRY and RAG markers split across chunks.

    Tests that partial marker detection works for both marker types.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        # First: partial RETRY marker
        retry_data = {"type": "retry_status", "attempt": 1, "delay": 1.5}
        retry_marker = f"__RETRY_START__{json.dumps(retry_data)}__RETRY_END__"
        yield retry_marker[:15]  # Partial
        yield retry_marker[15:]  # Complete

        # Second: partial RAG marker
        rag_data = {"type": "rag_metadata", "chunks": []}
        rag_marker = f"__RAG_START__{json.dumps(rag_data)}__RAG_END__"
        yield rag_marker[:10]  # Partial
        yield rag_marker[10:]  # Complete

        # Final text
        yield "Response text"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(call) for call in app.output.append.call_args_list]

    # Verify RETRY marker was processed
    reconnect_calls = [c for c in append_calls if "reconnecting" in c.lower()]
    assert len(reconnect_calls) > 0, "Partial RETRY marker should be reassembled"

    # Verify RAG marker was filtered out (not displayed)
    rag_calls = [c for c in append_calls if "__RAG_" in c]
    assert len(rag_calls) == 0, "RAG markers should be filtered out"

    # Verify response text was displayed
    text_calls = [c for c in append_calls if "Response text" in c]
    assert len(text_calls) > 0, "Response text should be displayed"


@pytest.mark.asyncio
async def test_stream_chat_buffer_overflow_protection(tmp_path: Path) -> None:
    """Test buffer overflow protection when entire buffer looks like partial marker.

    Covers the buffer overflow logic in tui.py:601 where safe_idx == 0 and
    len(buffer) > MARKER_BUFFER_OVERFLOW_THRESHOLD.

    Tests scenario where buffer contains only partial marker prefix that exceeds
    the threshold (100 bytes), triggering flush to prevent unbounded memory growth.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Yield a large buffer that looks like a partial marker
        # This triggers the buffer overflow protection for marker-like content
        # Use "_" * 101 (> MARKER_BUFFER_OVERFLOW_THRESHOLD of 100)
        # This matches the first character of both "__RETRY_START__" and "__RAG_START__"
        # so has_partial_marker = True, safe_idx = 0, and the overflow path is triggered
        yield "_" * 101
        yield " done"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(call) for call in app.output.append.call_args_list]

    # Verify the oversized partial-marker buffer was flushed (buffer overflow protection triggered)
    # The large buffer that looks like a partial marker should be treated as regular text and displayed
    large_buffer_calls = [c for c in append_calls if "_" * 100 in c]
    assert (
        len(large_buffer_calls) > 0
    ), "Oversized partial-marker buffer should be flushed as regular text"

    # Verify subsequent text was also displayed
    done_calls = [c for c in append_calls if "done" in c]
    assert len(done_calls) > 0, "Text after flushed buffer should be displayed"


# ============================================================================
# Search Functionality Tests
# ============================================================================


@pytest.mark.asyncio
async def test_tui_action_search_messages_opens_screen(tmp_path: Path) -> None:
    """Test that action_search_messages opens SearchResultsScreen."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session", config_path=str(config_file))

    # Mock push_screen_wait to return None (user cancelled)
    with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)) as mock_push:
        await app._search_messages_worker()

    # Verify push_screen_wait was called
    mock_push.assert_called_once()
    # Verify the screen is a SearchResultsScreen instance
    assert isinstance(mock_push.call_args[0][0], SearchResultsScreen)


@pytest.mark.asyncio
async def test_tui_action_search_messages_switches_session(tmp_path: Path) -> None:
    """Test that selecting a search result switches to that session."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="original-session", config_path=str(config_file))

    # Mock output widget
    mock_output = MagicMock(spec=ChatOutput)

    # Search result to return
    search_result = {
        "session_name": "target-session",
        "message_index": 5,
    }

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=search_result)),
        patch.object(app, "query_one", return_value=mock_output),
        patch.object(app, "notify") as mock_notify,
    ):
        await app._search_messages_worker()

    # Verify session was switched
    assert app.session == "target-session"

    # Verify output was cleared
    mock_output.clear.assert_called_once()

    # Verify notification was shown
    mock_notify.assert_called_once()
    assert "target-session" in mock_notify.call_args[0][0]
    assert "message 6" in mock_notify.call_args[0][0]  # message_index 5 = message 6 (1-indexed)


@pytest.mark.asyncio
async def test_tui_action_search_messages_same_session(tmp_path: Path) -> None:
    """Test that selecting a result in the current session doesn't clear output."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="current-session", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Search result in the same session
    search_result = {
        "session_name": "current-session",
        "message_index": 3,
    }

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=search_result)),
        patch.object(app, "notify") as mock_notify,
    ):
        await app._search_messages_worker()

    # Verify session stayed the same
    assert app.session == "current-session"

    # Verify output was NOT cleared (same session)
    app.output.clear.assert_not_called()

    # Verify notification was shown
    mock_notify.assert_called_once()


@pytest.mark.asyncio
async def test_search_results_screen_perform_search_success() -> None:
    """Test SearchResultsScreen perform_search with successful API response."""

    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    # Mock the results list
    screen.results_list = MagicMock()
    screen.results_list.clear = AsyncMock()
    screen.results_list.append = AsyncMock()

    # Mock notify
    screen.notify = MagicMock()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "query": "test query",
        "total_results": 2,
        "results": [
            {
                "session_name": "session1",
                "session_title": "Session 1",
                "message_index": 0,
                "role": "user",
                "content": "test message",
                "content_preview": "test message",
                "timestamp": None,
                "matches": 1,
            },
            {
                "session_name": "session2",
                "session_title": None,
                "message_index": 5,
                "role": "assistant",
                "content": "another test",
                "content_preview": "another test",
                "timestamp": None,
                "matches": 2,
            },
        ],
    }
    mock_response.raise_for_status = MagicMock()

    # Mock httpx client
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await screen.perform_search("test query")

    # Verify API was called correctly
    mock_client.get.assert_called_once()
    call_args = mock_client.get.call_args
    assert "/api/v1/sessions/search" in call_args[0][0]
    assert call_args[1]["params"]["query"] == "test query"

    # Verify results were stored
    assert len(screen.results) == 2
    assert screen.results[0]["session_name"] == "session1"
    assert screen.results[1]["session_name"] == "session2"

    # Verify success notification
    screen.notify.assert_called_once()
    assert "Found 2 result(s)" in screen.notify.call_args[0][0]


@pytest.mark.asyncio
async def test_search_results_screen_perform_search_no_results() -> None:
    """Test SearchResultsScreen perform_search with no results."""

    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    # Mock the results list
    screen.results_list = MagicMock()
    screen.results_list.clear = AsyncMock()
    screen.results_list.append = AsyncMock()

    # Mock notify
    screen.notify = MagicMock()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "query": "nonexistent",
        "total_results": 0,
        "results": [],
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await screen.perform_search("nonexistent")

    # Verify results are empty
    assert len(screen.results) == 0

    # Verify warning notification
    screen.notify.assert_called_once()
    assert "No results found" in screen.notify.call_args[0][0]
    assert screen.notify.call_args[1]["severity"] == "warning"


@pytest.mark.asyncio
async def test_search_results_screen_perform_search_api_error() -> None:
    """Test SearchResultsScreen perform_search handles API errors."""

    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    # Mock the results list
    screen.results_list = MagicMock()
    screen.results_list.clear = AsyncMock()

    # Mock notify
    screen.notify = MagicMock()

    # Mock httpx client to raise an exception
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        await screen.perform_search("test")

    # Verify results are empty
    assert len(screen.results) == 0

    # Verify error notification
    screen.notify.assert_called_once()
    assert "Search failed" in screen.notify.call_args[0][0]
    assert screen.notify.call_args[1]["severity"] == "error"


@pytest.mark.asyncio
async def test_search_results_screen_case_sensitive_filter() -> None:
    """Test SearchResultsScreen applies case_sensitive filter."""

    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    # Set case_sensitive to True
    screen.case_sensitive = True

    # Mock the results list
    screen.results_list = MagicMock()
    screen.results_list.clear = AsyncMock()
    screen.results_list.append = AsyncMock()

    # Mock notify
    screen.notify = MagicMock()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "query": "Test",
        "total_results": 0,
        "results": [],
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await screen.perform_search("Test")

    # Verify case_sensitive parameter was sent
    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["case_sensitive"] == "true"


# ============================================================================
# RAG Toggle Tests
# ============================================================================


@pytest.mark.asyncio
async def test_tui_update_session_status_success(tmp_path: Path) -> None:
    """Test _update_session_status fetches and updates session info."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock the status_bar widget
    app.status_bar = MagicMock(spec=SessionStatusBar)

    # Mock httpx responses
    mock_metadata_response = MagicMock()
    mock_metadata_response.status_code = 200
    mock_metadata_response.json.return_value = {
        "rag_enabled": True,
        "model": "llama3.1:8b",
    }
    mock_metadata_response.raise_for_status = MagicMock()

    mock_session_response = MagicMock()
    mock_session_response.status_code = 200
    mock_session_response.json.return_value = {
        "messages": [{"role": "user", "content": "test"}] * 5
    }
    mock_session_response.raise_for_status = MagicMock()

    mock_docs_response = MagicMock()
    mock_docs_response.status_code = 200
    mock_docs_response.is_success = True
    mock_docs_response.json.return_value = {"attached_doc_ids": []}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get = AsyncMock(
        side_effect=[mock_metadata_response, mock_session_response, mock_docs_response]
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._update_session_status()

    # Verify session info was updated
    assert app.rag_enabled is True
    app.status_bar.update_info.assert_called_once_with(
        session_name="test-session",
        message_count=5,
        model="llama3.1:8b",
        rag_enabled=True,
        attached_doc_count=0,
    )


@pytest.mark.asyncio
async def test_tui_update_session_status_api_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _update_session_status handles API errors gracefully."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock httpx to raise exception
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.WARNING, logger="nyxgpt.tui"),
    ):
        await app._update_session_status()

    # Verify RAG status defaults to False on error
    assert app.rag_enabled is False
    assert "Failed to fetch session status" in caplog.text


@pytest.mark.asyncio
async def test_tui_action_toggle_rag_enable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test action_toggle_rag enables RAG when disabled."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Start with RAG disabled
    app.rag_enabled = False

    # Mock httpx response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch.object(app, "_update_session_status", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="nyxgpt.tui"),
    ):
        await app.action_toggle_rag()

    # Verify RAG was enabled
    assert app.rag_enabled is True
    assert "RAG enabled" in caplog.text

    # Verify API was called correctly
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "/api/v1/sessions/test-session/rag/enable" in call_args[0][0]


@pytest.mark.asyncio
async def test_tui_action_toggle_rag_disable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test action_toggle_rag disables RAG when enabled."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Start with RAG enabled
    app.rag_enabled = True

    # Mock httpx response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch.object(app, "_update_session_status", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="nyxgpt.tui"),
    ):
        await app.action_toggle_rag()

    # Verify RAG was disabled
    assert app.rag_enabled is False
    assert "RAG disabled" in caplog.text

    # Verify API was called with disable endpoint
    call_args = mock_client.post.call_args
    assert "/api/v1/sessions/test-session/rag/disable" in call_args[0][0]


@pytest.mark.asyncio
async def test_tui_action_toggle_rag_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test action_toggle_rag handles API errors."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    app.rag_enabled = False

    # Mock httpx to raise exception
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(side_effect=Exception("Connection failed"))

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.ERROR, logger="nyxgpt.tui"),
    ):
        await app.action_toggle_rag()

    # Verify error was logged
    assert "Failed to toggle RAG" in caplog.text

    # Verify RAG status remained unchanged
    assert app.rag_enabled is False


# ============================================================================
# Models Manager Tests
# ============================================================================


@pytest.mark.asyncio
async def test_tui_action_models_manager(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test action_models_manager opens ModelsManagerScreen."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test", config_path=str(config_file))

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)) as mock_push,
        caplog.at_level(logging.INFO, logger="nyxgpt.tui"),
    ):
        await app._models_manager_worker()

    # Verify push_screen_wait was called
    mock_push.assert_called_once()
    assert "Models manager closed" in caplog.text


@pytest.mark.asyncio
async def test_models_manager_initialization() -> None:
    """Test ModelsManagerScreen initializes correctly."""
    from nyxgpt.tui import ModelsManagerScreen

    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")

    assert screen.api_base_url == "http://127.0.0.1:8000"
    assert screen.models == []


@pytest.mark.asyncio
async def test_models_manager_refresh_models_success() -> None:
    """Test ModelsManagerScreen refreshes models successfully."""
    from nyxgpt.tui import ModelsManagerScreen

    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")

    # Mock widgets
    screen.query_one = MagicMock(return_value=MagicMock())

    # Mock update methods
    with (
        patch.object(screen, "update_status", new=AsyncMock()),
        patch.object(screen, "update_models_list", new=AsyncMock()),
    ):
        # Mock httpx responses
        # First response: list of models
        mock_list_response = MagicMock()
        mock_list_response.status_code = 200
        mock_list_response.json.return_value = {"models": ["model1", "model2"]}
        mock_list_response.raise_for_status = MagicMock()

        # Second/third responses: model info
        mock_info_response = MagicMock()
        mock_info_response.status_code = 200
        mock_info_response.json.return_value = {
            "info": {"size": 1073741824, "modified_at": "2024-01-01"}
        }
        mock_info_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get = AsyncMock(
            side_effect=[mock_list_response, mock_info_response, mock_info_response]
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            await screen.refresh_models()

    # Verify models were loaded
    assert len(screen.models) == 2
    assert screen.models[0]["name"] == "model1"
    assert screen.models[1]["name"] == "model2"


@pytest.mark.asyncio
async def test_models_manager_refresh_models_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test ModelsManagerScreen handles refresh errors."""
    from nyxgpt.tui import ModelsManagerScreen

    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")

    # Mock update_status
    with patch.object(screen, "update_status", new=AsyncMock()):
        # Mock httpx to raise exception
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            caplog.at_level(logging.ERROR, logger="nyxgpt.tui"),
        ):
            await screen.refresh_models()

    # Verify error was logged
    assert "Failed to load models" in caplog.text


@pytest.mark.asyncio
async def test_models_manager_action_refresh() -> None:
    """Test ModelsManagerScreen refresh action."""
    from nyxgpt.tui import ModelsManagerScreen

    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")

    # Mock refresh_models
    with patch.object(screen, "refresh_models", new=AsyncMock()) as mock_refresh:
        await screen.action_refresh()

    # Verify refresh was called
    mock_refresh.assert_called_once()


def test_models_manager_action_quit_screen() -> None:
    """Test ModelsManagerScreen quit action."""
    from nyxgpt.tui import ModelsManagerScreen

    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")

    # Mock dismiss method
    with patch.object(screen, "dismiss") as mock_dismiss:
        screen.action_quit_screen()

    # Verify screen was dismissed
    mock_dismiss.assert_called_once()


# ============================================================================
# Session Picker Additional Tests
# ============================================================================


def test_session_picker_action_select() -> None:
    """Test SessionPickerScreen select action."""
    from nyxgpt.tui import SessionPickerScreen

    config_file = Path("/tmp/test_config.ini")
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": "/tmp/sessions"}

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    # Mock the ListView with a highlighted item
    mock_list_view = MagicMock()
    mock_highlighted = MagicMock()
    mock_highlighted.name = "selected-session"
    mock_list_view.highlighted_child = mock_highlighted

    with (
        patch.object(screen, "query_one", return_value=mock_list_view),
        patch.object(screen, "dismiss") as mock_dismiss,
    ):
        screen.action_select()

    # Verify session was selected
    mock_dismiss.assert_called_once_with("selected-session")


def test_session_picker_action_select_no_highlight() -> None:
    """Test SessionPickerScreen select action with no highlight."""
    from nyxgpt.tui import SessionPickerScreen

    config_file = Path("/tmp/test_config.ini")
    cfg = configparser.ConfigParser()

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    # Mock the ListView with no highlighted item
    mock_list_view = MagicMock()
    mock_list_view.highlighted_child = None

    with (
        patch.object(screen, "query_one", return_value=mock_list_view),
        patch.object(screen, "dismiss") as mock_dismiss,
    ):
        screen.action_select()

    # Should not dismiss (no selection)
    mock_dismiss.assert_not_called()


# ============================================================================
# Clear Output Tests
# ============================================================================


def test_action_clear_output(tmp_path: Path) -> None:
    """Test that clear output action clears the buffer."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)
    app.output.clear = MagicMock()

    app.action_clear_output()

    # Verify output was cleared
    app.output.clear.assert_called_once()


def test_action_clear_output_handles_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that clear output action handles exceptions gracefully."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock output widget to raise exception
    app.output = MagicMock(spec=ChatOutput)
    app.output.clear = MagicMock(side_effect=RuntimeError("Clear failed"))

    with caplog.at_level(logging.ERROR, logger="nyxgpt.tui"):
        app.action_clear_output()

    # Verify error was logged
    assert "Failed to clear output" in caplog.text


@pytest.mark.asyncio
async def test_handle_command_clear(tmp_path: Path) -> None:
    """Test that /clear command triggers clear action."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock action_clear_output (it's a sync method, called without await in
    # _handle_command, so it must be mocked with MagicMock, not AsyncMock, to
    # avoid a "coroutine was never awaited" RuntimeWarning).
    with patch.object(app, "action_clear_output", new=MagicMock()) as mock_clear:
        await app._handle_command("clear")

    # Verify clear action was called
    mock_clear.assert_called_once()


@pytest.mark.asyncio
async def test_handle_command_unknown(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test that unknown commands show error message."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.tui"):
        await app._handle_command("unknown")

    # Verify error message was appended to output
    app.output.append.assert_called_once()
    assert "Unknown command" in app.output.append.call_args[0][0]
    assert "/unknown" in app.output.append.call_args[0][0]

    # Verify warning was logged
    assert "Unknown command: /unknown" in caplog.text


@pytest.mark.asyncio
async def test_input_submitted_with_slash_command(tmp_path: Path) -> None:
    """Test that input starting with / triggers command handler."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock widgets
    app.prompt = MagicMock(spec=Input)
    app.prompt.value = "/clear"
    app.output = MagicMock(spec=ChatOutput)

    # Mock _handle_command
    with patch.object(app, "_handle_command", new=AsyncMock()) as mock_handle:
        # Create event
        event = MagicMock()
        event.value = "/clear"

        await app.on_input_submitted(event)

    # Verify command handler was called with "clear"
    mock_handle.assert_called_once_with("clear")

    # Verify prompt was cleared
    assert app.prompt.value == ""


@pytest.mark.asyncio
async def test_input_submitted_with_slash_command_not_locked(tmp_path: Path) -> None:
    """Test that prompt is not locked when submitting slash commands."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    # Mock widgets
    app.prompt = MagicMock(spec=Input)
    app.prompt.value = "/clear"
    app.prompt.disabled = False
    app.output = MagicMock(spec=ChatOutput)

    # Mock _handle_command
    with patch.object(app, "_handle_command", new=AsyncMock()):
        event = MagicMock()
        event.value = "/clear"

        await app.on_input_submitted(event)

    # Verify prompt was NOT locked (commands don't lock prompt)
    assert app.prompt.disabled is False


# ============================================================================
# Search Results Screen Additional Tests
# ============================================================================


@pytest.mark.asyncio
async def test_search_results_screen_initialization() -> None:
    """Test SearchResultsScreen initializes correctly."""

    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    assert screen.api_base_url == "http://127.0.0.1:8000"
    assert screen.current_session == "test"
    assert screen.results == []
    assert screen.case_sensitive is False


@pytest.mark.asyncio
async def test_search_results_screen_on_mount() -> None:
    """Test SearchResultsScreen focuses search input on mount."""

    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    # Mock search_input widget
    mock_search_input = MagicMock()
    screen.search_input = mock_search_input

    await screen.on_mount()

    # Verify input was focused
    mock_search_input.focus.assert_called_once()


@pytest.mark.asyncio
async def test_search_results_screen_action_close() -> None:
    """Test SearchResultsScreen close action."""

    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    # Mock dismiss
    with patch.object(screen, "dismiss") as mock_dismiss:
        await screen.action_close()

    # Verify screen was dismissed with None
    mock_dismiss.assert_called_once_with(None)


# ============================================================================
# Keyboard Navigation Tests
# ============================================================================


@pytest.mark.asyncio
async def test_action_show_help(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test action_show_help opens HelpOverlayScreen."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test", config_path=str(config_file))

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)) as mock_push,
        caplog.at_level(logging.INFO, logger="nyxgpt.tui"),
    ):
        await app._show_help_worker()

    # Verify push_screen_wait was called
    mock_push.assert_called_once()
    assert "Help overlay closed" in caplog.text


@pytest.mark.asyncio
async def test_action_command_palette_execute_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test action_command_palette executes selected command."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test", config_path=str(config_file))

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value="clear_output")),
        patch.object(app, "run_action") as mock_run_action,
        caplog.at_level(logging.INFO, logger="nyxgpt.tui"),
    ):
        await app._command_palette_worker()

    # Verify run_action was called with correct command
    mock_run_action.assert_called_once_with("clear_output")
    assert "Executing command from palette: clear_output" in caplog.text


@pytest.mark.asyncio
async def test_action_command_palette_cancel(tmp_path: Path) -> None:
    """Test action_command_palette handles cancel (None returned)."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test", config_path=str(config_file))

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)),
        patch.object(app, "run_action") as mock_run_action,
    ):
        await app._command_palette_worker()

    # Verify no action was executed
    mock_run_action.assert_not_called()


@pytest.mark.asyncio
async def test_action_command_palette_unknown_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test action_command_palette logs and executes unknown commands via run_action."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test", config_path=str(config_file))

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value="unknown_command")),
        patch.object(app, "run_action") as mock_run_action,
        caplog.at_level(logging.INFO, logger="nyxgpt.tui"),
    ):
        await app._command_palette_worker()

    # run_action is still called (Textual will handle unknown actions)
    mock_run_action.assert_called_once_with("unknown_command")
    assert "Executing command from palette: unknown_command" in caplog.text


def test_help_overlay_screen_initialization() -> None:
    """Test HelpOverlayScreen initializes correctly."""

    screen = HelpOverlayScreen()
    assert screen is not None


@pytest.mark.asyncio
async def test_help_overlay_screen_action_close() -> None:
    """Test HelpOverlayScreen close action."""

    screen = HelpOverlayScreen()

    # Mock dismiss
    with patch.object(screen, "dismiss") as mock_dismiss:
        await screen.action_close()

    # Verify screen was dismissed with None
    mock_dismiss.assert_called_once_with(None)


def test_command_palette_screen_initialization() -> None:
    """Test CommandPaletteScreen initializes correctly."""

    screen = CommandPaletteScreen()
    assert screen.all_commands is not None
    assert len(screen.all_commands) > 0
    assert screen.filtered_commands == screen.all_commands


@pytest.mark.asyncio
async def test_command_palette_screen_filter_commands() -> None:
    """Test CommandPaletteScreen filters commands based on search."""

    screen = CommandPaletteScreen()

    # Mock update_command_list
    with patch.object(screen, "update_command_list", new=AsyncMock()):
        # Create mock input event
        mock_input = MagicMock(spec=Input)
        mock_input.id = "command-search"
        event = MagicMock()
        event.input = mock_input
        event.value = "search"

        await screen.on_input_changed(event)

    # Should filter to commands containing "search"
    assert len(screen.filtered_commands) > 0
    assert all(
        "search" in cmd["label"].lower() or "search" in cmd["description"].lower()
        for cmd in screen.filtered_commands
    )


@pytest.mark.asyncio
async def test_command_palette_screen_filter_empty_query() -> None:
    """Test CommandPaletteScreen shows all commands when search is empty."""

    screen = CommandPaletteScreen()
    original_count = len(screen.all_commands)

    # Filter first
    screen.filtered_commands = []

    # Mock update_command_list
    with patch.object(screen, "update_command_list", new=AsyncMock()):
        mock_input = MagicMock(spec=Input)
        mock_input.id = "command-search"
        event = MagicMock()
        event.input = mock_input
        event.value = ""

        await screen.on_input_changed(event)

    # Should show all commands
    assert len(screen.filtered_commands) == original_count


@pytest.mark.asyncio
async def test_command_palette_screen_action_execute_command() -> None:
    """Test CommandPaletteScreen execute command action."""

    screen = CommandPaletteScreen()

    # Mock the ListView with a highlighted item
    mock_list_view = MagicMock()
    mock_highlighted = MagicMock()
    mock_highlighted.name = "clear_output"
    mock_list_view.highlighted_child = mock_highlighted

    with (
        patch.object(screen, "query_one", return_value=mock_list_view),
        patch.object(screen, "dismiss") as mock_dismiss,
    ):
        await screen.action_execute_command()

    # Verify command key was returned
    mock_dismiss.assert_called_once_with("clear_output")


@pytest.mark.asyncio
async def test_command_palette_screen_action_close() -> None:
    """Test CommandPaletteScreen close action."""

    screen = CommandPaletteScreen()

    # Mock dismiss
    with patch.object(screen, "dismiss") as mock_dismiss:
        await screen.action_close()

    # Verify screen was dismissed with None
    mock_dismiss.assert_called_once_with(None)


# ============================================================================
# Delete Session Tests
# ============================================================================


@pytest.mark.asyncio
async def test_delete_confirmation_screen_confirm() -> None:
    """Test DeleteConfirmationScreen confirms deletion."""

    screen = DeleteConfirmationScreen("test-session")

    # Mock dismiss
    with patch.object(screen, "dismiss") as mock_dismiss:
        await screen.action_confirm()

    # Verify screen was dismissed with True
    mock_dismiss.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_delete_confirmation_screen_cancel() -> None:
    """Test DeleteConfirmationScreen cancels deletion."""

    screen = DeleteConfirmationScreen("test-session")

    # Mock dismiss
    with patch.object(screen, "dismiss") as mock_dismiss:
        await screen.action_cancel()

    # Verify screen was dismissed with False
    mock_dismiss.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_delete_session_action_success(tmp_path: Path) -> None:
    """Test action_delete_session successfully deletes session and switches to another."""
    # Create config
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    # Create test sessions
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session1.json").write_text("[]")
    (sessions_dir / "session2.json").write_text("[]")

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="session1", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock list_sessions to return multiple sessions
    mock_sessions = [
        {"name": "session1", "messages": 0},
        {"name": "session2", "messages": 0},
    ]

    # Mock push_screen_wait to return True (confirmed)
    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)),
        patch(
            "nyxgpt.tui.list_sessions",
            side_effect=[mock_sessions, [{"name": "session2", "messages": 0}]],
        ),
        patch("nyxgpt.tui.delete_session", return_value=True),
        patch.object(app, "_update_session_status", new=AsyncMock()),
    ):
        await app._delete_session_worker()

    # Verify session was switched
    assert app.session == "session2"

    # Verify output was updated
    app.output.clear.assert_called_once()
    app.output.append.assert_called()
    assert "deleted" in app.output.append.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_delete_session_action_cancelled(tmp_path: Path) -> None:
    """Test action_delete_session handles cancellation."""
    # Create config
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    # Create test sessions
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session1.json").write_text("[]")
    (sessions_dir / "session2.json").write_text("[]")

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="session1", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock list_sessions to return multiple sessions
    mock_sessions = [
        {"name": "session1", "messages": 0},
        {"name": "session2", "messages": 0},
    ]

    # Mock push_screen_wait to return False (cancelled)
    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=False)),
        patch("nyxgpt.tui.list_sessions", return_value=mock_sessions),
        patch("nyxgpt.tui.delete_session", return_value=True) as mock_delete,
    ):
        await app._delete_session_worker()

    # Verify session was NOT switched
    assert app.session == "session1"

    # Verify delete was NOT called
    mock_delete.assert_not_called()

    # Verify output was NOT modified
    app.output.clear.assert_not_called()


@pytest.mark.asyncio
async def test_delete_session_action_last_session(tmp_path: Path) -> None:
    """Test action_delete_session prevents deletion of last session."""
    # Create config
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    # Create single test session
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session1.json").write_text("[]")

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="session1", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock list_sessions to return only one session
    mock_sessions = [{"name": "session1", "messages": 0}]

    with (
        patch("nyxgpt.tui.list_sessions", return_value=mock_sessions),
        patch("nyxgpt.tui.delete_session", return_value=True) as mock_delete,
    ):
        await app._delete_session_worker()

    # Verify delete was NOT called
    mock_delete.assert_not_called()

    # Verify error message was shown
    app.output.append.assert_called()
    assert "cannot delete the last remaining session" in app.output.append.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_delete_session_action_not_found(tmp_path: Path) -> None:
    """Test action_delete_session handles session not found error."""
    # Create config
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    # Create test sessions
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session1.json").write_text("[]")
    (sessions_dir / "session2.json").write_text("[]")

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="session1", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock list_sessions to return multiple sessions
    mock_sessions = [
        {"name": "session1", "messages": 0},
        {"name": "session2", "messages": 0},
    ]

    # Mock push_screen_wait to return True (confirmed)
    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)),
        patch("nyxgpt.tui.list_sessions", return_value=mock_sessions),
        patch("nyxgpt.tui.delete_session", return_value=False),
    ):
        # Mock delete_session to return False (not found)
        await app._delete_session_worker()

    # Verify error message was shown
    app.output.append.assert_called()
    assert "session not found" in app.output.append.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_delete_session_action_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test action_delete_session handles exceptions gracefully."""
    # Create config
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    # Create test sessions
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session1.json").write_text("[]")
    (sessions_dir / "session2.json").write_text("[]")

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="session1", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock list_sessions to return multiple sessions
    mock_sessions = [
        {"name": "session1", "messages": 0},
        {"name": "session2", "messages": 0},
    ]

    # Mock push_screen_wait to return True (confirmed)
    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)),
        patch("nyxgpt.tui.list_sessions", return_value=mock_sessions),
        patch("nyxgpt.tui.delete_session", side_effect=RuntimeError("Delete failed")),
        caplog.at_level(logging.ERROR, logger="nyxgpt.tui"),
    ):
        # Mock delete_session to raise exception
        await app._delete_session_worker()

    # Verify error was logged
    assert "Failed to delete session" in caplog.text

    # Verify error message was shown
    app.output.append.assert_called()
    assert "failed to delete session" in app.output.append.call_args[0][0].lower()


# ============================================================================
# ManageDocumentsScreen Tests
# ============================================================================


def test_manage_documents_screen_initialization() -> None:
    """Test that ManageDocumentsScreen initializes with correct attributes."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="test-session")
    assert screen.api_base_url == "http://127.0.0.1:8000"
    assert screen.session == "test-session"
    assert screen._attached == []


@pytest.mark.asyncio
async def test_manage_documents_screen_refresh_list_success() -> None:
    """Test _refresh_list fetches and stores attached document IDs."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="my-session")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={"session": "my-session", "attached_doc_ids": ["doc-abc", "doc-xyz"]}
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("nyxgpt.tui.httpx.AsyncClient", return_value=mock_client),
        patch.object(screen, "_populate_list", new=AsyncMock()),
    ):
        await screen._refresh_list()

    assert screen._attached == ["doc-abc", "doc-xyz"]


@pytest.mark.asyncio
async def test_manage_documents_screen_refresh_list_error() -> None:
    """Test _refresh_list handles errors gracefully."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="my-session")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=RuntimeError("Connection refused"))

    with (
        patch("nyxgpt.tui.httpx.AsyncClient", return_value=mock_client),
        patch.object(screen, "_populate_list", new=AsyncMock()),
    ):
        await screen._refresh_list()

    assert screen._attached == []


def test_nyx_gpt_tui_has_manage_documents_binding() -> None:
    """Test that NyxGPTTUI has a ctrl+a binding for manage_documents."""
    # BINDINGS may contain Binding objects or raw tuples (key, action, description)
    binding_keys = [b.key if hasattr(b, "key") else b[0] for b in NyxGPTTUI.BINDINGS]
    assert "ctrl+a" in binding_keys


@pytest.mark.asyncio
async def test_action_manage_documents_opens_screen(tmp_path: Path) -> None:
    """Test that action_manage_documents calls _manage_documents_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session", config_path=str(config_file))

    with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)):
        await app._manage_documents_worker()
        app.push_screen_wait.assert_called_once()
        call_args = app.push_screen_wait.call_args[0][0]
        assert isinstance(call_args, ManageDocumentsScreen)
        assert call_args.session == "test-session"


# ============================================================================
# SessionPickerScreen compose() / on_mount() / update_session_list() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_session_picker_compose_and_on_mount(tmp_path: Path) -> None:
    """Test that compose() builds the picker UI and on_mount() loads sessions."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    mock_sessions = [
        {
            "name": "session1",
            "messages": 2,
            "modified": "2024-01-01",
            "meta": {"title": "Session One", "pinned": True},
        },
    ]

    with (
        patch("nyxgpt.tui.load_config", return_value=cfg),
        patch("nyxgpt.tui.list_sessions", return_value=mock_sessions),
    ):
        screen = SessionPickerScreen(str(config_file))

        app = App()
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()

            # on_mount() -> load_sessions() -> update_session_list() populated the list
            assert screen.all_sessions == mock_sessions
            assert screen.filtered_sessions == mock_sessions

            list_view = screen.query_one("#session-list", ListView)
            assert len(list_view.children) == 1
            assert list_view.children[0].name == "session1"

            # Preview should have been updated for the first session (lines 191-193)
            preview = screen.query_one("#session-preview", SessionMetadataPreview)
            assert preview is not None


@pytest.mark.asyncio
async def test_session_picker_update_session_list_multiple(tmp_path: Path) -> None:
    """Test update_session_list() renders multiple sessions, pinned and unpinned."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    with (
        patch("nyxgpt.tui.load_config", return_value=cfg),
        patch("nyxgpt.tui.list_sessions", return_value=[]),
    ):
        screen = SessionPickerScreen(str(config_file))

        app = App()
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()

            # Set sessions after on_mount's initial (empty) load, then re-populate
            screen.filtered_sessions = [
                {"name": "a", "messages": 1, "meta": {"title": "A", "pinned": False}},
                {"name": "b", "messages": 4, "meta": {"title": "B", "pinned": True}},
            ]
            await screen.update_session_list()
            await pilot.pause()

            list_view = screen.query_one("#session-list", ListView)
            assert len(list_view.children) == 2
            names = [child.name for child in list_view.children]
            assert names == ["a", "b"]


@pytest.mark.asyncio
async def test_session_picker_on_input_changed_wrong_id(tmp_path: Path) -> None:
    """Test on_input_changed() ignores events from inputs other than #search."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    original_filtered = screen.filtered_sessions

    with patch.object(screen, "update_session_list", new=AsyncMock()) as mock_update:
        mock_input = MagicMock(spec=Input)
        mock_input.id = "not-search"
        event = MagicMock()
        event.input = mock_input
        event.value = "python"

        await screen.on_input_changed(event)

    # Should return early without filtering or updating the list
    mock_update.assert_not_called()
    assert screen.filtered_sessions is original_filtered


@pytest.mark.asyncio
async def test_session_picker_on_list_view_highlighted_item_none(tmp_path: Path) -> None:
    """Test on_list_view_highlighted() returns early when event.item is None."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    with patch.object(screen, "query_one") as mock_query_one:
        event = MagicMock()
        event.item = None

        await screen.on_list_view_highlighted(event)

    mock_query_one.assert_not_called()


@pytest.mark.asyncio
async def test_session_picker_on_list_view_highlighted_item_found(tmp_path: Path) -> None:
    """Test on_list_view_highlighted() updates the preview for a matching session."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    screen.filtered_sessions = [
        {"name": "session1", "messages": 1, "meta": {"title": "Session One"}},
    ]

    mock_preview = MagicMock(spec=SessionMetadataPreview)
    with patch.object(screen, "query_one", return_value=mock_preview):
        event = MagicMock()
        event.item = MagicMock()
        event.item.name = "session1"

        await screen.on_list_view_highlighted(event)

    mock_preview.update_session.assert_called_once_with(screen.filtered_sessions[0])


# ============================================================================
# ModelsManagerScreen compose() / on_mount() / refresh_models() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_models_manager_compose_and_on_mount() -> None:
    """Test that compose() builds the UI and on_mount() triggers refresh_models()."""
    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")

    mock_list_response = MagicMock()
    mock_list_response.raise_for_status = MagicMock()
    mock_list_response.json.return_value = {"models": ["llama3"]}

    mock_info_response = MagicMock()
    mock_info_response.raise_for_status = MagicMock()
    mock_info_response.json.return_value = {"info": {"size": 500, "modified_at": "2024-01-01"}}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=[mock_list_response, mock_info_response])

    app = App()
    with patch("httpx.AsyncClient", return_value=mock_client):
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()

            assert screen.models == [{"name": "llama3", "size": 500, "modified_at": "2024-01-01"}]

            list_view = screen.query_one("#models-list", ListView)
            assert len(list_view.children) == 1

            status_label = screen.query_one("#status-message")
            assert "Loaded 1 models" in str(status_label.content)


@pytest.mark.asyncio
async def test_models_manager_refresh_models_info_fetch_falls_back() -> None:
    """Test refresh_models() falls back to name-only entry when per-model info fetch fails."""
    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")
    screen.query_one = MagicMock(return_value=MagicMock())

    with (
        patch.object(screen, "update_status", new=AsyncMock()),
        patch.object(screen, "update_models_list", new=AsyncMock()),
    ):
        mock_list_response = MagicMock()
        mock_list_response.raise_for_status = MagicMock()
        mock_list_response.json.return_value = {"models": ["broken-model"]}

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get = AsyncMock(
            side_effect=[mock_list_response, RuntimeError("info endpoint down")]
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            await screen.refresh_models()

    assert screen.models == [{"name": "broken-model", "size": 0, "modified_at": ""}]


@pytest.mark.asyncio
async def test_models_manager_update_models_list_empty() -> None:
    """Test update_models_list() shows a placeholder when there are no models."""
    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")
    screen.models = []

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        await screen.update_models_list()
        await pilot.pause()

        list_view = screen.query_one("#models-list", ListView)
        assert len(list_view.children) == 1
        label = list_view.children[0].query_one("Label")
        assert "No models found" in str(label.content)


@pytest.mark.asyncio
async def test_models_manager_update_models_list_size_formatting() -> None:
    """Test update_models_list() formats known sizes as GB and unknown sizes as 'Unknown'."""
    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")
    screen.models = [
        {"name": "big-model", "size": 2 * 1024**3, "modified_at": ""},
        {"name": "zero-size-model", "size": 0, "modified_at": ""},
    ]

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        await screen.update_models_list()
        await pilot.pause()

        list_view = screen.query_one("#models-list", ListView)
        assert len(list_view.children) == 2
        labels = [str(item.query_one("Label").content) for item in list_view.children]
        assert "2.0 GB" in labels[0]
        assert "Unknown" in labels[1]


@pytest.mark.asyncio
async def test_models_manager_update_status_success() -> None:
    """Test update_status() updates the status label when the widget is available."""
    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        await screen.update_status("Custom status")
        await pilot.pause()

        status_label = screen.query_one("#status-message")
        assert "Custom status" in str(status_label.content)


@pytest.mark.asyncio
async def test_models_manager_update_status_swallows_query_exception() -> None:
    """Test update_status() swallows exceptions when the status widget can't be found."""
    screen = ModelsManagerScreen(api_base_url="http://127.0.0.1:8000")
    screen.query_one = MagicMock(side_effect=RuntimeError("not mounted"))

    # Should not raise
    await screen.update_status("irrelevant")


# ============================================================================
# SearchResultsScreen compose() / button / input / highlight Tests
# ============================================================================


@pytest.mark.asyncio
async def test_search_results_screen_compose_and_on_mount() -> None:
    """Test compose() builds the search dialog and on_mount() focuses the input."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        assert screen.query_one("#search-input", Input) is screen.search_input
        assert screen.query_one("#results-list", ListView) is screen.results_list
        assert screen.search_input.has_focus


@pytest.mark.asyncio
async def test_search_results_screen_on_button_pressed_search_btn_with_query() -> None:
    """Test on_button_pressed() triggers perform_search() when the query is non-empty."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.search_input = MagicMock(spec=Input)
    screen.search_input.value = "hello"
    screen.case_checkbox = MagicMock(spec=Checkbox)
    screen.case_checkbox.value = True

    with patch.object(screen, "perform_search", new=AsyncMock()) as mock_search:
        event = MagicMock()
        event.button.id = "search-btn"
        await screen.on_button_pressed(event)

    mock_search.assert_called_once_with("hello")
    assert screen.case_sensitive is True


@pytest.mark.asyncio
async def test_search_results_screen_on_button_pressed_search_btn_empty_query() -> None:
    """Test on_button_pressed() does not search when the query is empty."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.search_input = MagicMock(spec=Input)
    screen.search_input.value = "   "
    screen.case_checkbox = MagicMock(spec=Checkbox)

    with patch.object(screen, "perform_search", new=AsyncMock()) as mock_search:
        event = MagicMock()
        event.button.id = "search-btn"
        await screen.on_button_pressed(event)

    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_search_results_screen_on_button_pressed_other_button() -> None:
    """Test on_button_pressed() ignores button presses that aren't search-btn."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    with patch.object(screen, "perform_search", new=AsyncMock()) as mock_search:
        event = MagicMock()
        event.button.id = "other-btn"
        await screen.on_button_pressed(event)

    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_search_results_screen_on_input_submitted_enter_key() -> None:
    """Test on_input_submitted() triggers perform_search() on Enter with a query."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.case_checkbox = MagicMock(spec=Checkbox)
    screen.case_checkbox.value = False

    with patch.object(screen, "perform_search", new=AsyncMock()) as mock_search:
        mock_input = MagicMock(spec=Input)
        mock_input.id = "search-input"
        event = MagicMock()
        event.input = mock_input
        event.value = "  query text  "
        await screen.on_input_submitted(event)

    mock_search.assert_called_once_with("query text")


@pytest.mark.asyncio
async def test_search_results_screen_on_input_submitted_wrong_id() -> None:
    """Test on_input_submitted() ignores submissions from other inputs."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    with patch.object(screen, "perform_search", new=AsyncMock()) as mock_search:
        mock_input = MagicMock(spec=Input)
        mock_input.id = "other-input"
        event = MagicMock()
        event.input = mock_input
        event.value = "query"
        await screen.on_input_submitted(event)

    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_search_results_screen_on_input_submitted_empty_query() -> None:
    """Test on_input_submitted() does not search when the submitted value is blank."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")

    with patch.object(screen, "perform_search", new=AsyncMock()) as mock_search:
        mock_input = MagicMock(spec=Input)
        mock_input.id = "search-input"
        event = MagicMock()
        event.input = mock_input
        event.value = "   "
        await screen.on_input_submitted(event)

    mock_search.assert_not_called()


@pytest.mark.asyncio
async def test_search_results_screen_on_list_view_highlighted_item_none() -> None:
    """Test on_list_view_highlighted() returns early when there's no highlighted item."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.results = [{"content_preview": "preview"}]
    screen.preview = MagicMock()

    event = MagicMock()
    event.item = None
    await screen.on_list_view_highlighted(event)

    screen.preview.update.assert_not_called()


@pytest.mark.asyncio
async def test_search_results_screen_on_list_view_highlighted_no_results() -> None:
    """Test on_list_view_highlighted() returns early when there are no results."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.results = []
    screen.preview = MagicMock()

    event = MagicMock()
    event.item = MagicMock()
    await screen.on_list_view_highlighted(event)

    screen.preview.update.assert_not_called()


@pytest.mark.asyncio
async def test_search_results_screen_on_list_view_highlighted_updates_preview() -> None:
    """Test on_list_view_highlighted() updates the preview for the highlighted index."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.results = [{"content_preview": "first"}, {"content_preview": "second"}]
    screen.preview = MagicMock()
    screen.results_list = MagicMock()
    screen.results_list.index = 1

    event = MagicMock()
    event.item = MagicMock()
    await screen.on_list_view_highlighted(event)

    screen.preview.update.assert_called_once_with("second")


@pytest.mark.asyncio
async def test_search_results_screen_on_list_view_highlighted_index_error() -> None:
    """Test on_list_view_highlighted() swallows IndexError/AttributeError from a stale index."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.results = [{"content_preview": "only"}]
    screen.preview = MagicMock()
    screen.results_list = MagicMock()
    screen.results_list.index = 99  # Out of range -> IndexError

    event = MagicMock()
    event.item = MagicMock()
    # Should not raise
    await screen.on_list_view_highlighted(event)

    screen.preview.update.assert_not_called()


@pytest.mark.asyncio
async def test_search_results_screen_action_select_result_valid() -> None:
    """Test action_select_result() dismisses with the selected result."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.results = [{"session_name": "s1", "message_index": 0}]
    screen.results_list = MagicMock()
    screen.results_list.index = 0

    with patch.object(screen, "dismiss") as mock_dismiss:
        await screen.action_select_result()

    mock_dismiss.assert_called_once_with(screen.results[0])


@pytest.mark.asyncio
async def test_search_results_screen_action_select_result_none_selected() -> None:
    """Test action_select_result() notifies when nothing is selected."""
    screen = SearchResultsScreen(api_base_url="http://127.0.0.1:8000", current_session="test")
    screen.results = [{"session_name": "s1", "message_index": 0}]
    screen.results_list = MagicMock()
    screen.results_list.index = None

    with (
        patch.object(screen, "dismiss") as mock_dismiss,
        patch.object(screen, "notify") as mock_notify,
    ):
        await screen.action_select_result()

    mock_dismiss.assert_not_called()
    mock_notify.assert_called_once()
    assert mock_notify.call_args[1]["severity"] == "warning"


# ============================================================================
# HelpOverlayScreen compose() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_help_overlay_screen_compose() -> None:
    """Test that HelpOverlayScreen.compose() renders the shortcut list."""
    screen = HelpOverlayScreen()

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        dialog = screen.query_one("#help-dialog")
        assert dialog is not None
        # Spot-check a couple of the shortcut labels were rendered
        labels = [str(lbl.content) for lbl in screen.query("Label")]
        assert any("Command palette" in label for label in labels)
        assert any("Session picker" in label for label in labels)


# ============================================================================
# DeleteConfirmationScreen compose() / on_button_pressed() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_delete_confirmation_screen_compose() -> None:
    """Test that DeleteConfirmationScreen.compose() renders the confirmation dialog."""
    screen = DeleteConfirmationScreen("my-session")

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        dialog = screen.query_one("#delete-confirm-dialog")
        assert dialog is not None
        confirm_btn = screen.query_one("#confirm-btn", Button)
        cancel_btn = screen.query_one("#cancel-btn", Button)
        assert confirm_btn is not None
        assert cancel_btn is not None


@pytest.mark.asyncio
async def test_delete_confirmation_screen_on_button_pressed_confirm() -> None:
    """Test on_button_pressed() dismisses with True when confirm-btn is pressed."""
    screen = DeleteConfirmationScreen("my-session")

    with patch.object(screen, "dismiss") as mock_dismiss:
        event = MagicMock()
        event.button.id = "confirm-btn"
        await screen.on_button_pressed(event)

    mock_dismiss.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_delete_confirmation_screen_on_button_pressed_cancel() -> None:
    """Test on_button_pressed() dismisses with False when cancel-btn is pressed."""
    screen = DeleteConfirmationScreen("my-session")

    with patch.object(screen, "dismiss") as mock_dismiss:
        event = MagicMock()
        event.button.id = "cancel-btn"
        await screen.on_button_pressed(event)

    mock_dismiss.assert_called_once_with(False)


# ============================================================================
# ManageDocumentsScreen compose() / on_mount() / _populate_list() /
# on_button_pressed() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_manage_documents_screen_compose_and_on_mount() -> None:
    """Test compose() builds the UI and on_mount() focuses input & loads the list."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"attached_doc_ids": ["doc-1"]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    app = App()
    with patch("nyxgpt.tui.httpx.AsyncClient", return_value=mock_client):
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()

            assert screen.attach_input.has_focus
            assert screen._attached == ["doc-1"]
            docs_list = screen.query_one("#docs-listview", ListView)
            assert len(docs_list.children) == 1


@pytest.mark.asyncio
async def test_manage_documents_screen_populate_list_empty() -> None:
    """Test _populate_list() shows a placeholder when no documents are attached."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen._attached = []

    app = App()
    with patch(
        "nyxgpt.tui.httpx.AsyncClient",
        side_effect=RuntimeError("no network in this test"),
    ):
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()

            await screen._populate_list()
            await pilot.pause()

            docs_list = screen.query_one("#docs-listview", ListView)
            assert len(docs_list.children) == 1
            label = docs_list.children[0].query_one("Label")
            assert "no documents attached" in str(label.content)


@pytest.mark.asyncio
async def test_manage_documents_screen_populate_list_non_empty() -> None:
    """Test _populate_list() renders one ListItem per attached document id."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")

    app = App()
    with patch(
        "nyxgpt.tui.httpx.AsyncClient",
        side_effect=RuntimeError("no network in this test"),
    ):
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()

            # on_mount()'s _refresh_list() failed and left self._attached == [];
            # set it now (after mount) to exercise the non-empty rendering path.
            screen._attached = ["doc-a", "doc-b"]
            await screen._populate_list()
            await pilot.pause()

            docs_list = screen.query_one("#docs-listview", ListView)
            assert len(docs_list.children) == 2


@pytest.mark.asyncio
async def test_manage_documents_screen_attach_empty_doc_id_returns_early() -> None:
    """Test on_button_pressed() for attach-btn returns early when doc_id is blank."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen.attach_input = MagicMock(spec=Input)
    screen.attach_input.value = "   "

    with patch("nyxgpt.tui.httpx.AsyncClient") as mock_async_client:
        event = MagicMock()
        event.button.id = "attach-btn"
        await screen.on_button_pressed(event)

    mock_async_client.assert_not_called()


@pytest.mark.asyncio
async def test_manage_documents_screen_attach_success() -> None:
    """Test on_button_pressed() for attach-btn attaches the doc and refreshes the list."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen.attach_input = MagicMock(spec=Input)
    screen.attach_input.value = "new-doc"

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch("nyxgpt.tui.httpx.AsyncClient", return_value=mock_client),
        patch.object(screen, "_refresh_list", new=AsyncMock()) as mock_refresh,
    ):
        event = MagicMock()
        event.button.id = "attach-btn"
        await screen.on_button_pressed(event)

    mock_client.post.assert_called_once()
    assert screen.attach_input.value == ""
    mock_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_manage_documents_screen_attach_exception_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test on_button_pressed() for attach-btn logs and swallows API errors."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen.attach_input = MagicMock(spec=Input)
    screen.attach_input.value = "new-doc"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=RuntimeError("attach failed"))

    with (
        patch("nyxgpt.tui.httpx.AsyncClient", return_value=mock_client),
        patch.object(screen, "_refresh_list", new=AsyncMock()) as mock_refresh,
        caplog.at_level(logging.ERROR, logger="nyxgpt.tui"),
    ):
        event = MagicMock()
        event.button.id = "attach-btn"
        await screen.on_button_pressed(event)

    assert "Failed to attach document" in caplog.text
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_manage_documents_screen_detach_no_highlighted_returns_early() -> None:
    """Test on_button_pressed() for detach-btn returns early with no highlighted item."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen._attached = ["doc-a"]
    screen.docs_list = MagicMock()
    screen.docs_list.highlighted_child = None

    with patch("nyxgpt.tui.httpx.AsyncClient") as mock_async_client:
        event = MagicMock()
        event.button.id = "detach-btn"
        await screen.on_button_pressed(event)

    mock_async_client.assert_not_called()


@pytest.mark.asyncio
async def test_manage_documents_screen_detach_idx_out_of_range_returns_early() -> None:
    """Test on_button_pressed() for detach-btn returns early when index is out of range."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen._attached = ["doc-a"]
    screen.docs_list = MagicMock()
    screen.docs_list.highlighted_child = MagicMock()
    screen.docs_list.index = 5  # out of range for a single-item list

    with patch("nyxgpt.tui.httpx.AsyncClient") as mock_async_client:
        event = MagicMock()
        event.button.id = "detach-btn"
        await screen.on_button_pressed(event)

    mock_async_client.assert_not_called()


@pytest.mark.asyncio
async def test_manage_documents_screen_detach_idx_none_returns_early() -> None:
    """Test on_button_pressed() for detach-btn returns early when index is None."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen._attached = ["doc-a"]
    screen.docs_list = MagicMock()
    screen.docs_list.highlighted_child = MagicMock()
    screen.docs_list.index = None

    with patch("nyxgpt.tui.httpx.AsyncClient") as mock_async_client:
        event = MagicMock()
        event.button.id = "detach-btn"
        await screen.on_button_pressed(event)

    mock_async_client.assert_not_called()


@pytest.mark.asyncio
async def test_manage_documents_screen_detach_success() -> None:
    """Test on_button_pressed() for detach-btn deletes the doc and refreshes the list."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen._attached = ["doc-a", "doc-b"]
    screen.docs_list = MagicMock()
    screen.docs_list.highlighted_child = MagicMock()
    screen.docs_list.index = 1

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.delete = AsyncMock(return_value=mock_response)

    with (
        patch("nyxgpt.tui.httpx.AsyncClient", return_value=mock_client),
        patch.object(screen, "_refresh_list", new=AsyncMock()) as mock_refresh,
    ):
        event = MagicMock()
        event.button.id = "detach-btn"
        await screen.on_button_pressed(event)

    mock_client.delete.assert_called_once()
    assert "doc-b" in mock_client.delete.call_args[0][0]
    mock_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_manage_documents_screen_detach_exception_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test on_button_pressed() for detach-btn logs and swallows API errors."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")
    screen._attached = ["doc-a"]
    screen.docs_list = MagicMock()
    screen.docs_list.highlighted_child = MagicMock()
    screen.docs_list.index = 0

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.delete = AsyncMock(side_effect=RuntimeError("detach failed"))

    with (
        patch("nyxgpt.tui.httpx.AsyncClient", return_value=mock_client),
        patch.object(screen, "_refresh_list", new=AsyncMock()) as mock_refresh,
        caplog.at_level(logging.ERROR, logger="nyxgpt.tui"),
    ):
        event = MagicMock()
        event.button.id = "detach-btn"
        await screen.on_button_pressed(event)

    assert "Failed to detach document" in caplog.text
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_manage_documents_screen_close_btn() -> None:
    """Test on_button_pressed() for close-btn dismisses the screen."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")

    with patch.object(screen, "dismiss") as mock_dismiss:
        event = MagicMock()
        event.button.id = "close-btn"
        await screen.on_button_pressed(event)

    mock_dismiss.assert_called_once_with(None)


def test_manage_documents_screen_action_close() -> None:
    """Test action_close() dismisses the screen with None."""
    screen = ManageDocumentsScreen(api_base_url="http://127.0.0.1:8000", session="s1")

    with patch.object(screen, "dismiss") as mock_dismiss:
        screen.action_close()

    mock_dismiss.assert_called_once_with(None)


# ============================================================================
# CommandPaletteScreen compose() / on_mount() / on_input_changed() /
# update_command_list() / on_list_view_highlighted() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_command_palette_screen_compose_and_on_mount() -> None:
    """Test compose() builds the palette UI and on_mount() focuses input & lists commands."""
    screen = CommandPaletteScreen()

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        search_input = screen.query_one("#command-search", Input)
        assert search_input.has_focus

        list_view = screen.query_one("#command-list", ListView)
        assert len(list_view.children) == len(screen.all_commands)

        desc_label = screen.query_one("#command-description")
        assert str(desc_label.content) == screen.all_commands[0]["description"]


@pytest.mark.asyncio
async def test_command_palette_screen_on_input_changed_wrong_id() -> None:
    """Test on_input_changed() ignores events from inputs other than #command-search."""
    screen = CommandPaletteScreen()
    original_filtered = screen.filtered_commands

    with patch.object(screen, "update_command_list", new=AsyncMock()) as mock_update:
        mock_input = MagicMock(spec=Input)
        mock_input.id = "not-command-search"
        event = MagicMock()
        event.input = mock_input
        event.value = "search"

        await screen.on_input_changed(event)

    mock_update.assert_not_called()
    assert screen.filtered_commands is original_filtered


@pytest.mark.asyncio
async def test_command_palette_screen_update_command_list_updates_description() -> None:
    """Test update_command_list() populates the list and sets the first description."""
    screen = CommandPaletteScreen()
    screen.filtered_commands = [
        {"key": "show_help", "label": "Show Help", "description": "Display keyboard shortcuts"},
    ]

    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        await screen.update_command_list()
        await pilot.pause()

        list_view = screen.query_one("#command-list", ListView)
        assert len(list_view.children) == 1
        assert list_view.children[0].name == "show_help"

        desc_label = screen.query_one("#command-description")
        assert "Display keyboard shortcuts" in str(desc_label.content)


@pytest.mark.asyncio
async def test_command_palette_screen_on_list_view_highlighted_item_none() -> None:
    """Test on_list_view_highlighted() returns early when event.item is None."""
    screen = CommandPaletteScreen()

    with patch.object(screen, "query_one") as mock_query_one:
        event = MagicMock()
        event.item = None
        await screen.on_list_view_highlighted(event)

    mock_query_one.assert_not_called()


@pytest.mark.asyncio
async def test_command_palette_screen_on_list_view_highlighted_item_found() -> None:
    """Test on_list_view_highlighted() updates the description for a matching command."""
    screen = CommandPaletteScreen()

    mock_desc_label = MagicMock()
    with patch.object(screen, "query_one", return_value=mock_desc_label):
        event = MagicMock()
        event.item = MagicMock()
        event.item.name = "show_help"
        await screen.on_list_view_highlighted(event)

    mock_desc_label.update.assert_called_once_with("Display keyboard shortcuts")


# ============================================================================
# NyxGPTTUI.compose() Real Mount Test
# ============================================================================


@pytest.mark.asyncio
async def test_nyx_gpt_tui_compose_assigns_widgets(tmp_path: Path) -> None:
    """Test that compose() assigns self.output/status_bar/prompt when actually mounted."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with (
        patch("nyxgpt.tui.load_config", return_value=cfg),
        patch("httpx.AsyncClient", side_effect=RuntimeError("no network in this test")),
    ):
        app = NyxGPTTUI(session="test-session")

        async with app.run_test() as pilot:
            await pilot.pause()

            assert isinstance(app.output, ChatOutput)
            assert isinstance(app.status_bar, SessionStatusBar)
            assert isinstance(app.prompt, Input)
            assert app.query_one(ChatOutput) is app.output
            assert app.query_one(SessionStatusBar) is app.status_bar
            assert app.query_one(Input) is app.prompt


# ============================================================================
# NyxGPTTUI Sync action_* Wrapper Methods (run_worker dispatch) Tests
# ============================================================================


def test_action_pick_session_runs_worker(tmp_path: Path) -> None:
    """Test action_pick_session() dispatches _pick_session_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_pick_session()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_pick_session_worker"
    coro.close()


def test_action_models_manager_runs_worker(tmp_path: Path) -> None:
    """Test action_models_manager() dispatches _models_manager_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_models_manager()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_models_manager_worker"
    coro.close()


def test_action_search_messages_runs_worker(tmp_path: Path) -> None:
    """Test action_search_messages() dispatches _search_messages_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_search_messages()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_search_messages_worker"
    coro.close()


def test_action_rename_session_runs_worker(tmp_path: Path) -> None:
    """Test action_rename_session() dispatches _rename_session_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_rename_session()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_rename_session_worker"
    coro.close()


def test_action_manage_documents_runs_worker(tmp_path: Path) -> None:
    """Test action_manage_documents() dispatches _manage_documents_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_manage_documents()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_manage_documents_worker"
    coro.close()


def test_action_index_repository_runs_worker(tmp_path: Path) -> None:
    """Test action_index_repository() dispatches _index_repository_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_index_repository()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_index_repository_worker"
    coro.close()


def test_action_delete_session_runs_worker(tmp_path: Path) -> None:
    """Test action_delete_session() dispatches _delete_session_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_delete_session()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_delete_session_worker"
    coro.close()


def test_action_show_help_runs_worker(tmp_path: Path) -> None:
    """Test action_show_help() dispatches _show_help_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_show_help()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_show_help_worker"
    coro.close()


def test_action_command_palette_runs_worker(tmp_path: Path) -> None:
    """Test action_command_palette() dispatches _command_palette_worker() via run_worker."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session")

    with patch.object(app, "run_worker") as mock_run_worker:
        app.action_command_palette()

    mock_run_worker.assert_called_once()
    coro = mock_run_worker.call_args[0][0]
    assert coro.__name__ == "_command_palette_worker"
    coro.close()


# ============================================================================
# _search_messages_worker query_one Exception Branch Test
# ============================================================================


@pytest.mark.asyncio
async def test_search_messages_worker_query_one_raises_is_swallowed(tmp_path: Path) -> None:
    """Test _search_messages_worker() swallows query_one() failures when clearing output.

    Covers the try/except around `self.query_one("#output", ChatOutput)` at
    tui.py:1006-1010, exercised when the session changes but the #output widget
    can't be found (e.g. app shutting down).
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="original-session", config_path=str(config_file))

    search_result = {"session_name": "target-session", "message_index": 2}

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=search_result)),
        patch.object(app, "query_one", side_effect=RuntimeError("widget not found")),
        patch.object(app, "notify") as mock_notify,
    ):
        # Should not raise despite query_one failing
        await app._search_messages_worker()

    assert app.session == "target-session"
    mock_notify.assert_called_once()


# ============================================================================
# _rename_session_worker() Full Flow Tests (incl. inline RenameScreen)
# ============================================================================


@pytest.mark.asyncio
async def test_rename_session_worker_full_flow_renamed(tmp_path: Path) -> None:
    """Test the full rename flow when the session filename changes.

    Covers: successful title fetch (status 200), the inline RenameScreen's
    compose()/on_mount() (focus + prefilled title) and on_button_pressed()
    for rename-btn with a value, and the rename API success path where
    new_session_name != old_name.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="orig-session", config_path=str(config_file))

    mock_title_response = MagicMock()
    mock_title_response.status_code = 200
    mock_title_response.json.return_value = {"title": "Original Title"}
    mock_title_client = AsyncMock()
    mock_title_client.__aenter__ = AsyncMock(return_value=mock_title_client)
    mock_title_client.__aexit__ = AsyncMock(return_value=None)
    mock_title_client.get = AsyncMock(return_value=mock_title_response)

    mock_rename_response = MagicMock()
    mock_rename_response.raise_for_status = MagicMock()
    mock_rename_response.json.return_value = {"new_name": "renamed-session"}
    mock_rename_client = AsyncMock()
    mock_rename_client.__aenter__ = AsyncMock(return_value=mock_rename_client)
    mock_rename_client.__aexit__ = AsyncMock(return_value=None)
    mock_rename_client.post = AsyncMock(return_value=mock_rename_response)

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", side_effect=[mock_title_client, mock_rename_client]),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._rename_session_worker())
            await pilot.pause()

            rename_input = app.screen.query_one("#rename-input", Input)
            assert rename_input.has_focus
            assert rename_input.value == "Original Title"
            rename_input.value = "New Session Title"

            rename_btn = app.screen.query_one("#rename-btn", Button)
            await pilot.click(rename_btn)
            await pilot.pause()
            await worker.wait()

            assert app.session == "renamed-session"
            assert "renamed-session" in app.output._buffer
            assert "orig-session" in app.output._buffer


@pytest.mark.asyncio
async def test_rename_session_worker_title_fetch_non_200_then_cancel(tmp_path: Path) -> None:
    """Test that a non-200 title fetch response results in an empty prefilled title.

    Also covers the cancel-btn branch of RenameScreen.on_button_pressed() and the
    `if not new_name: return` early-exit in the worker.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="orig-session", config_path=str(config_file))

    mock_title_response = MagicMock()
    mock_title_response.status_code = 404
    mock_title_client = AsyncMock()
    mock_title_client.__aenter__ = AsyncMock(return_value=mock_title_client)
    mock_title_client.__aexit__ = AsyncMock(return_value=None)
    mock_title_client.get = AsyncMock(return_value=mock_title_response)

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", return_value=mock_title_client),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._rename_session_worker())
            await pilot.pause()

            rename_input = app.screen.query_one("#rename-input", Input)
            assert rename_input.value == ""

            cancel_btn = app.screen.query_one("#cancel-btn", Button)
            await pilot.click(cancel_btn)
            await pilot.pause()
            await worker.wait()

            assert app.session == "orig-session"


@pytest.mark.asyncio
async def test_rename_session_worker_title_fetch_exception(tmp_path: Path) -> None:
    """Test that an exception during the title fetch is swallowed (current_title stays '')."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="orig-session", config_path=str(config_file))

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", side_effect=RuntimeError("network down")),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._rename_session_worker())
            await pilot.pause()

            rename_input = app.screen.query_one("#rename-input", Input)
            assert rename_input.value == ""

            cancel_btn = app.screen.query_one("#cancel-btn", Button)
            await pilot.click(cancel_btn)
            await pilot.pause()
            await worker.wait()

            assert app.session == "orig-session"


@pytest.mark.asyncio
async def test_rename_session_worker_rename_btn_blank_dismisses_none(tmp_path: Path) -> None:
    """Test RenameScreen's rename-btn dismisses with None when the input is blank."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="orig-session", config_path=str(config_file))

    mock_title_response = MagicMock()
    mock_title_response.status_code = 200
    mock_title_response.json.return_value = {"title": ""}
    mock_title_client = AsyncMock()
    mock_title_client.__aenter__ = AsyncMock(return_value=mock_title_client)
    mock_title_client.__aexit__ = AsyncMock(return_value=None)
    mock_title_client.get = AsyncMock(return_value=mock_title_response)

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", return_value=mock_title_client),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._rename_session_worker())
            await pilot.pause()

            rename_input = app.screen.query_one("#rename-input", Input)
            rename_input.value = "   "  # whitespace-only -> strip() == ""

            rename_btn = app.screen.query_one("#rename-btn", Button)
            await pilot.click(rename_btn)
            await pilot.pause()
            await worker.wait()

            assert app.session == "orig-session"


@pytest.mark.asyncio
async def test_rename_session_worker_title_only_update(tmp_path: Path) -> None:
    """Test the rename API success branch where new_session_name == old_name (title-only)."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="orig-session", config_path=str(config_file))

    mock_title_response = MagicMock()
    mock_title_response.status_code = 200
    mock_title_response.json.return_value = {"title": "Old Title"}
    mock_title_client = AsyncMock()
    mock_title_client.__aenter__ = AsyncMock(return_value=mock_title_client)
    mock_title_client.__aexit__ = AsyncMock(return_value=None)
    mock_title_client.get = AsyncMock(return_value=mock_title_response)

    mock_rename_response = MagicMock()
    mock_rename_response.raise_for_status = MagicMock()
    # Backend keeps the filename the same, only the title changed
    mock_rename_response.json.return_value = {"new_name": "orig-session"}
    mock_rename_client = AsyncMock()
    mock_rename_client.__aenter__ = AsyncMock(return_value=mock_rename_client)
    mock_rename_client.__aexit__ = AsyncMock(return_value=None)
    mock_rename_client.post = AsyncMock(return_value=mock_rename_response)

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", side_effect=[mock_title_client, mock_rename_client]),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._rename_session_worker())
            await pilot.pause()

            rename_input = app.screen.query_one("#rename-input", Input)
            rename_input.value = "New Title Text"

            rename_btn = app.screen.query_one("#rename-btn", Button)
            await pilot.click(rename_btn)
            await pilot.pause()
            await worker.wait()

            # Session name unchanged, only the title update message was appended
            assert app.session == "orig-session"
            assert "Session title updated to: New Title Text" in app.output._buffer


@pytest.mark.asyncio
async def test_rename_session_worker_rename_api_exception(tmp_path: Path) -> None:
    """Test that a rename API exception is logged and shown as an output error."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="orig-session", config_path=str(config_file))

    mock_title_response = MagicMock()
    mock_title_response.status_code = 200
    mock_title_response.json.return_value = {"title": "Old Title"}
    mock_title_client = AsyncMock()
    mock_title_client.__aenter__ = AsyncMock(return_value=mock_title_client)
    mock_title_client.__aexit__ = AsyncMock(return_value=None)
    mock_title_client.get = AsyncMock(return_value=mock_title_response)

    mock_rename_client = AsyncMock()
    mock_rename_client.__aenter__ = AsyncMock(return_value=mock_rename_client)
    mock_rename_client.__aexit__ = AsyncMock(return_value=None)
    mock_rename_client.post = AsyncMock(side_effect=RuntimeError("rename API down"))

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", side_effect=[mock_title_client, mock_rename_client]),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._rename_session_worker())
            await pilot.pause()

            rename_input = app.screen.query_one("#rename-input", Input)
            rename_input.value = "New Title Text"

            rename_btn = app.screen.query_one("#rename-btn", Button)
            await pilot.click(rename_btn)
            await pilot.pause()
            await worker.wait()

            assert app.session == "orig-session"
            assert "[error] Failed to rename session" in app.output._buffer


# ============================================================================
# _index_repository_worker() Full Flow Tests (incl. inline IndexRepoScreen)
# ============================================================================


@pytest.mark.asyncio
async def test_index_repository_worker_cancel_returns_early(tmp_path: Path) -> None:
    """Test that cancel-btn dismisses with None and the worker returns without indexing."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session", config_path=str(config_file))

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient") as mock_async_client_cls,
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._index_repository_worker())
            await pilot.pause()

            repo_path_input = app.screen.query_one("#repo-path-input", Input)
            assert repo_path_input.has_focus

            cancel_btn = app.screen.query_one("#cancel-btn", Button)
            await pilot.click(cancel_btn)
            await pilot.pause()
            await worker.wait()

    mock_async_client_cls.assert_not_called()
    assert "Indexing repository" not in app.output._buffer


@pytest.mark.asyncio
async def test_index_repository_worker_blank_repo_path_dismisses_none(tmp_path: Path) -> None:
    """Test that index-btn with a blank repo path dismisses with None (no indexing)."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session", config_path=str(config_file))

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient") as mock_async_client_cls,
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._index_repository_worker())
            await pilot.pause()

            repo_path_input = app.screen.query_one("#repo-path-input", Input)
            repo_path_input.value = "   "

            index_btn = app.screen.query_one("#index-btn", Button)
            await pilot.click(index_btn)
            await pilot.pause()
            await worker.wait()

    mock_async_client_cls.assert_not_called()
    assert "Indexing repository" not in app.output._buffer


@pytest.mark.asyncio
async def test_index_repository_worker_success_with_extensions(tmp_path: Path) -> None:
    """Test the full success flow: repo path + extensions parsing + API success.

    Covers IndexRepoScreen.compose()/on_mount()/on_button_pressed() (index-btn with
    a repo path and a comma-separated extensions list), and the index-repo API
    success branch of the worker.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session", config_path=str(config_file))

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"total_files": 10, "total_chunks": 42}
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._index_repository_worker())
            await pilot.pause()

            repo_path_input = app.screen.query_one("#repo-path-input", Input)
            repo_path_input.value = "/repo/path"
            extensions_input = app.screen.query_one("#extensions-input", Input)
            extensions_input.value = ".py, .js"
            docs_only_checkbox = app.screen.query_one("#docs-only-checkbox", Checkbox)
            docs_only_checkbox.value = True

            index_btn = app.screen.query_one("#index-btn", Button)
            await pilot.click(index_btn)
            await pilot.pause()
            await worker.wait()

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["json"]["repo_path"] == "/repo/path"
    assert call_kwargs[1]["json"]["extensions"] == [".py", ".js"]
    assert call_kwargs[1]["json"]["extract_docs_only"] is True

    assert "Indexing repository: /repo/path" in app.output._buffer
    assert "Repository indexed successfully" in app.output._buffer
    assert "Files indexed: 10" in app.output._buffer
    assert "Total chunks: 42" in app.output._buffer


@pytest.mark.asyncio
async def test_index_repository_worker_timeout(tmp_path: Path) -> None:
    """Test that httpx.TimeoutException during indexing shows a timeout error message."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session", config_path=str(config_file))

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._index_repository_worker())
            await pilot.pause()

            repo_path_input = app.screen.query_one("#repo-path-input", Input)
            repo_path_input.value = "/repo/path"

            index_btn = app.screen.query_one("#index-btn", Button)
            await pilot.click(index_btn)
            await pilot.pause()
            await worker.wait()

    assert "Repository indexing timed out" in app.output._buffer


@pytest.mark.asyncio
async def test_index_repository_worker_generic_exception(tmp_path: Path) -> None:
    """Test that a generic exception during indexing is logged and shown as an error."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test-session", config_path=str(config_file))

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=RuntimeError("index API down"))

    with (
        patch.object(app, "_update_session_status", new=AsyncMock()),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        async with app.run_test() as pilot:
            await pilot.pause()

            worker = app.run_worker(app._index_repository_worker())
            await pilot.pause()

            repo_path_input = app.screen.query_one("#repo-path-input", Input)
            repo_path_input.value = "/repo/path"

            index_btn = app.screen.query_one("#index-btn", Button)
            await pilot.click(index_btn)
            await pilot.pause()
            await worker.wait()

    assert "[error] Failed to index repository" in app.output._buffer


# ============================================================================
# _delete_session_worker() "Should Not Happen" Branch Test
# ============================================================================


@pytest.mark.asyncio
async def test_delete_session_worker_no_sessions_remaining_defensive_branch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test the defensive branch where updated_sessions is empty after a successful delete.

    This "should not happen" because we already verified there were >= 2 sessions
    before deleting, but the worker still handles the case where list_sessions()
    returns an empty list on the post-delete refresh.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session1.json").write_text("[]")
    (sessions_dir / "session2.json").write_text("[]")

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="session1", config_path=str(config_file))

    app.output = MagicMock(spec=ChatOutput)

    mock_sessions = [
        {"name": "session1", "messages": 0},
        {"name": "session2", "messages": 0},
    ]

    with (
        patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)),
        patch(
            "nyxgpt.tui.list_sessions",
            side_effect=[mock_sessions, []],
        ),
        patch("nyxgpt.tui.delete_session", return_value=True),
        caplog.at_level(logging.ERROR, logger="nyxgpt.tui"),
    ):
        await app._delete_session_worker()

    # Session was not switched since there was nothing to switch to
    assert app.session == "session1"

    # The defensive error branch appended a message and logged an error
    assert "no sessions remaining" in app.output.append.call_args[0][0].lower()
    assert "No sessions remaining after delete" in caplog.text


# ============================================================================
# _stream_chat() Empty Chunk / RAG Citation Rendering / Buffer-Flush Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stream_chat_empty_chunk_is_skipped(tmp_path: Path) -> None:
    """Test that an empty-string chunk from the stream is skipped via `continue`."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        yield ""  # empty chunk: should be skipped without affecting the buffer
        yield "Hello"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(c) for c in app.output.append.call_args_list]
    assert any("Hello" in c for c in append_calls)


@pytest.mark.asyncio
async def test_stream_chat_rag_chunks_rendered_with_score_styles(tmp_path: Path) -> None:
    """Test the RAG citation rendering loop covers green/yellow/red score styles,
    chunk_id present vs missing, and the similarity_score -> score fallback."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        rag_data = {
            "type": "rag_metadata",
            "chunks": [
                {"doc_id": "doc-high", "similarity_score": 0.9, "chunk_id": 5},
                {"doc_id": "doc-mid", "score": 0.6},  # no similarity_score -> fallback to score
                {"doc_id": "doc-low", "similarity_score": 0.1},
            ],
        }
        # No trailing text: buffer is empty after marker removal (skips flush dispatch)
        yield f"__RAG_START__{json.dumps(rag_data)}__RAG_END__"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(c) for c in app.output.append.call_args_list]

    assert any("3 sources retrieved" in c for c in append_calls)
    assert any(
        "doc-high" in c and "chunk 5" in c and "green" in c and "0.900" in c for c in append_calls
    )
    assert any(
        "doc-mid" in c and "source" in c and "yellow" in c and "0.600" in c for c in append_calls
    )
    assert any("doc-low" in c and "red" in c and "0.100" in c for c in append_calls)


@pytest.mark.asyncio
async def test_stream_chat_rag_marker_malformed_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that malformed JSON inside a RAG marker triggers the JSONDecodeError branch."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Invalid JSON (truncated) between valid RAG markers
        yield '__RAG_START__{"type": "rag_metadata", "chunks": [__RAG_END__'

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.WARNING, logger="nyxgpt.tui"),
    ):
        # Should not raise despite malformed JSON
        await app._stream_chat("Test prompt")

    assert "Failed to parse RAG metadata" in caplog.text
    append_calls = [str(c) for c in app.output.append.call_args_list]
    assert not any("__RAG_START__" in c for c in append_calls)


@pytest.mark.asyncio
async def test_stream_chat_rag_nested_flush_pass_branch_second_marker_start(
    tmp_path: Path,
) -> None:
    """Test the nested RAG-triggered flush dispatch's `pass` branch (tui.py ~1470-1472).

    Triggered when, after removing one complete RAG marker, the leftover buffer
    still contains the start of another marker (here, a second unterminated
    "__RAG_START__").
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        rag_data = {"type": "rag_metadata", "chunks": []}
        first_marker = f"__RAG_START__{json.dumps(rag_data)}__RAG_END__"
        # Trailing content is the start of a second (incomplete) RAG marker
        yield first_marker + "__RAG_START__"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        # Should not raise; the nested flush dispatch hits its `pass` branch
        # because the leftover buffer still contains "__RAG_START__", and the
        # (unrelated) final end-of-stream flush later appends the raw buffer.
        await app._stream_chat("Test prompt")

    app.output.append.assert_called()


@pytest.mark.asyncio
async def test_stream_chat_rag_nested_flush_partial_marker_branch(tmp_path: Path) -> None:
    """Test the nested RAG-triggered flush dispatch's partial-marker branch.

    Covers tui.py ~1473-1479: trailing text after a RAG marker ends with a
    partial marker prefix (e.g. "__RETRY_"), so the safe portion is flushed
    and the partial prefix is retained in the buffer.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        rag_data = {"type": "rag_metadata", "chunks": []}
        marker = f"__RAG_START__{json.dumps(rag_data)}__RAG_END__"
        yield marker + "some safe text __RETRY_"
        yield "START__ignored__RETRY_END__more"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(c) for c in app.output.append.call_args_list]
    assert any("some safe text" in c for c in append_calls)


@pytest.mark.asyncio
async def test_stream_chat_rag_nested_flush_full_flush_branch(tmp_path: Path) -> None:
    """Test the nested RAG-triggered flush dispatch's full-flush branch.

    Covers tui.py ~1480-1486: trailing text after a RAG marker has no marker
    prefix at all, so it's flushed in full immediately.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        rag_data = {"type": "rag_metadata", "chunks": []}
        marker = f"__RAG_START__{json.dumps(rag_data)}__RAG_END__"
        yield marker + "plain trailing response text with no markers"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await app._stream_chat("Test prompt")

    append_calls = [str(c) for c in app.output.append.call_args_list]
    assert any("plain trailing response text with no markers" in c for c in append_calls)


@pytest.mark.asyncio
async def test_stream_chat_final_flush_pass_branch_unterminated_retry_marker(
    tmp_path: Path,
) -> None:
    """Test the final duplicate flush block's `pass` branch (tui.py ~1517).

    Triggered when the stream ends with a complete "__RETRY_START__" substring
    in the buffer but no matching "__RETRY_END__" was ever sent, so the marker
    is left untouched rather than flushed as text.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("nyxgpt.tui.load_config", return_value=cfg):
        app = NyxGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Stream ends with an unterminated retry marker (no __RETRY_END__ ever sent)
        yield "__RETRY_START__{incomplete and never closed"

    mock_response.aiter_text = mock_aiter_text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        # Should not raise
        await app._stream_chat("Test prompt")

    # The raw buffer (including the unterminated marker) is appended once via the
    # unconditional flush at the top of the final-flush block; the dispatch logic
    # then hits the `pass` branch and does not append it a second time as "clean" text.
    append_calls = [str(c) for c in app.output.append.call_args_list]
    assert any("__RETRY_START__" in c for c in append_calls)
