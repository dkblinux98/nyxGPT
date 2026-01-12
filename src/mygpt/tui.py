"""
Terminal UI (TUI) for myGPT.

This is an intentionally minimal Textual-based client that talks to the local
FastAPI backend. It focuses on correctness and streaming, not visual polish.
"""

from __future__ import annotations

import asyncio
import httpx
import logging
from typing import Optional

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Input, ListView, ListItem, Label
from textual.containers import Vertical, Container
from textual.binding import Binding
from textual.screen import Screen

from mygpt.config import load_config
from mygpt.sessions import list_sessions

log = logging.getLogger(__name__)

# Buffer size threshold for flushing partial marker detection buffers.
# This prevents unbounded memory growth when processing streaming responses
# that may contain partial or incomplete markers. The value is chosen to be
# large enough to hold typical markers (which are ~100-200 chars) while
# preventing memory issues from malformed streams.
MARKER_BUFFER_FLUSH_THRESHOLD = 1000

# Overflow threshold for when entire buffer is a potential partial marker.
# This catches the edge case where the buffer consists entirely of characters
# that match a marker prefix (e.g., all underscores) and prevents it from
# growing beyond the maximum possible marker length.
MARKER_BUFFER_OVERFLOW_THRESHOLD = 15


class ChatOutput(Static):
    """Widget to display assistant output incrementally."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buffer: str = ""

    def clear(self) -> None:
        self._buffer = ""
        self.update("")

    def append(self, text: str) -> None:
        # Keep our own buffer to avoid Textual version differences
        self._buffer += text
        self.update(self._buffer)


class SessionMetadataPreview(Static):
    """Widget to display session metadata preview."""

    def update_session(self, session: dict) -> None:
        """Update the preview with session metadata."""
        meta = session.get("meta", {})
        title = meta.get("title", session["name"])
        summary = meta.get("summary", "No summary available")
        messages_count = session.get("messages", 0)
        modified = session.get("modified", "Unknown")
        tags = meta.get("tags", [])
        pinned = "📌 " if meta.get("pinned") else ""

        content = f"""
{pinned}{title}

Modified: {modified}
Messages: {messages_count}
Tags: {', '.join(tags) if tags else 'None'}

{summary}
        """.strip()

        self.update(content)


class SessionPickerScreen(Screen):
    """Interactive session picker with search and keyboard navigation."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
        ("ctrl+c", "cancel", "Cancel"),
    ]

    def __init__(self, config_path: Optional[str] = None) -> None:
        super().__init__()
        self.config = load_config(config_path)
        self.all_sessions: list[dict] = []
        self.filtered_sessions: list[dict] = []

    def compose(self) -> ComposeResult:
        """Create the session picker UI."""
        yield Header()
        with Container():
            yield Label("Select a session:")
            yield Input(placeholder="Search sessions...", id="search")
            yield ListView(id="session-list")
            with Container(id="preview-container"):
                yield Label("Session Preview:")
                yield SessionMetadataPreview(id="session-preview")
        yield Footer()

    async def on_mount(self) -> None:
        """Load sessions when the screen is mounted."""
        await self.load_sessions()

    async def load_sessions(self) -> None:
        """Load all sessions and populate the list."""
        self.all_sessions = list_sessions(self.config)
        self.filtered_sessions = self.all_sessions
        await self.update_session_list()

    async def update_session_list(self) -> None:
        """Update the ListView with filtered sessions."""
        list_view = self.query_one("#session-list", ListView)
        await list_view.clear()

        for session in self.filtered_sessions:
            meta = session.get("meta", {})
            title = meta.get("title", session["name"])
            pinned = "📌 " if meta.get("pinned") else ""
            messages = session.get("messages", 0)

            label = f"{pinned}{title} ({messages} messages)"
            await list_view.append(ListItem(Label(label), name=session["name"]))

        # Update preview for first session if any
        if self.filtered_sessions:
            preview = self.query_one("#session-preview", SessionMetadataPreview)
            preview.update_session(self.filtered_sessions[0])

    async def on_input_changed(self, event: Input.Changed) -> None:
        """Filter sessions based on search input."""
        if event.input.id != "search":
            return

        query = event.value.lower()
        if not query:
            self.filtered_sessions = self.all_sessions
        else:
            # Search in name, title, summary, and tags
            self.filtered_sessions = [
                s for s in self.all_sessions
                if query in s["name"].lower()
                or query in s.get("meta", {}).get("title", "").lower()
                or query in s.get("meta", {}).get("summary", "").lower()
                or any(query in tag.lower() for tag in s.get("meta", {}).get("tags", []))
            ]

        await self.update_session_list()

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Update preview when a session is highlighted."""
        if event.item is None:
            return

        session_name = event.item.name
        session = next((s for s in self.filtered_sessions if s["name"] == session_name), None)

        if session:
            preview = self.query_one("#session-preview", SessionMetadataPreview)
            preview.update_session(session)

    def action_select(self) -> None:
        """Select the currently highlighted session."""
        list_view = self.query_one("#session-list", ListView)
        if list_view.highlighted_child:
            session_name = list_view.highlighted_child.name
            self.dismiss(session_name)

    def action_cancel(self) -> None:
        """Cancel session selection."""
        self.dismiss(None)


class ModelsManagerScreen(Screen):
    """Interactive models manager for listing, pulling, and deleting Ollama models."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "quit_screen", "Back"),
        ("ctrl+c", "quit_screen", "Back"),
    ]

    def __init__(self, api_base_url: str) -> None:
        super().__init__()
        self.api_base_url = api_base_url
        self.models: list[dict] = []

    def compose(self) -> ComposeResult:
        """Create the models manager UI."""
        yield Header()
        with Container():
            yield Label("Ollama Models (r=refresh, esc=back)")
            yield ListView(id="models-list")
            yield Label("", id="status-message")
        yield Footer()

    async def on_mount(self) -> None:
        """Load models when the screen is mounted."""
        await self.refresh_models()

    async def refresh_models(self) -> None:
        """Fetch and display models from API."""
        await self.update_status("Loading models...")

        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(f"{self.api_base_url}/api/v1/models", timeout=10.0)
                res.raise_for_status()
                data = res.json()
                model_names = data.get("models", [])

                # Fetch detailed info for each model
                self.models = []
                for name in model_names:
                    try:
                        info_res = await client.get(
                            f"{self.api_base_url}/api/v1/models/{name}/info",
                            timeout=10.0
                        )
                        info_res.raise_for_status()
                        info_data = info_res.json()
                        model_info = info_data.get("info", {})
                        self.models.append({
                            "name": name,
                            "size": model_info.get("size", 0),
                            "modified_at": model_info.get("modified_at", ""),
                        })
                    except Exception:
                        # If info fails, just add name
                        self.models.append({"name": name, "size": 0, "modified_at": ""})

                await self.update_models_list()
                await self.update_status(f"Loaded {len(self.models)} models")

        except Exception as e:
            log.error(f"Failed to load models: {e}")
            await self.update_status(f"Error: {e}")

    async def update_models_list(self) -> None:
        """Update the ListView with models."""
        list_view = self.query_one("#models-list", ListView)
        await list_view.clear()

        if not self.models:
            await list_view.append(ListItem(Label("No models found")))
            return

        for model in self.models:
            name = model.get("name", "")
            size_bytes = model.get("size", 0)
            # Format size (simple version)
            if size_bytes > 0:
                size_gb = size_bytes / (1024 ** 3)
                size_str = f"{size_gb:.1f} GB"
            else:
                size_str = "Unknown"

            label_text = f"{name} ({size_str})"
            await list_view.append(ListItem(Label(label_text), name=name))

    async def update_status(self, message: str) -> None:
        """Update status message."""
        try:
            status = self.query_one("#status-message", Label)
            status.update(message)
        except Exception:
            pass

    async def action_refresh(self) -> None:
        """Refresh the models list."""
        await self.refresh_models()

    def action_quit_screen(self) -> None:
        """Close the models manager screen."""
        self.dismiss()


class MyGPTTUI(App):
    CSS_PATH = None
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+s", "pick_session", "Sessions"),
        ("ctrl+r", "toggle_rag", "Toggle RAG"),
        ("ctrl+m", "models_manager", "Models"),
        ("ctrl+n", "rename_session", "Rename"),
    ]

    def __init__(self, session: str = "default", api_base_url: Optional[str] = None, config_path: Optional[str] = None) -> None:
        super().__init__()
        cfg = load_config(config_path)
        self.session = session
        # api_base_url parameter or config value, guaranteed to be str due to fallback
        base_url = api_base_url if api_base_url is not None else cfg.get("api", "base_url", fallback="http://127.0.0.1:8000")
        self.api_base_url: str = str(base_url)  # Ensure str type for mypy
        self.config_path = config_path
        self.rag_enabled = False  # Track current session RAG state

        log.info("TUI initialized", extra={"session": session, "api": self.api_base_url})

    def _unlock_prompt(self) -> None:
        """Re-enable the input prompt and focus it.

        This is called after streaming completes to allow the user to send
        another message. If it fails (e.g., app is shutting down), we log
        the error but don't crash.
        """
        try:
            self.prompt.disabled = False
            self.prompt.focus()
            log.debug("Input prompt unlocked and focused")
        except AttributeError as e:
            # Widget not yet composed or already destroyed
            log.warning(f"Failed to unlock prompt (widget not available): {e}")
        except Exception as e:
            # Other Textual-related errors (app shutting down, etc.)
            log.warning(f"Failed to unlock prompt: {type(e).__name__}: {e}")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            self.output = ChatOutput()
            yield self.output
            self.rag_status = Label("RAG: OFF", id="rag-status")
            yield self.rag_status
            self.prompt = Input(placeholder="Type a message and press Enter")
            yield self.prompt
        yield Footer()

    async def on_mount(self) -> None:
        """Called when the app is mounted and ready.

        Ensures the input prompt is in a clean state and ready to accept input.
        This provides a defensive reset in case the app was previously in an
        inconsistent state.
        """
        self._unlock_prompt()
        # Fetch RAG status for current session
        await self._fetch_rag_status()

    async def _fetch_rag_status(self) -> None:
        """Fetch RAG enabled status for current session."""
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    f"{self.api_base_url}/api/v1/sessions/{self.session}/metadata"
                )
                res.raise_for_status()
                data = res.json()
                self.rag_enabled = data.get("rag_enabled", False)
                self._update_rag_status()
        except Exception as e:
            log.warning(f"Failed to fetch RAG status for session {self.session}: {e}")
            self.rag_enabled = False

    def _update_rag_status(self) -> None:
        """Update RAG status label."""
        try:
            status = "RAG: ON" if self.rag_enabled else "RAG: OFF"
            self.rag_status.update(status)
        except Exception:
            # Widget not yet available or app shutting down
            pass

    async def action_toggle_rag(self) -> None:
        """Toggle RAG for current session."""
        try:
            endpoint = "disable" if self.rag_enabled else "enable"
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    f"{self.api_base_url}/api/v1/sessions/{self.session}/rag/{endpoint}"
                )
                res.raise_for_status()

            self.rag_enabled = not self.rag_enabled
            self._update_rag_status()
            log.info(f"RAG {'enabled' if self.rag_enabled else 'disabled'} for session {self.session}")
        except Exception as e:
            log.error(f"Failed to toggle RAG: {type(e).__name__}: {e}")

    async def action_pick_session(self) -> None:
        """Open the session picker and switch to the selected session."""
        session_name = await self.push_screen_wait(SessionPickerScreen(self.config_path))

        if session_name:
            # Update the current session
            self.session = session_name
            # Clear the chat output
            self.output.clear()
            # Show confirmation message
            self.output.append(f"Switched to session: {session_name}\n\n")
            # Fetch RAG status for new session
            await self._fetch_rag_status()
            log.info("Session switched", extra={"session": session_name})

    async def action_models_manager(self) -> None:
        """Open the models manager screen."""
        await self.push_screen_wait(ModelsManagerScreen(self.api_base_url))
        log.info("Models manager closed")

    async def action_rename_session(self) -> None:
        """Rename the current session with automatic filename sync."""
        from textual.widgets import Label
        from textual.containers import Container
        from textual.screen import ModalScreen
        from textual.widgets import Button

        class RenameScreen(ModalScreen[str | None]):
            """Modal screen for renaming a session."""

            def __init__(self, current_session: str, current_title: str = "") -> None:
                super().__init__()
                self.current_session = current_session
                self.current_title = current_title

            def compose(self) -> ComposeResult:
                with Container(id="rename-dialog"):
                    yield Label(f"Rename session: {self.current_session}")
                    self.rename_input = Input(
                        placeholder="Enter new session name or title",
                        value=self.current_title,
                        id="rename-input"
                    )
                    yield self.rename_input
                    with Container(classes="rename-buttons"):
                        yield Button("Rename", variant="primary", id="rename-btn")
                        yield Button("Cancel", id="cancel-btn")

            async def on_mount(self) -> None:
                self.rename_input.focus()

            async def on_button_pressed(self, event: Button.Pressed) -> None:
                if event.button.id == "rename-btn":
                    new_name = self.rename_input.value.strip()
                    if new_name:
                        self.dismiss(new_name)
                    else:
                        self.dismiss(None)
                elif event.button.id == "cancel-btn":
                    self.dismiss(None)

        # Fetch current title from metadata
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    f"{self.api_base_url}/api/v1/sessions/{self.session}/metadata"
                )
                if res.status_code == 200:
                    data = res.json()
                    current_title = data.get("title", "")
                else:
                    current_title = ""
        except Exception:
            current_title = ""

        # Show rename dialog
        new_name = await self.push_screen_wait(RenameScreen(self.session, current_title))

        if not new_name:
            return

        # Call rename API
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    f"{self.api_base_url}/api/v1/sessions/{self.session}/rename",
                    json={"new_name": new_name, "sync_filename": True}
                )
                res.raise_for_status()
                data = res.json()

                old_name = self.session
                new_session_name = data.get("new_name", self.session)

                # Update session name if filename changed
                if new_session_name != old_name:
                    self.session = new_session_name
                    self.output.append(f"\nSession renamed: {old_name} → {new_session_name}\n\n")
                    log.info(f"Session renamed: {old_name} → {new_session_name}")
                else:
                    self.output.append(f"\nSession title updated to: {new_name}\n\n")
                    log.info(f"Session title updated: {new_name}")

        except Exception as e:
            log.error(f"Failed to rename session: {type(e).__name__}: {e}")
            self.output.append(f"\n[error] Failed to rename session: {e}\n\n")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        # Prevent double-submits while a stream is active
        self.prompt.value = ""
        self.prompt.disabled = True
        log.debug("Input prompt locked for streaming")

        self.output.clear()
        self.output.append("→ " + text + "\n\n")

        # Run streaming in the background so the UI stays responsive.
        asyncio.create_task(self._stream_chat(text))

    async def _stream_chat(self, prompt: str) -> None:
        url = f"{self.api_base_url}/api/v1/chat/stream"

        payload = {
            "session": self.session,
            "prompt": prompt,
            "rag_enabled": self.rag_enabled,
        }

        log.debug("Sending chat request", extra={"session": self.session, "rag_enabled": self.rag_enabled})

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    # Optional label so it's obvious when assistant starts
                    self.output.append("Assistant: ")

                    # Buffer for detecting special markers
                    buffer = ""
                    async for chunk in resp.aiter_text():
                        if not chunk:
                            continue

                        buffer += chunk

                        # Check for retry status markers
                        while "__RETRY_START__" in buffer and "__RETRY_END__" in buffer:
                            start_idx = buffer.index("__RETRY_START__")
                            end_idx = buffer.index("__RETRY_END__") + len("__RETRY_END__")

                            # Extract the marker content
                            marker_content = buffer[start_idx:end_idx]

                            # Parse retry status
                            try:
                                import json
                                json_start = start_idx + len("__RETRY_START__")
                                json_end = end_idx - len("__RETRY_END__")
                                retry_json = buffer[json_start:json_end]
                                retry_data = json.loads(retry_json)

                                # Display reconnection status
                                attempt = retry_data.get("attempt", "?")
                                delay = retry_data.get("delay", "?")
                                self.output.append(
                                    f"\n[reconnecting] Connection lost. Retrying (attempt {attempt}) in {delay:.1f}s...\n"
                                )
                            except Exception as parse_err:
                                log.warning(f"Failed to parse retry status: {parse_err}")

                            # Remove the marker from buffer and continue
                            buffer = buffer[:start_idx] + buffer[end_idx:]

                        # Check for RAG markers (existing functionality)
                        if "__RAG_START__" in buffer and "__RAG_END__" in buffer:
                            start_idx = buffer.index("__RAG_START__")
                            end_idx = buffer.index("__RAG_END__") + len("__RAG_END__")

                            # Skip RAG markers in TUI (they're for WebUI)
                            buffer = buffer[:start_idx] + buffer[end_idx:]

                        # Yield any complete text that's not part of markers
                        # Keep potential partial markers in buffer
                        if buffer:
                            # First check if we might have a partial marker at the end
                            safe_idx = len(buffer)
                            has_partial_marker = False
                            for marker in ["__RETRY_START__", "__RAG_START__"]:
                                for i in range(1, len(marker)):
                                    if buffer.endswith(marker[:i]):
                                        safe_idx = len(buffer) - i
                                        has_partial_marker = True
                                        break
                                if has_partial_marker:
                                    break

                            # If buffer has complete markers, don't flush yet (let the marker handlers above process it)
                            if "__RETRY_START__" in buffer or "__RAG_START__" in buffer:
                                # Complete markers present, let them be processed in next iteration
                                pass
                            elif has_partial_marker and safe_idx > 0:
                                # Has partial marker at end, flush safe part and keep partial
                                self.output.append(buffer[:safe_idx])
                                buffer = buffer[safe_idx:]
                            elif not has_partial_marker:
                                # No markers at all, flush everything
                                self.output.append(buffer)
                                buffer = ""
                            elif safe_idx == 0 and len(buffer) > MARKER_BUFFER_OVERFLOW_THRESHOLD:
                                # Entire buffer is potential partial marker but too large, flush it
                                self.output.append(buffer)
                                buffer = ""

                    # Flush any remaining buffer
                    if buffer:
                        self.output.append(buffer)

            self.output.append("\n\n")

        except Exception as e:
            log.exception("TUI chat stream failed")
            self.output.append(f"\n\n[error] {type(e).__name__}: {e}\n\n")

        finally:
            self._unlock_prompt()


if __name__ == "__main__":
    MyGPTTUI().run()
