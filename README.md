# myGPT

**myGPT** is a local-first, private, extensible ChatGPT-style system designed to run entirely on your own machine.

It uses **Ollama** for local LLM inference, supports persistent **conversation sessions**, optional **Retrieval‑Augmented Generation (RAG)** backed by **Apache Cassandra**, a powerful **CLI**, a **FastAPI backend**, a rich **terminal UI (TUI)**, and a lightweight **local web UI** built with Next.js.

Your data stays on your machine. No cloud dependency is required.

---

## Why myGPT?

- Local‑only by default (no cloud calls)
- Your prompts, sessions, and embeddings never leave your machine
- Clear separation between CLI, API, UI, and core logic
- Designed for experimentation, learning, and extension
- Production‑like ops tooling for a local system

---

## Key features

- Local LLM inference via **Ollama**
- Persistent sessions stored outside the repository
- Optional **RAG** using Cassandra 5.0 native vector search
- Config‑driven RAG context pruning and prompt optimization
- Streaming responses (CLI, TUI, API, Web UI)
- Unified core shared between CLI and FastAPI
- Optional **API rate limiting** (disabled by default for localhost use)
- Homebrew‑managed background services
- Robust unit and integration test suite

---

## Quick start

### Requirements

- Python 3.11+
- Ollama
- Homebrew
- Docker Desktop (required for Cassandra / RAG)
- Node.js (for the local web UI)

---

### Install (development / editable)

From the repository root with your virtual environment active:

```bash
pip install -e .
```

---

### Configuration

All runtime configuration lives **outside the repository**:

```bash
mkdir -p ~/.myGPT
cp example.config.ini ~/.myGPT/config.ini
chmod 600 ~/.myGPT/config.ini
```

Edit `~/.myGPT/config.ini` to select models, logging options, RAG settings, and service paths.

---

## Running myGPT

### One‑command setup (recommended)

Install and configure all local services (API, web UI, logs, Cassandra helpers):

```bash
mygpt ops install
```

Check system health:

```bash
mygpt ops doctor
```

---

### CLI

```bash
mygpt chat "Hello"
```

---

### Terminal UI (TUI)

```bash
mygpt tui
```

The TUI streams responses, persists sessions, and supports RAG‑assisted chat.

---

### FastAPI backend

The API service is managed via the `mygpt ops` command. Start all services (including the API):

```bash
mygpt ops install
```

Or restart just the API:

```bash
mygpt ops restart api
```

Verify:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/v1/info
```

Interactive API docs (local only):

```bash
open http://127.0.0.1:8000/docs
```

#### Rate limiting

The FastAPI backend includes optional rate limiting to protect against abuse and DoS attacks. **Disabled by default** for localhost-only usage.

To enable rate limiting, edit `~/.myGPT/config.ini`:

```ini
[rate_limit]
enabled = true
requests_per_second = 10
burst_size = 20
```

Rate limiting uses a token bucket algorithm to track requests per IP address. When enabled, all API responses include rate limit headers:

- `X-RateLimit-Limit` – Maximum requests allowed
- `X-RateLimit-Remaining` – Remaining requests in current window
- `X-RateLimit-Reset` – Unix timestamp when limit resets

If the limit is exceeded, the API returns a `429 Too Many Requests` error:

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Too many requests. Please try again later.",
    "request_id": "..."
  }
}
```

---

### Local Web UI (Next.js)

The web UI is managed via the `mygpt ops` command:

```bash
mygpt ops restart web
```

Open in your browser:

```bash
open http://127.0.0.1:3000
```

The web UI connects to FastAPI and supports streaming chat and session browsing.

---

### RAG (Retrieval-Augmented Generation) Controls

myGPT supports per-session RAG to inject relevant context from uploaded documents into chat conversations.

**Supported file types:** `.txt`, `.md` (with frontmatter parsing), `.json`, `.pdf`

#### Web UI

Use the RAG controls in the chat interface (left of the message input):
- **RAG Toggle** button to enable/disable RAG for the current session
- **File Upload** to ingest documents into the RAG database
- RAG status displays current state (ON/OFF)

#### Terminal UI (TUI)

Press `Ctrl+R` to toggle RAG on/off for the current session. The RAG status is displayed in the UI.

#### CLI / API

Enable RAG globally via config (`~/.myGPT/config.ini`):

```ini
[rag]
enable_chat_context = true
```

Or override per-request via the API:

```json
{
  "session": "my-session",
  "prompt": "Your question here",
  "rag_enabled": true
}
```

Upload documents via API:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/upload \
  -F "file=@document.md"
```

**Priority chain:** Explicit API parameter > Session metadata > Global config

---

## Logs & runtime data

All runtime state lives under:

```text
~/.myGPT/
```

Including:

- `sessions/` – conversation sessions
- `logs/` – API, web UI, Ollama, and Cassandra logs
- `scripts/` – service wrapper scripts

No runtime data is stored in the git repository.

---

## Documentation

Detailed documentation is organized under `docs/`:

- **API & Ops** – `docs/api.md`
- **UI (TUI + Web)** – `docs/ui.md`
- **RAG & Cassandra** – `docs/rag.md`
- **Sessions & Memory** – `docs/sessions.md`
- **Performance Tuning** – `docs/performance.md`
- **Testing** – `docs/testing.md`
- **Architecture** – `docs/architecture.md`
- **Troubleshooting** – `docs/troubleshooting.md`

If you are new to the project, start with **architecture**, then **api**, then **ui**.

---

## Project notes

- Distribution name: **myGPT**
- Python package name: **mygpt**
- Runtime data is always externalized
- Build artifacts such as `*.egg-info/` must not be committed

---

## Status

The core architecture, ops tooling, streaming, TUI, web UI, and RAG foundations are complete.

Future work focuses on:
- UX refinement
- performance tuning
- richer session metadata and search
- optional multi‑user and auth extensions

