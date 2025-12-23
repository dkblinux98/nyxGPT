# FastAPI Backend

myGPT provides a local FastAPI backend that exposes chat, RAG, session, and health endpoints. The API is intended for:

- the CLI (primary consumer today)
- future local TUI and web UI
- debugging and integration testing

The API is designed to run **locally only** by default.

---

## Running the API

### Development (manual)

```bash
mygpt api
```

or, if running directly:

```bash
uvicorn mygpt.app:app --reload
```

### As a background service (Homebrew)

The recommended way to run the API persistently is via Homebrew services.

See: [`docs/homebrew.md`](homebrew.md)

---

## Base URL

By default, the API listens on:

```
http://127.0.0.1:8000
```

Configuration lives in `~/.myGPT/config.ini` under the `[api]` section.

---

## Health & diagnostics

### `GET /health`

Simple health check.

**Response:**

```json
{ "status": "ok" }
```

Used by:
- integration tests
- service monitors

---

## Versioned API

All functional endpoints live under:

```
/api/v1
```

---

## Info endpoint

### `GET /api/v1/info`

Returns basic runtime configuration details.

**Response:**

```json
{
  "ollama_base_url": "http://127.0.0.1:11434",
  "default_model": "llama3.1:8b",
  "sessions_dir": "/Users/you/.myGPT/sessions"
}
```

---

## Chat endpoint

### `POST /api/v1/chat`

Send a chat prompt and receive a model response.

**Request:**

```json
{
  "prompt": "Hello",
  "session": "default",
  "model": "llama3.1:8b"
}
```

**Response:**

```json
{
  "reply": "Hello! How can I help you today?",
  "session": "default"
}
```

- Sessions are persisted automatically.
- RAG context may be injected depending on configuration.

---

## RAG endpoints

RAG-related endpoints are documented in detail in:

➡️ [`docs/rag.md`](rag.md)

At a high level, the API supports:

- document ingestion
- vector search / retrieval
- RAG-assisted chat

---

## Authentication (future)

Authentication scaffolding exists but is disabled by default.

Planned options:
- local API tokens
- loopback-only enforcement

No authentication is required in the current local-only setup.

---

## Error handling

- All errors return JSON
- HTTP status codes are used consistently
- Internal errors are logged to `~/.myGPT/logs/mygpt.log`

---

## Notes

- The API is **not intended to be exposed publicly**.
- HTTPS termination is expected to be handled externally if ever needed.
- Streaming responses will be added in a future phase.
