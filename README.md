# myGPT

**myGPT** is a local-first, private, extensible ChatGPT-style system designed to run entirely on your own machine.

It uses **Ollama** for local LLM inference, supports persistent **sessions**, optional **Retrieval-Augmented Generation (RAG)** backed by **Apache Cassandra**, a powerful **CLI**, and a **FastAPI backend** intended to support future terminal and web UIs.

---

## Why myGPT?

- No cloud dependency by default
- Your data never leaves your machine
- Clear separation between CLI, API, and core logic
- Designed for experimentation, learning, and extension

---

## Key features

- Local LLM inference via Ollama
- Persistent conversation sessions stored outside the repo
- Optional RAG using Cassandra 5.0 native vector search
- Shared core logic between CLI and FastAPI backend
- Robust unit and integration test suite
- Architecture designed for future TUI and React/Next.js UI

---

## Quick start

### Requirements

- Python 3.11 or newer
- Ollama running locally
- (Optional) Docker for Cassandra (RAG support)

---

### Install (development / editable)

From the repository root, with your virtual environment active:

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

Edit `~/.myGPT/config.ini` to match your environment (models, logging, RAG, etc.).

---

## Running myGPT

### CLI

```bash
mygpt chat "Hello"
```

Sessions, logs, and other runtime artifacts are stored under:

```
~/.myGPT/
```

---

### FastAPI backend (recommended via Homebrew)

Start the API as a background service:

```bash
brew services start mygpt-api
```

Verify it is running:

```bash
curl http://127.0.0.1:8000/health
```

---

## Documentation

Detailed documentation is organized under the `docs/` directory:

- **Configuration** – `docs/configuration.md`
- **API** – `docs/api.md`
- **RAG (Cassandra + Docker)** – `docs/rag.md`
- **Sessions & Memory** – `docs/sessions.md`
- **Testing** – `docs/testing.md`
- **Homebrew Service** – `docs/homebrew.md`
- **Architecture** – `docs/architecture.md`

If you are new to the project, start with **architecture** and **configuration**.

---

## Project notes

- Runtime data (sessions, logs, embeddings) never lives in the git repository
- The distribution name is **myGPT**; the Python package name is **mygpt**
- Build artifacts such as `*.egg-info/` should not be committed

---

## Status

myGPT is under active development. The architecture is stable; features such as streaming responses and user interfaces are planned next.
