# UI

This document describes the local UI surfaces provided by myGPT:
- a terminal-based UI (TUI)
- a local web UI backed by FastAPI

Both UIs depend on the FastAPI backend and its streaming chat endpoints.

## Run the backend (FastAPI)

The backend is normally run as a background service via Homebrew:

```bash
brew services start mygpt-api
```

Verify it is running before starting any UI.

### Verify

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/info
```

Interactive docs (local only):

```bash
open http://127.0.0.1:8000/docs
```

## Terminal UI (TUI)

Start the terminal UI:

```bash
mygpt tui
```

The TUI:
- streams assistant responses token-by-token
- uses the Sessions API to persist history
- defaults to the `default` session

If no backend is running, the TUI will fail to connect.

## Sessions API (UI-critical)

- List sessions (returns `{ "sessions": [...] }`):

```bash
curl -s http://127.0.0.1:8000/api/v1/sessions
```

- Initialize a session (no model call; safe for UI bootstrapping; idempotent):

## Streaming chat (UI critical)

Both the TUI and web UI rely on the streaming endpoint:

POST `/api/v1/chat/stream`

This endpoint:
- yields text chunks incrementally
- persists messages to the active session
- optionally injects RAG context before streaming

UI implementations must treat the response as a stream, not a single JSON payload.

## Operational dependencies

For reliable UI operation:

- Docker Desktop must be running at login
- the Cassandra container must be running
- the FastAPI service must be active
- logs should be available under `~/.myGPT/logs`

See `docs/api.md → Operational Tasks` for setup details.

# UI

This document describes the local UI surfaces provided by **myGPT**:

- **Terminal UI (TUI)** — a rich terminal-based chat interface
- **Local Web UI** — a lightweight Next.js application backed by FastAPI

Both UIs depend on the FastAPI backend and its streaming chat endpoints.

---

## Backend requirement (FastAPI)

Both UIs require the FastAPI backend to be running.

The backend is normally managed as a Homebrew service:

```bash
brew services start mygpt-api
```

Verify it is running before starting any UI:

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
{ "sessions": [ ... ] }
```

- **Initialize a session**

Session creation is idempotent and does **not** trigger a model call. This allows UI bootstrapping without side effects.

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

The local web UI is a small Next.js application located in `web/`.

### Running via Homebrew (recommended)

The web UI can be launched as a background service using Homebrew:

```bash
brew services start mygpt-web
```

This uses a wrapper script that ultimately runs:

```bash
~/.myGPT/scripts/run-web.sh
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

### PATH and Node resolution

Homebrew / launchd services run with a minimal `PATH`.

To ensure reliability:

- `mygpt-web` sets a safe PATH in its launch wrapper
- `run-web.sh` explicitly ensures `node` and `npm` are discoverable using
  `[paths] node_bin` and `npm_bin` from `~/.myGPT/config.ini`

---

## Operational dependencies

For reliable UI operation, ensure the following are active at login:

- **Docker Desktop** (required for Cassandra)
- **Cassandra container** (`mygpt-cassandra`)
- **FastAPI backend** (`mygpt-api`)
- **Web UI service** (`mygpt-web`)

Logs from all components should be available under:

```text
~/.myGPT/logs
```

For installation, startup, and diagnostics, see:

> **docs/api.md → Operational Tasks**