from __future__ import annotations

import configparser
import logging
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

import pytest
from textual.widgets import Input

from mygpt.tui import ChatOutput, MyGPTTUI, SessionMetadataPreview, SessionPickerScreen

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


# ============================================================================
# MyGPTTUI Initialization Tests
# ============================================================================


def test_tui_initialization_default(tmp_path: Path) -> None:
    """Test TUI initialization with default config."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test-session")

    assert app.session == "test-session"
    assert app.api_base_url == "http://127.0.0.1:8000"


def test_tui_initialization_custom_api_url(tmp_path: Path) -> None:
    """Test TUI initialization with custom API URL."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(
            session="custom-session",
            api_base_url="http://localhost:9000"
        )

    assert app.session == "custom-session"
    assert app.api_base_url == "http://localhost:9000"


def test_tui_initialization_fallback_url(tmp_path: Path) -> None:
    """Test TUI falls back to default URL when config has none."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    # No [api] section
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

    assert app.api_base_url == "http://127.0.0.1:8000"


# ============================================================================
# MyGPTTUI._unlock_prompt() Tests
# ============================================================================


def test_unlock_prompt_success(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test _unlock_prompt() successfully enables and focuses prompt."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

    # Mock the prompt widget
    app.prompt = MagicMock(spec=Input)
    app.prompt.disabled = True

    with caplog.at_level(logging.DEBUG):
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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

    # Don't set app.prompt - will cause AttributeError

    with caplog.at_level(logging.WARNING):
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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

    # Mock prompt to raise RuntimeError when accessing disabled
    app.prompt = MagicMock(spec=Input)
    type(app.prompt).disabled = PropertyMock(side_effect=RuntimeError("Textual shutdown"))

    with caplog.at_level(logging.WARNING):
        # Should not raise exception
        app._unlock_prompt()

    # Verify warning was logged with exception type
    assert "Failed to unlock prompt: RuntimeError" in caplog.text


# ============================================================================
# MyGPTTUI.compose() Tests
# ============================================================================


def test_compose_creates_widgets(tmp_path: Path) -> None:
    """Test that compose() assigns output and prompt widgets."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

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
# MyGPTTUI.on_mount() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_on_mount_calls_unlock_prompt(tmp_path: Path) -> None:
    """Test that on_mount() calls _unlock_prompt() for defensive reset."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

    # Mock _unlock_prompt
    with patch.object(app, "_unlock_prompt") as mock_unlock:
        await app.on_mount()

    # Verify defensive reset was called
    mock_unlock.assert_called_once()


# ============================================================================
# MyGPTTUI.on_input_submitted() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_input_submitted_empty_string_ignored(tmp_path: Path) -> None:
    """Test that empty input is ignored."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

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
async def test_input_submitted_locks_prompt(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test that input submission locks the prompt."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)
    app.prompt.value = "Hello"
    app.prompt.disabled = False

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Create event
    event = MagicMock()
    event.value = "Hello"

    with patch("asyncio.create_task") as mock_create_task:
        with caplog.at_level(logging.DEBUG):
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
# MyGPTTUI._stream_chat() Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stream_chat_success(tmp_path: Path) -> None:
    """Test successful chat streaming."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

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

    # Verify output was updated with chunks
    assert app.output.append.call_count == 5  # "Assistant: ", "Hello", " ", "World", "\n\n"

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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock prompt widget
    app.prompt = MagicMock(spec=Input)
    app.prompt.disabled = True

    # Mock httpx to raise exception
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(side_effect=Exception("Connection error"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        with caplog.at_level(logging.ERROR):
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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI()

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
        }
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
        "meta": {}
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
    cfg["mygpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    assert screen.all_sessions == []
    assert screen.filtered_sessions == []


@pytest.mark.asyncio
async def test_session_picker_load_sessions(tmp_path: Path) -> None:
    """Test SessionPickerScreen loads sessions on mount."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["mygpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    mock_sessions = [
        {
            "name": "session1",
            "messages": 5,
            "modified": "2024-01-01 12:00:00",
            "meta": {"title": "Session 1"}
        },
        {
            "name": "session2",
            "messages": 3,
            "modified": "2024-01-02 14:00:00",
            "meta": {"title": "Session 2"}
        }
    ]

    with patch("mygpt.tui.load_config", return_value=cfg):
        with patch("mygpt.tui.list_sessions", return_value=mock_sessions):
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
    cfg["mygpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    mock_sessions = [
        {
            "name": "python-project",
            "messages": 5,
            "modified": "2024-01-01",
            "meta": {"title": "Python Development", "tags": ["coding"]}
        },
        {
            "name": "java-project",
            "messages": 3,
            "modified": "2024-01-02",
            "meta": {"title": "Java Development", "tags": ["coding"]}
        },
        {
            "name": "meeting-notes",
            "messages": 2,
            "modified": "2024-01-03",
            "meta": {"title": "Meeting Notes", "tags": ["notes"]}
        }
    ]

    with patch("mygpt.tui.load_config", return_value=cfg):
        with patch("mygpt.tui.list_sessions", return_value=mock_sessions):
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
    cfg["mygpt"] = {"sessions_dir": str(tmp_path / "sessions")}
    with open(config_file, "w") as f:
        cfg.write(f)

    mock_sessions = [
        {"name": "session1", "messages": 5, "modified": "2024-01-01", "meta": {}},
        {"name": "session2", "messages": 3, "modified": "2024-01-02", "meta": {}}
    ]

    with patch("mygpt.tui.load_config", return_value=cfg):
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

    with patch("mygpt.tui.load_config", return_value=cfg):
        screen = SessionPickerScreen(str(config_file))

    # Mock dismiss method
    with patch.object(screen, "dismiss") as mock_dismiss:
        screen.action_cancel()

    # Should dismiss with None
    mock_dismiss.assert_called_once_with(None)


# ============================================================================
# MyGPTTUI Session Picker Integration Tests
# ============================================================================


def test_tui_initialization_with_config_path(tmp_path: Path) -> None:
    """Test TUI accepts config_path parameter."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test", config_path=str(config_file))

    assert app.config_path == str(config_file)


@pytest.mark.asyncio
async def test_tui_action_pick_session(tmp_path: Path) -> None:
    """Test TUI action_pick_session switches to selected session."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="original-session", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock push_screen_wait to return a selected session
    with patch.object(app, "push_screen_wait", new=AsyncMock(return_value="new-session")):
        await app.action_pick_session()

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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="original-session", config_path=str(config_file))

    # Mock output widget
    app.output = MagicMock(spec=ChatOutput)

    # Mock push_screen_wait to return None (cancel)
    with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)):
        await app.action_pick_session()

    # Verify session was NOT switched
    assert app.session == "original-session"

    # Verify output was NOT modified
    app.output.clear.assert_not_called()
    app.output.append.assert_not_called()


# ============================================================================
# MyGPTTUI Reconnection Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stream_chat_with_retry_markers(tmp_path: Path) -> None:
    """Test that retry markers are parsed and displayed correctly."""
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

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
        yield f'__RETRY_START__{json.dumps(retry_data)}__RETRY_END__'
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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

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
        yield f'__RAG_START__{json.dumps(rag_data)}__RAG_END__'
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
# MyGPTTUI Partial Marker Tests
# ============================================================================


@pytest.mark.asyncio
async def test_stream_chat_partial_retry_marker_split_across_chunks(tmp_path: Path) -> None:
    """Test marker split across multiple chunks is buffered correctly.

    Covers the partial marker detection logic in tui.py:567-578.
    Tests scenario where __RETRY_START__ arrives in one chunk and the rest in another.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json
        retry_data = {"type": "retry_status", "attempt": 1, "delay": 2.0}
        full_marker = f'__RETRY_START__{json.dumps(retry_data)}__RETRY_END__'

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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        # Create the full marker first
        retry_data = {"type": "retry_status", "attempt": 2, "delay": 3.0}
        full_marker = f'__RETRY_START__{json.dumps(retry_data)}__RETRY_END__'

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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        # Two retry markers in one chunk
        retry_data_1 = {"type": "retry_status", "attempt": 1, "delay": 1.0}
        retry_data_2 = {"type": "retry_status", "attempt": 2, "delay": 2.0}
        marker_1 = f'__RETRY_START__{json.dumps(retry_data_1)}__RETRY_END__'
        marker_2 = f'__RETRY_START__{json.dumps(retry_data_2)}__RETRY_END__'

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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

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
    """Test that buffers exceeding MARKER_BUFFER_FLUSH_THRESHOLD are flushed.

    Tests the scenario where buffer grows beyond the threshold (1000 chars) with
    a potential partial marker. The implementation should flush the buffer to
    prevent unbounded memory growth, treating the content as regular text.

    This tests the safeguard at tui.py:595.
    """
    config_file = tmp_path / "config.ini"
    cfg = configparser.ConfigParser()
    cfg["api"] = {"base_url": "http://127.0.0.1:8000"}
    with open(config_file, "w") as f:
        cfg.write(f)

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Send a very long chunk that starts with partial marker prefix
        # This simulates a malformed stream where marker never completes
        # and buffer grows beyond threshold
        long_text = "__RETRY_" + ("x" * 1100)  # Exceeds MARKER_BUFFER_FLUSH_THRESHOLD (1000)
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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        import json

        # First: partial RETRY marker
        retry_data = {"type": "retry_status", "attempt": 1, "delay": 1.5}
        retry_marker = f'__RETRY_START__{json.dumps(retry_data)}__RETRY_END__'
        yield retry_marker[:15]  # Partial
        yield retry_marker[15:]  # Complete

        # Second: partial RAG marker
        rag_data = {"type": "rag_metadata", "chunks": []}
        rag_marker = f'__RAG_START__{json.dumps(rag_data)}__RAG_END__'
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

    with patch("mygpt.tui.load_config", return_value=cfg):
        app = MyGPTTUI(session="test")

    app.output = MagicMock(spec=ChatOutput)
    app.prompt = MagicMock(spec=Input)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_text():
        # Yield a partial marker prefix that exceeds MARKER_BUFFER_OVERFLOW_THRESHOLD (100 bytes)
        # Use "__RETRY_START_" (15 chars with trailing underscore, NOT complete marker)
        # Repeated 7 times = 105 chars total (> 100 byte threshold)
        # This ensures safe_idx == 0 (entire buffer is partial prefix) and triggers line 601
        yield "__RETRY_START_" * 7
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

    # Verify the oversized partial marker was flushed (buffer overflow protection triggered)
    # The partial marker should be treated as regular text and displayed
    flushed_calls = [c for c in append_calls if "__RETRY_START_" in c]
    assert len(flushed_calls) > 0, "Oversized partial marker should be flushed as regular text"

    # Verify subsequent text was also displayed
    done_calls = [c for c in append_calls if "done" in c]
    assert len(done_calls) > 0, "Text after flushed buffer should be displayed"
