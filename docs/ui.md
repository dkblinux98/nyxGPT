# UI

This document describes the local UI surfaces provided by **myGPT**:

- **Terminal UI (TUI)** — a rich terminal-based chat interface
- **Local Web UI** — a lightweight Next.js application backed by FastAPI

Both UIs depend on the FastAPI backend and its streaming chat endpoints.

---

## Backend requirement (FastAPI)

Both UIs require the FastAPI backend to be running.

The backend is normally managed via the `mygpt ops` command:

```bash
# Install and start all services (including API)
mygpt ops install

# Restart just the API service
mygpt ops restart api

# Check system health
mygpt ops doctor
```

Verify the API is running:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/info
```

Interactive API docs (local only):

```bash
open http://127.0.0.1:8000/docs
```

---

## Terminal UI (TUI)

Start the terminal UI with:

```bash
mygpt tui
```

The TUI:

- streams assistant responses token-by-token
- persists conversations via the Sessions API
- defaults to the `default` session
- supports RAG-assisted chat if enabled

### TUI Keyboard Shortcuts

- **Ctrl+C** — Quit the TUI
- **Ctrl+S** — Open session picker (browse and switch sessions)
- **Ctrl+R** — Toggle RAG for current session
- **Ctrl+M** — Manage models
- **Ctrl+N** — Rename current session

### Session Picker

Press **Ctrl+S** to open the interactive session picker which allows you to:

- Browse all available sessions
- Search sessions by name, title, summary, or tags
- View session metadata (message count, last modified, tags, summary)
- Navigate with arrow keys (Up/Down) or keyboard search
- Press **Enter** to switch to the selected session
- Press **Escape** or **Ctrl+C** to cancel

Pinned sessions are displayed with a 📌 icon and appear at the top of the list.

If the FastAPI backend is not running, the TUI will fail to connect.

---

## Sessions API (UI-critical)

Both UIs depend on session primitives for listing and persisting conversations.

- **List sessions**

```bash
curl -s http://127.0.0.1:8000/api/v1/sessions
```

Response shape:

```json
{
  "sessions": [
    {
      "name": "session-name",
      "title": "Session Title",
      "message_count": 10,
      "last_modified": "2026-01-12T12:00:00Z"
    }
  ]
}
```

- **Initialize a session**

Session creation is idempotent and does **not** trigger a model call. This allows UI bootstrapping without side effects.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"name": "my-session"}'
```

---

## Streaming chat (UI-critical)

Both the TUI and the web UI rely on the streaming endpoint:

```
POST /api/v1/chat/stream
```

This endpoint:

- yields text chunks incrementally
- persists assistant and user messages to the active session
- optionally injects RAG context before streaming begins

**Important:**
UI clients must treat this response as a stream, not as a single JSON payload.

---

## Local Web UI (Next.js)

The local web UI is a Next.js application located in `web/`.

### Running via mygpt ops (recommended)

The web UI can be launched via the `mygpt ops` command:

```bash
# Install and start all services (including web UI)
mygpt ops install

# Restart just the web UI
mygpt ops restart web

# Check system health
mygpt ops doctor
```

Once running, open:

```bash
open http://127.0.0.1:3000
```

### Configuration

The web UI reads its runtime configuration from:

```text
web/.env.local
```

Typical values:

```env
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

### Web UI Features

The web UI includes:

- **Chat interface** with streaming responses and session management
- **Model management** page (`/models`) for pulling, deleting, and viewing Ollama models
- **Configuration wizard** (`/admin`) for step-by-step system setup
- **Log viewer** (`/admin/logs`) for viewing and searching application logs

#### Toast Notifications

The web UI includes a toast notification system for user feedback:

- **Success notifications** — Confirm successful operations (session creation, model pull, etc.)
- **Error notifications** — Display error messages with context
- **Warning notifications** — Show warnings and non-critical issues
- **Info notifications** — Provide informational messages

Toasts appear in the bottom-right corner, auto-dismiss after 5 seconds (configurable), and can be manually dismissed by clicking the × button. Multiple toasts stack vertically.

#### Configuration Wizard

Access the wizard at `http://127.0.0.1:3000/admin` to configure:

1. **Model Selection** — Choose your default LLM model
2. **RAG Configuration** — Enable/disable retrieval-augmented generation
3. **API Settings** — Configure log level and test connectivity
4. **Summary** — Review and save your configuration

Keyboard shortcuts:
- `←` / `→` — Navigate between steps
- `Enter` — Advance to next step or save configuration

#### Log Viewer

Access the log viewer at `http://127.0.0.1:3000/admin/logs` to:

- **View log files** — Browse all available log files (main log and rotated backups)
- **Real-time filtering** — Filter by log level (DEBUG, INFO, WARNING, ERROR)
- **Search** — Search for specific text across log entries (case-insensitive)
- **Tail mode** — View the last N lines of a log file
- **Auto-refresh** — Automatically reload logs at configurable intervals (1-60 seconds)
- **Download** — Download log files for offline analysis
- **Auto-scroll** — Automatically scroll to the newest log entries

The log viewer provides a dark-themed, monospaced display optimized for reading structured log files.

---

## Operational dependencies

For reliable UI operation, ensure the following are active:

- **Docker Desktop** (required for Cassandra)
- **Cassandra container** (`mygpt-cassandra`)
- **FastAPI backend** (`mygpt-api`)
- **Web UI service** (`mygpt-web`)

Logs from all components are available under:

```text
~/.myGPT/logs
```

For installation, startup, and diagnostics, see:

> **docs/api.md → Operational Tasks**
