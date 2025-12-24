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

## Streaming chat endpoint

### `POST /api/v1/chat/stream`

Stream a chat response token-by-token as plain text.

This endpoint is functionally equivalent to `/api/v1/chat` but returns the assistant response incrementally as it is generated, which provides a much better user experience for interactive clients.

**Request:**

```json
{
  "prompt": "Write a haiku about streaming",
  "session": "default",
  "model": "llama3.1:8b"
}
```

**Response:**

- HTTP 200
- `Content-Type: text/plain; charset=utf-8`
- Body is streamed incrementally as text chunks

Example using `curl`:

```bash
curl -N http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Write a haiku about streaming","session":"default"}'
```

Notes:

- The connection remains open until generation completes
- Retrieved RAG context (if enabled) is injected *before* streaming begins
- The full response is persisted to the session once streaming completes

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
