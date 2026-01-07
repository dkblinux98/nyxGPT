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


class MyGPTTUI(App):
    CSS_PATH = None
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+s", "pick_session", "Sessions"),
        ("ctrl+r", "toggle_rag", "Toggle RAG"),
    ]

    def __init__(self, session: str = "default", api_base_url: Optional[str] = None, config_path: Optional[str] = None) -> None:
        super().__init__()
        cfg = load_config(config_path)
        self.session = session
        self.api_base_url = api_base_url or cfg.get("api", "base_url", fallback="http://127.0.0.1:8000")
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
                    async for chunk in resp.aiter_text():
                        if chunk:
                            self.output.append(chunk)

            self.output.append("\n\n")

        except Exception as e:
            log.exception("TUI chat stream failed")
            self.output.append(f"\n\n[error] {type(e).__name__}: {e}\n\n")

        finally:
            self._unlock_prompt()


if __name__ == "__main__":
    MyGPTTUI().run()
