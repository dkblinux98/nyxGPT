"""
Terminal UI (TUI) for nyxGPT.

This is an intentionally minimal Textual-based client that talks to the local
FastAPI backend. It focuses on correctness and streaming, not visual polish.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from nyxgpt.config import load_config
from nyxgpt.sessions import delete_session, get_sessions_dir, list_sessions

log = logging.getLogger(__name__)

# Overflow threshold for buffer safety valve. This is a conservative lower
# threshold used when the entire buffer appears to be a potential partial
# marker. Catching runaway buffers early (at 100 bytes) prevents excessive
# memory buildup and output delays in edge cases with malformed streams.
MARKER_BUFFER_OVERFLOW_THRESHOLD = 100


class ChatOutput(Static):
    """Widget to display assistant output incrementally."""

    def __init__(self, *args, **kwargs):
        """Initialize the widget with an empty output buffer."""
        super().__init__(*args, **kwargs)
        self._buffer: str = ""

    def clear(self) -> None:
        """Reset the output buffer and clear the displayed text."""
        self._buffer = ""
        self.update("")

    def append(self, text: str) -> None:
        """Append `text` to the buffer and refresh the displayed content."""
        # Keep our own buffer to avoid Textual version differences
        self._buffer += text
        self.update(self._buffer)

    def remove_typing_indicator(self) -> None:
        """Remove the typing indicator (⋯) from the end of the buffer if present."""
        if self._buffer.endswith("⋯"):
            self._buffer = self._buffer[:-1]
            self.update(self._buffer)


class SessionStatusBar(Static):
    """Widget to display current session information in the status bar."""

    def __init__(self, *args, **kwargs):
        """Initialize the status bar with empty/default session info."""
        super().__init__(*args, **kwargs)
        self.session_name: str = ""
        self.message_count: int = 0
        self.model: str = ""
        self.rag_enabled: bool = False
        self.attached_doc_count: int = 0

    def update_info(
        self,
        session_name: str,
        message_count: int,
        model: str,
        rag_enabled: bool,
        attached_doc_count: int = 0,
    ) -> None:
        """Update session information and refresh display."""
        self.session_name = session_name
        self.message_count = message_count
        self.model = model
        self.rag_enabled = rag_enabled
        self.attached_doc_count = attached_doc_count
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Refresh the status bar display with current info."""
        rag_indicator = "RAG:ON" if self.rag_enabled else "RAG:OFF"
        docs_indicator = f"FI:{self.attached_doc_count}" if self.attached_doc_count > 0 else ""
        parts = [
            f"Session: {self.session_name}",
            f"Messages: {self.message_count}",
            f"Model: {self.model}",
            rag_indicator,
        ]
        if docs_indicator:
            parts.append(docs_indicator)
        self.update(" | ".join(parts))


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
Tags: {", ".join(tags) if tags else "None"}

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

    def __init__(self, config_path: str | None = None) -> None:
        """Initialize the picker, loading config and clearing the session lists."""
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
                s
                for s in self.all_sessions
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
        """Initialize the models manager with the API base URL and an empty model list."""
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
                            timeout=10.0,
                        )
                        info_res.raise_for_status()
                        info_data = info_res.json()
                        model_info = info_data.get("info", {})
                        self.models.append(
                            {
                                "name": name,
                                "size": model_info.get("size", 0),
                                "modified_at": model_info.get("modified_at", ""),
                            }
                        )
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
                size_gb = size_bytes / (1024**3)
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


class SearchResultsScreen(ModalScreen[dict | None]):
    """Modal screen for searching messages across sessions."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "select_result", "Jump to Message"),
    ]

    def __init__(self, api_base_url: str, current_session: str):
        """Initialize the search screen with an empty result set."""
        super().__init__()
        self.api_base_url = api_base_url
        self.current_session = current_session
        self.results: list[dict] = []
        self.case_sensitive = False

    def compose(self) -> ComposeResult:
        """Create the search dialog UI (input, filters, results, and preview)."""
        yield Header()
        with Container(id="search-dialog"):
            yield Label("Search Messages")
            self.search_input = Input(placeholder="Enter search query...", id="search-input")
            yield self.search_input

            # Filters
            with Container(id="filters"):
                self.case_checkbox = Checkbox("Case sensitive", id="case-sensitive")
                yield self.case_checkbox

            yield Button("Search", id="search-btn", variant="primary")

            yield Label("Results:", id="results-label")
            self.results_list = ListView(id="results-list")
            yield self.results_list

            # Preview panel
            with Container(id="preview-panel"):
                yield Label("Message Preview:")
                self.preview = Static(id="preview-content")
                yield self.preview
        yield Footer()

    async def on_mount(self) -> None:
        """Focus search input when screen appears."""
        self.search_input.focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle search button press."""
        if event.button.id == "search-btn":
            query = self.search_input.value.strip()
            if query:
                self.case_sensitive = self.case_checkbox.value
                await self.perform_search(query)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in search input."""
        if event.input.id == "search-input":
            query = event.value.strip()
            if query:
                self.case_sensitive = self.case_checkbox.value
                await self.perform_search(query)

    async def perform_search(self, query: str) -> None:
        """Call backend search API and populate results."""
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    f"{self.api_base_url}/api/v1/sessions/search",
                    params={
                        "query": query,
                        "case_sensitive": str(self.case_sensitive).lower(),
                        "limit": 50,
                    },
                )
                res.raise_for_status()
                data = res.json()
                self.results = data.get("results", [])
                await self._populate_list()

                if len(self.results) == 0:
                    self.notify(f"No results found for '{query}'", severity="warning")
                else:
                    self.notify(f"Found {len(self.results)} result(s)", severity="information")
        except Exception as e:
            self.notify(f"Search failed: {str(e)}", severity="error")
            self.results = []
            await self._populate_list()

    async def _populate_list(self) -> None:
        """Populate results list with search results."""
        await self.results_list.clear()

        if not self.results:
            return

        for _idx, result in enumerate(self.results):
            session_title = result.get("session_title") or result.get("session_name")
            role_icon = "👤" if result.get("role") == "user" else "🤖"
            matches_text = (
                f" ({result.get('matches')} matches)" if result.get("matches", 0) > 1 else ""
            )
            label_text = f"{role_icon} {session_title}{matches_text}"

            item = ListItem(Label(label_text))
            await self.results_list.append(item)

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Update preview when result is selected."""
        if event.item is None or not self.results:
            return

        try:
            idx = self.results_list.index
            if idx is not None:
                result = self.results[idx]
                preview_text = result.get("content_preview", "")
                self.preview.update(preview_text)
        except (IndexError, AttributeError):
            pass

    async def action_select_result(self) -> None:
        """Return selected result to caller."""
        if self.results_list.index is not None and self.results_list.index < len(self.results):
            result = self.results[self.results_list.index]
            self.dismiss(result)
        else:
            self.notify("No result selected", severity="warning")

    async def action_close(self) -> None:
        """Close the search screen."""
        self.dismiss(None)


class HelpOverlayScreen(ModalScreen[None]):
    """Modal screen displaying keyboard shortcuts and help information."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        ("ctrl+c", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        """Create the help overlay UI listing keyboard shortcuts."""
        yield Header()
        with Container(id="help-dialog"):
            yield Label("Keyboard Shortcuts")

            # Main shortcuts
            yield Label("\n[bold]Main Shortcuts:[/bold]")
            yield Label("  Ctrl+H / F1    - Show this help")
            yield Label("  Ctrl+P         - Command palette")
            yield Label("  Ctrl+C         - Quit application")

            # Navigation
            yield Label("\n[bold]Navigation:[/bold]")
            yield Label("  Tab            - Next pane")
            yield Label("  Shift+Tab      - Previous pane")

            # Sessions
            yield Label("\n[bold]Sessions:[/bold]")
            yield Label("  Ctrl+S         - Session picker")
            yield Label("  Ctrl+N         - Rename session")
            yield Label("  Ctrl+D         - Delete session")

            # Features
            yield Label("\n[bold]Features:[/bold]")
            yield Label("  Ctrl+F         - Search messages")
            yield Label("  Ctrl+R         - Toggle RAG")
            yield Label("  Ctrl+A         - Manage force-included documents")
            yield Label("  Ctrl+I         - Index code repository")
            yield Label("  Ctrl+M         - Models manager")
            yield Label("  Ctrl+L         - Clear output")

            # Commands
            yield Label("\n[bold]Slash Commands:[/bold]")
            yield Label("  /clear         - Clear output buffer")

            yield Label("\nPress ESC or Ctrl+C to close")
        yield Footer()

    async def action_close(self) -> None:
        """Close the help overlay."""
        self.dismiss(None)


class DeleteConfirmationScreen(ModalScreen[bool]):
    """Modal screen for confirming session deletion."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        ("ctrl+c", "cancel", "Cancel"),
        ("enter", "confirm", "Confirm"),
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
    ]

    def __init__(self, session_name: str) -> None:
        """Initialize the confirmation dialog for deleting `session_name`."""
        super().__init__()
        self.session_name = session_name

    def compose(self) -> ComposeResult:
        """Create the delete confirmation dialog UI."""
        yield Header()
        with Container(id="delete-confirm-dialog"):
            yield Label(f"Delete session '{self.session_name}'?")
            yield Label("[bold red]This cannot be undone.[/bold red]")
            with Container(classes="confirm-buttons"):
                yield Button("Delete", variant="error", id="confirm-btn")
                yield Button("Cancel", id="cancel-btn")
        yield Footer()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "confirm-btn":
            self.dismiss(True)
        elif event.button.id == "cancel-btn":
            self.dismiss(False)

    async def action_confirm(self) -> None:
        """Confirm deletion."""
        self.dismiss(True)

    async def action_cancel(self) -> None:
        """Cancel deletion."""
        self.dismiss(False)


class ManageDocumentsScreen(ModalScreen[None]):
    """Modal screen for managing documents attached to the current session."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        ("ctrl+c", "close", "Close"),
    ]

    def __init__(self, api_base_url: str, session: str) -> None:
        """Initialize the manager for `session`'s attached documents."""
        super().__init__()
        self.api_base_url = api_base_url
        self.session = session
        self._attached: list[str] = []

    def compose(self) -> ComposeResult:
        """Create the attached-documents UI (list, attach input, and buttons)."""
        with Container(id="docs-dialog"):
            yield Label(f"Attached documents: {self.session}")
            self.docs_list = ListView(id="docs-listview")
            yield self.docs_list
            self.attach_input = Input(placeholder="Enter doc_id to attach", id="attach-input")
            yield self.attach_input
            with Container(classes="rename-buttons"):
                yield Button("Attach", variant="primary", id="attach-btn")
                yield Button("Detach selected", variant="warning", id="detach-btn")
                yield Button("Close", id="close-btn")
        yield Footer()

    async def on_mount(self) -> None:
        """Focus the attach input and load the current attached document list."""
        self.attach_input.focus()
        await self._refresh_list()

    async def _refresh_list(self) -> None:
        """Fetch the session's attached document IDs and repopulate the list."""
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    f"{self.api_base_url}/api/v1/sessions/{self.session}/documents"
                )
                res.raise_for_status()
                data = res.json()
                self._attached = data.get("attached_doc_ids", [])
        except Exception as e:
            log.error(f"Failed to fetch attached documents: {e}")
            self._attached = []
        await self._populate_list()

    async def _populate_list(self) -> None:
        """Render `self._attached` into the document list widget."""
        await self.docs_list.clear()
        if not self._attached:
            await self.docs_list.append(ListItem(Label("(no documents attached)")))
        else:
            for doc_id in self._attached:
                await self.docs_list.append(ListItem(Label(doc_id)))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle attach/detach/close button presses."""
        if event.button.id == "attach-btn":
            doc_id = self.attach_input.value.strip()
            if not doc_id:
                return
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        f"{self.api_base_url}/api/v1/sessions/{self.session}/documents",
                        json={"doc_id": doc_id},
                    )
                    res.raise_for_status()
                self.attach_input.value = ""
                await self._refresh_list()
            except Exception as e:
                log.error(f"Failed to attach document '{doc_id}': {e}")
        elif event.button.id == "detach-btn":
            highlighted = self.docs_list.highlighted_child
            if highlighted is None or not self._attached:
                return
            idx = self.docs_list.index
            if idx is None or idx >= len(self._attached):
                return
            doc_id = self._attached[idx]
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.delete(
                        f"{self.api_base_url}/api/v1/sessions/{self.session}/documents/{doc_id}"
                    )
                    res.raise_for_status()
                await self._refresh_list()
            except Exception as e:
                log.error(f"Failed to detach document '{doc_id}': {e}")
        elif event.button.id == "close-btn":
            self.dismiss(None)

    def action_close(self) -> None:
        """Close the documents manager."""
        self.dismiss(None)


class CommandPaletteScreen(ModalScreen[str | None]):
    """Modal screen for quick command access with fuzzy search."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        ("ctrl+c", "close", "Close"),
        ("enter", "execute_command", "Execute"),
    ]

    def __init__(self) -> None:
        """Initialize the command palette with its static list of available commands."""
        super().__init__()
        self.all_commands = [
            {
                "key": "pick_session",
                "label": "Session Picker",
                "description": "Browse and switch sessions",
            },
            {
                "key": "search_messages",
                "label": "Search Messages",
                "description": "Search across all sessions",
            },
            {
                "key": "toggle_rag",
                "label": "Toggle RAG",
                "description": "Enable/disable RAG for current session",
            },
            {
                "key": "models_manager",
                "label": "Models Manager",
                "description": "Manage Ollama models",
            },
            {
                "key": "rename_session",
                "label": "Rename Session",
                "description": "Rename current session",
            },
            {
                "key": "delete_session",
                "label": "Delete Session",
                "description": "Delete current session",
            },
            {
                "key": "clear_output",
                "label": "Clear Output",
                "description": "Clear chat output buffer",
            },
            {
                "key": "show_help",
                "label": "Show Help",
                "description": "Display keyboard shortcuts",
            },
            {"key": "quit", "label": "Quit", "description": "Exit the application"},
        ]
        self.filtered_commands = self.all_commands.copy()

    def compose(self) -> ComposeResult:
        """Create the command palette UI (search input, command list, description)."""
        yield Header()
        with Container(id="command-palette-dialog"):
            yield Label("Command Palette")
            yield Input(placeholder="Type to search commands...", id="command-search")
            yield ListView(id="command-list")
            with Container(id="command-preview"):
                yield Label("", id="command-description")
        yield Footer()

    async def on_mount(self) -> None:
        """Focus search input when screen appears."""
        search_input = self.query_one("#command-search", Input)
        search_input.focus()
        await self.update_command_list()

    async def on_input_changed(self, event: Input.Changed) -> None:
        """Filter commands based on search input."""
        if event.input.id != "command-search":
            return

        query = event.value.lower()
        if not query:
            self.filtered_commands = self.all_commands.copy()
        else:
            # Simple fuzzy search: match if query chars appear in order
            self.filtered_commands = [
                cmd
                for cmd in self.all_commands
                if query in cmd["label"].lower() or query in cmd["description"].lower()
            ]

        await self.update_command_list()

    async def update_command_list(self) -> None:
        """Update the ListView with filtered commands."""
        list_view = self.query_one("#command-list", ListView)
        await list_view.clear()

        for cmd in self.filtered_commands:
            label_text = f"{cmd['label']} - {cmd['description']}"
            await list_view.append(ListItem(Label(label_text), name=cmd["key"]))

        # Update description for first command if any
        if self.filtered_commands:
            desc_label = self.query_one("#command-description", Label)
            desc_label.update(self.filtered_commands[0]["description"])

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Update description when a command is highlighted."""
        if event.item is None:
            return

        command_key = event.item.name
        command = next((c for c in self.filtered_commands if c["key"] == command_key), None)

        if command:
            desc_label = self.query_one("#command-description", Label)
            desc_label.update(command["description"])

    async def action_execute_command(self) -> None:
        """Execute the currently highlighted command."""
        list_view = self.query_one("#command-list", ListView)
        if list_view.highlighted_child:
            command_key = list_view.highlighted_child.name
            self.dismiss(command_key)

    async def action_close(self) -> None:
        """Close the command palette."""
        self.dismiss(None)


class NyxGPTTUI(App):
    """The main Textual application: chat view, session state, and key bindings."""

    CSS_PATH = None
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+h", "show_help", "Help"),
        ("f1", "show_help", "Help"),
        ("ctrl+p", "command_palette", "Commands"),
        ("tab", "focus_next", "Next"),
        ("shift+tab", "focus_previous", "Previous"),
        ("ctrl+s", "pick_session", "Sessions"),
        ("ctrl+f", "search_messages", "Search"),
        ("ctrl+r", "toggle_rag", "Toggle RAG"),
        ("ctrl+i", "index_repository", "Index Repo"),
        ("ctrl+m", "models_manager", "Models"),
        ("ctrl+n", "rename_session", "Rename"),
        ("ctrl+d", "delete_session", "Delete"),
        ("ctrl+l", "clear_output", "Clear"),
        ("ctrl+a", "manage_documents", "Docs"),
    ]

    def __init__(
        self,
        session: str = "default",
        api_base_url: str | None = None,
        config_path: str | None = None,
    ) -> None:
        """Initialize the app for `session`, resolving the API base URL from config."""
        super().__init__()
        cfg = load_config(config_path)
        self.session = session
        # api_base_url parameter or config value, guaranteed to be str due to fallback
        base_url = (
            api_base_url
            if api_base_url is not None
            else cfg.get("api", "base_url", fallback="http://127.0.0.1:8000")
        )
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
        """Create the main app layout: header, chat output, status bar, and prompt."""
        yield Header(show_clock=True)
        with Vertical():
            self.output = ChatOutput()
            yield self.output
            self.status_bar = SessionStatusBar(id="session-status-bar")
            yield self.status_bar
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
        # Fetch session info for current session
        await self._update_session_status()

    async def _update_session_status(self) -> None:
        """Fetch and update session status bar with current session info."""
        try:
            async with httpx.AsyncClient() as client:
                # Fetch session metadata
                metadata_res = await client.get(
                    f"{self.api_base_url}/api/v1/sessions/{self.session}/metadata"
                )
                metadata_res.raise_for_status()
                metadata = metadata_res.json()

                # Fetch session messages to count them
                session_res = await client.get(
                    f"{self.api_base_url}/api/v1/sessions/{self.session}"
                )
                session_res.raise_for_status()
                session_data = session_res.json()

                # Fetch attached documents count
                attached_doc_count = 0
                try:
                    docs_res = await client.get(
                        f"{self.api_base_url}/api/v1/sessions/{self.session}/documents"
                    )
                    if docs_res.is_success:
                        docs_data = docs_res.json()
                        attached_doc_count = len(docs_data.get("attached_doc_ids", []))
                except Exception:
                    pass

                # Extract info
                self.rag_enabled = metadata.get("rag_enabled", False)
                model = metadata.get("model", "unknown")
                message_count = len(session_data.get("messages", []))

                # Update status bar
                # Widget not yet available or app shutting down
                with contextlib.suppress(Exception):
                    self.status_bar.update_info(
                        session_name=self.session,
                        message_count=message_count,
                        model=model,
                        rag_enabled=self.rag_enabled,
                        attached_doc_count=attached_doc_count,
                    )

        except Exception as e:
            log.warning(f"Failed to fetch session status for session {self.session}: {e}")
            self.rag_enabled = False

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
            # Update status bar to reflect new RAG status
            await self._update_session_status()
            log.info(
                f"RAG {'enabled' if self.rag_enabled else 'disabled'} for session {self.session}"
            )
        except Exception as e:
            log.error(f"Failed to toggle RAG: {type(e).__name__}: {e}")

    def action_pick_session(self) -> None:
        """Open the session picker and switch to the selected session."""
        self.run_worker(self._pick_session_worker())

    async def _pick_session_worker(self) -> None:
        """Worker to handle session picker."""
        session_name = await self.push_screen_wait(SessionPickerScreen(self.config_path))

        if session_name:
            # Update the current session
            self.session = session_name
            # Clear the chat output
            self.output.clear()
            # Show confirmation message
            self.output.append(f"Switched to session: {session_name}\n\n")
            # Update status bar for new session
            await self._update_session_status()
            log.info("Session switched", extra={"session": session_name})

    def action_models_manager(self) -> None:
        """Open the models manager screen."""
        self.run_worker(self._models_manager_worker())

    async def _models_manager_worker(self) -> None:
        """Worker to handle models manager."""
        await self.push_screen_wait(ModelsManagerScreen(self.api_base_url))
        log.info("Models manager closed")

    def action_search_messages(self) -> None:
        """Open message search modal."""
        self.run_worker(self._search_messages_worker())

    async def _search_messages_worker(self) -> None:
        """Worker to handle message search."""
        result = await self.push_screen_wait(SearchResultsScreen(self.api_base_url, self.session))

        if result:
            # User selected a search result
            session_name = result.get("session_name")
            message_index = result.get("message_index")

            if session_name and message_index is not None:
                # Switch to the session if different
                if session_name != self.session:
                    self.session = session_name
                    self.title = f"nyxGPT TUI - {session_name}"
                    # Clear current chat output
                    try:
                        output = self.query_one("#output", ChatOutput)
                        output.clear()
                    except Exception:
                        pass

                # Notify user about navigation
                self.notify(
                    f"Switched to session '{session_name}', message {message_index + 1}",
                    severity="information",
                )

    def action_rename_session(self) -> None:
        """Rename the current session with automatic filename sync."""
        self.run_worker(self._rename_session_worker())

    async def _rename_session_worker(self) -> None:
        """Worker to handle async rename session logic."""
        from textual.containers import Container
        from textual.screen import ModalScreen
        from textual.widgets import Button, Label

        class RenameScreen(ModalScreen[str | None]):
            """Modal screen for renaming a session."""

            def __init__(self, current_session: str, current_title: str = "") -> None:
                """Store the session name and current title to prefill the dialog."""
                super().__init__()
                self.current_session = current_session
                self.current_title = current_title

            def compose(self) -> ComposeResult:
                """Yield the rename dialog's input field and action buttons."""
                with Container(id="rename-dialog"):
                    yield Label(f"Rename session: {self.current_session}")
                    self.rename_input = Input(
                        placeholder="Enter new session name or title",
                        value=self.current_title,
                        id="rename-input",
                    )
                    yield self.rename_input
                    with Container(classes="rename-buttons"):
                        yield Button("Rename", variant="primary", id="rename-btn")
                        yield Button("Cancel", id="cancel-btn")

            async def on_mount(self) -> None:
                """Focus the rename input as soon as the dialog is shown."""
                self.rename_input.focus()

            async def on_button_pressed(self, event: Button.Pressed) -> None:
                """Dismiss with the new name on confirm, or `None` on cancel."""
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
                    json={"new_name": new_name, "sync_filename": True},
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

    def action_manage_documents(self) -> None:
        """Open the document attachment manager for the current session."""
        self.run_worker(self._manage_documents_worker())

    async def _manage_documents_worker(self) -> None:
        """Worker to handle document attachment management."""
        await self.push_screen_wait(ManageDocumentsScreen(self.api_base_url, self.session))
        log.info("Document manager closed")

    def action_index_repository(self) -> None:
        """Index a code repository for RAG."""
        self.run_worker(self._index_repository_worker())

    async def _index_repository_worker(self) -> None:
        """Worker to handle async repository indexing."""
        from textual.containers import Container
        from textual.screen import ModalScreen
        from textual.widgets import Button, Label

        class IndexRepoScreen(ModalScreen[dict | None]):
            """Modal screen for indexing a code repository."""

            def compose(self) -> ComposeResult:
                """Yield the repo-indexing dialog's inputs and action buttons."""
                with Container(id="index-repo-dialog"):
                    yield Label("Index Code Repository for RAG")
                    self.repo_path_input = Input(
                        placeholder="Enter repository path",
                        id="repo-path-input",
                    )
                    yield self.repo_path_input
                    yield Label("Extensions (optional, e.g., .py,.js):")
                    self.extensions_input = Input(
                        placeholder="Leave empty for all supported types",
                        id="extensions-input",
                    )
                    yield self.extensions_input
                    self.docs_only_checkbox = Checkbox(
                        "Extract only docs/comments", id="docs-only-checkbox"
                    )
                    yield self.docs_only_checkbox
                    with Container(classes="index-repo-buttons"):
                        yield Button("Index", variant="primary", id="index-btn")
                        yield Button("Cancel", id="cancel-btn")

            async def on_mount(self) -> None:
                """Focus the repository path input as soon as the dialog is shown."""
                self.repo_path_input.focus()

            async def on_button_pressed(self, event: Button.Pressed) -> None:
                """Dismiss with the collected indexing options, or `None` on cancel."""
                if event.button.id == "index-btn":
                    repo_path = self.repo_path_input.value.strip()
                    if repo_path:
                        extensions = self.extensions_input.value.strip()
                        ext_list = (
                            [e.strip() for e in extensions.split(",") if e.strip()]
                            if extensions
                            else None
                        )
                        self.dismiss(
                            {
                                "repo_path": repo_path,
                                "extensions": ext_list,
                                "docs_only": self.docs_only_checkbox.value,
                            }
                        )
                    else:
                        self.dismiss(None)
                elif event.button.id == "cancel-btn":
                    self.dismiss(None)

        # Show index repo dialog
        params = await self.push_screen_wait(IndexRepoScreen())

        if not params:
            return

        # Show indexing message
        self.output.append(
            f"\nIndexing repository: {params['repo_path']}\nThis may take a while...\n\n"
        )

        # Call index-repo API
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                res = await client.post(
                    f"{self.api_base_url}/api/v1/rag/index-repo",
                    json={
                        "repo_path": params["repo_path"],
                        "extensions": params["extensions"],
                        "extract_docs_only": params["docs_only"],
                    },
                )
                res.raise_for_status()
                data = res.json()

                self.output.append(
                    f"\n✓ Repository indexed successfully!\n"
                    f"  Files indexed: {data.get('total_files', 0)}\n"
                    f"  Total chunks: {data.get('total_chunks', 0)}\n\n"
                )
                log.info(f"Repository indexed: {params['repo_path']}")

        except httpx.TimeoutException:
            log.error("Repository indexing timed out")
            self.output.append(
                "\n[error] Repository indexing timed out (>5 minutes). "
                "Try indexing a smaller repository or use the CLI.\n\n"
            )
        except Exception as e:
            log.error(f"Failed to index repository: {type(e).__name__}: {e}")
            self.output.append(f"\n[error] Failed to index repository: {e}\n\n")

    def action_delete_session(self) -> None:
        """Delete the current session with confirmation."""
        self.run_worker(self._delete_session_worker())

    async def _delete_session_worker(self) -> None:
        """Worker to handle async delete session logic."""
        # Check if this is the last session
        cfg = load_config(self.config_path)
        sessions_dir = get_sessions_dir(cfg)
        all_sessions = list_sessions(cfg)

        if len(all_sessions) <= 1:
            self.output.append("\n[error] Cannot delete the last remaining session.\n\n")
            log.warning("Attempted to delete last session")
            return

        # Show confirmation dialog
        confirmed = await self.push_screen_wait(DeleteConfirmationScreen(self.session))

        if not confirmed:
            log.info(f"Session deletion cancelled for {self.session}")
            return

        # Delete the session
        session_to_delete = self.session
        try:
            success = delete_session(session_to_delete, sessions_dir)
            if not success:
                self.output.append("\n[error] Failed to delete session: Session not found.\n\n")
                log.error(f"Session not found: {session_to_delete}")
                return

            # Switch to another session (first available after refresh)
            updated_sessions = list_sessions(cfg)
            if updated_sessions:
                # Switch to first available session
                new_session = updated_sessions[0]["name"]
                self.session = new_session
                self.output.clear()
                self.output.append(
                    f"Session '{session_to_delete}' deleted.\nSwitched to session: {new_session}\n\n"
                )
                # Update status bar for new session
                await self._update_session_status()
                log.info(f"Session deleted: {session_to_delete}, switched to {new_session}")
            else:
                # This should not happen as we checked for last session, but handle defensively
                self.output.append("\n[error] Session deleted but no sessions remaining.\n\n")
                log.error("No sessions remaining after delete")

        except Exception as e:
            log.error(f"Failed to delete session: {type(e).__name__}: {e}")
            self.output.append(f"\n[error] Failed to delete session: {e}\n\n")

    def action_clear_output(self) -> None:
        """Clear the chat output buffer."""
        try:
            self.output.clear()
            log.info(f"Output cleared for session {self.session}")
        except Exception as e:
            log.error(f"Failed to clear output: {type(e).__name__}: {e}")

    def action_show_help(self) -> None:
        """Show the keyboard shortcuts help overlay."""
        self.run_worker(self._show_help_worker())

    async def _show_help_worker(self) -> None:
        """Worker to handle help overlay."""
        await self.push_screen_wait(HelpOverlayScreen())
        log.info("Help overlay closed")

    def action_command_palette(self) -> None:
        """Show the command palette and execute selected command."""
        self.run_worker(self._command_palette_worker())

    async def _command_palette_worker(self) -> None:
        """Worker to handle command palette."""
        command_key = await self.push_screen_wait(CommandPaletteScreen())

        if command_key:
            log.info(f"Executing command from palette: {command_key}")
            # Execute the action
            await self.run_action(command_key)

    async def _handle_command(self, command: str) -> None:
        """Handle slash commands."""
        if command == "clear":
            self.action_clear_output()
        else:
            self.output.append(f"[System] Unknown command: /{command}\n\n")
            log.warning(f"Unknown command: /{command}")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle prompt submission: dispatch slash commands or start a chat stream."""
        text = event.value.strip()
        if not text:
            return

        # Handle commands
        if text.startswith("/"):
            await self._handle_command(text[1:])
            self.prompt.value = ""
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
        """Stream a chat response from the backend and render it incrementally."""
        import json

        url = f"{self.api_base_url}/api/v1/chat/stream"

        payload = {
            "session": self.session,
            "prompt": prompt,
            "rag_enabled": self.rag_enabled,
        }

        log.debug(
            "Sending chat request",
            extra={"session": self.session, "rag_enabled": self.rag_enabled},
        )

        try:
            async with (
                httpx.AsyncClient(timeout=None) as client,
                client.stream("POST", url, json=payload) as resp,
            ):
                resp.raise_for_status()
                # Show typing indicator before first token
                self.output.append("Assistant: ⋯")
                first_content = True

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

                        # Parse retry status
                        try:
                            json_start = start_idx + len("__RETRY_START__")
                            json_end = end_idx - len("__RETRY_END__")
                            retry_json = buffer[json_start:json_end]
                            retry_data = json.loads(retry_json)

                            # Display reconnection status
                            attempt = retry_data.get("attempt", "?")
                            delay = retry_data.get("delay", "?")
                            if first_content:
                                self.output.remove_typing_indicator()
                                first_content = False
                            self.output.append(
                                f"\n[reconnecting] Connection lost. Retrying (attempt {attempt}) in {delay:.1f}s...\n"
                            )
                        except Exception as parse_err:
                            log.warning(f"Failed to parse retry status: {parse_err}")
                        finally:
                            # Always remove the marker from buffer (whether parsing succeeded or failed)
                            buffer = buffer[:start_idx] + buffer[end_idx:]

                    # Check for RAG markers and display citation summary
                    if "__RAG_START__" in buffer and "__RAG_END__" in buffer:
                        start_idx = buffer.index("__RAG_START__")
                        end_idx = buffer.index("__RAG_END__") + len("__RAG_END__")

                        # Parse and display RAG citation summary
                        try:
                            rag_start = start_idx + len("__RAG_START__")
                            rag_json = buffer[rag_start : buffer.index("__RAG_END__")]
                            rag_data = json.loads(rag_json)

                            if rag_data.get("type") == "rag_metadata" and isinstance(
                                rag_data.get("chunks"), list
                            ):
                                chunks = rag_data["chunks"]
                                chunk_count = len(chunks)

                                # Display compact citation summary
                                citation_summary = f"\n[dim][RAG: {chunk_count} source{'s' if chunk_count != 1 else ''} retrieved][/dim]\n"
                                self.output.append(citation_summary)

                                # Display brief citation details (doc_id and score)
                                for idx, chunk in enumerate(chunks, 1):
                                    doc_id = chunk.get("doc_id", "Unknown")
                                    chunk_id = chunk.get("chunk_id")
                                    # Use explicit None checking to avoid treating 0.0 as falsy
                                    score = chunk.get("similarity_score")
                                    if score is None:
                                        score = chunk.get("score", 0.0)

                                    # Format score with color based on quality
                                    if score >= 0.7:
                                        score_style = "green"
                                    elif score >= 0.5:
                                        score_style = "yellow"
                                    else:
                                        score_style = "red"

                                    chunk_ref = (
                                        f"chunk {chunk_id}" if chunk_id is not None else "source"
                                    )
                                    citation_line = f"[dim]  [{idx}] {doc_id} ({chunk_ref}) - score: [{score_style}]{score:.3f}[/{score_style}][/dim]\n"
                                    self.output.append(citation_line)

                                self.output.append("\n")
                        except (json.JSONDecodeError, KeyError, ValueError) as parse_err:
                            log.warning(f"Failed to parse RAG metadata: {parse_err}")
                        finally:
                            # Remove RAG markers from buffer
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
                                if first_content and buffer[:safe_idx].strip():
                                    self.output.remove_typing_indicator()
                                    first_content = False
                                self.output.append(buffer[:safe_idx])
                                buffer = buffer[safe_idx:]
                            elif not has_partial_marker:
                                # No markers at all, flush everything
                                if first_content and buffer.strip():
                                    self.output.remove_typing_indicator()
                                    first_content = False
                                self.output.append(buffer)
                                buffer = ""
                            elif (  # pragma: no cover
                                safe_idx == 0 and len(buffer) > MARKER_BUFFER_OVERFLOW_THRESHOLD
                            ):
                                # Unreachable with the current marker constants: safe_idx can only
                                # be 0 when len(buffer) <= 15 (the longest marker prefix checked
                                # above), which can never exceed MARKER_BUFFER_OVERFLOW_THRESHOLD
                                # (100). Kept as a defensive safeguard in case those constants change.
                                # Entire buffer is potential partial marker but too large, flush it
                                if first_content and buffer.strip():
                                    self.output.remove_typing_indicator()
                                    first_content = False
                                self.output.append(buffer)
                                buffer = ""

                # Flush any remaining buffer
                if buffer:
                    self.output.append(buffer)

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
                            if first_content and buffer[:safe_idx].strip():
                                self.output.remove_typing_indicator()
                                first_content = False
                            self.output.append(buffer[:safe_idx])
                            buffer = buffer[safe_idx:]
                        elif not has_partial_marker:
                            # No markers at all, flush everything
                            if first_content and buffer.strip():
                                self.output.remove_typing_indicator()
                                first_content = False
                            self.output.append(buffer)
                            buffer = ""
                        elif (  # pragma: no cover
                            safe_idx == 0 and len(buffer) > MARKER_BUFFER_OVERFLOW_THRESHOLD
                        ):
                            # Unreachable with the current marker constants: safe_idx can only
                            # be 0 when len(buffer) <= 15 (the longest marker prefix checked
                            # above), which can never exceed MARKER_BUFFER_OVERFLOW_THRESHOLD
                            # (100). Kept as a defensive safeguard in case those constants change.
                            # Entire buffer is potential partial marker but too large, flush it
                            if first_content and buffer.strip():
                                self.output.remove_typing_indicator()
                                first_content = False
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
            # Update status bar to reflect new message count
            await self._update_session_status()
            self._unlock_prompt()


if __name__ == "__main__":  # pragma: no cover
    NyxGPTTUI().run()
