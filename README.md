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

The API is managed as a Homebrew service:

```bash
brew services start mygpt-api
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

---

### Local Web UI (Next.js)

The web UI is also managed via Homebrew:

```bash
brew services start mygpt-web
```

Open in your browser:

```bash
open http://127.0.0.1:3000
```

The web UI connects to FastAPI and supports streaming chat and session browsing.

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
- **Testing** – `docs/testing.md`
- **Architecture** – `docs/architecture.md`

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

