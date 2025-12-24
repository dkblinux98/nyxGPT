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

## Sessions endpoints

Sessions store conversation history and metadata and are persisted automatically on disk.

### `GET /api/v1/sessions`

List all known sessions.

**Response:**

```json
{
  "sessions": [
    {
      "name": "default",
      "messages": 14,
      "modified": "2025-12-23 23:35:32",
      "pinned": false,
      "tags": [],
      "title": "",
      "summary": "",
      "token_estimate": 228,
      "model": "llama3.1:8b"
    }
  ]
}
```

---

### `POST /api/v1/sessions/init`

Initialize a session. This endpoint is **idempotent** and does not invoke the model.

**Request:**

```json
{
  "name": "my-session"
}
```

**Response:**

```json
{ "status": "ok", "session": "my-session" }
```

---

### `GET /api/v1/sessions/{name}`

Retrieve a single session, including messages and metadata.

**Response (abridged):**

```json
{
  "name": "my-session",
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi!"}
  ],
  "meta": {
    "pinned": false,
    "tags": [],
    "title": "",
    "summary": ""
  }
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

### RAG prompt & context optimization

When RAG-assisted chat is enabled, retrieved context injected into the prompt is governed by configuration settings in `~/.myGPT/config.ini` under the `[rag]` section.

Key controls include:

- `enable_chat_context`: turn RAG injection on or off for chat
- `chat_top_k`: number of candidate chunks retrieved from the vector store
- `min_score`: minimum similarity score required for a chunk to be included
- `max_chunks`: hard cap on the number of chunks injected
- `chat_context_max_chars`: maximum total character budget for injected context
- `dedupe`: remove duplicate or near-duplicate chunks

These controls allow you to balance answer quality, latency, and prompt size. Sensible defaults are provided; see `example.config.ini` for recommended values.

---

## Streaming chat endpoint

### `POST /api/v1/chat/stream`

Stream a chat response token-by-token as plain text.

This endpoint is functionally equivalent to `/api/v1/chat` but returns the assistant response incrementally as it is generated, providing a much better user experience for interactive clients such as a TUI or web UI.

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

## Authentication (optional)

Authentication scaffolding exists and is **disabled by default**.

When enabled via `~/.myGPT/config.ini`:

- All API requests must include a shared API key header
- Requests missing or providing an invalid key will return `401 Unauthorized`

### Configuration

```ini
[auth]
enabled = true
api_key = your-secret-key
header = X-API-Key
```

### Example

```bash
curl http://127.0.0.1:8000/api/v1/info \
  -H "X-API-Key: your-secret-key"
```

---

## Error handling

- All errors return JSON
- HTTP status codes are used consistently
- Internal errors are logged to `~/.myGPT/logs/mygpt.log`

---

## Notes

- The API is **not intended to be exposed publicly**.
- HTTPS termination is expected to be handled externally if ever needed.
- Streaming responses are supported via `/api/v1/chat/stream`.
