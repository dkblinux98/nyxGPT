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
from textual.widgets import Header, Footer, Static, Input
from textual.containers import Vertical

from mygpt.config import load_config

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


class MyGPTTUI(App):
    CSS_PATH = None
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self, session: str = "default", api_base_url: Optional[str] = None) -> None:
        super().__init__()
        cfg = load_config(None)
        self.session = session
        self.api_base_url = api_base_url or cfg.get("api", "base_url", fallback="http://127.0.0.1:8000")

        log.info("TUI initialized", extra={"session": session, "api": self.api_base_url})

    def _unlock_prompt(self) -> None:
        try:
            self.prompt.disabled = False
            self.prompt.focus()
        except Exception:
            # Best effort; Textual may be shutting down
            pass

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            self.output = ChatOutput()
            yield self.output
            self.prompt = Input(placeholder="Type a message and press Enter")
            yield self.prompt
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        # Prevent double-submits while a stream is active
        self.prompt.value = ""
        self.prompt.disabled = True

        self.output.clear()
        self.output.append("→ " + text + "\n\n")

        # Run streaming in the background so the UI stays responsive.
        asyncio.create_task(self._stream_chat(text))

    async def _stream_chat(self, prompt: str) -> None:
        url = f"{self.api_base_url}/api/v1/chat/stream"

        payload = {
            "session": self.session,
            "prompt": prompt,
        }

        log.debug("Sending chat request", extra={"session": self.session})

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