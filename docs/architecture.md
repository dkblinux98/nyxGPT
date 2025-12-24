# Architecture

This document describes the high-level architecture of **myGPT**, how its components fit together, and the design principles guiding the project.

---

## Design goals

myGPT is designed to be:

- **Local-first** — runs entirely on your machine by default
- **Private** — no required external APIs or cloud services
- **Composable** — clear separation between CLI, API, and future UIs
- **Extensible** — easy to add features such as RAG, streaming, or new UIs
- **Testable** — strong separation of concerns enables robust unit and integration tests

---

## High-level components

```
+------------------+
|  CLI / Clients  |
|  (mygpt chat)   |
+--------+---------+
         |
         v
+------------------+
|   FastAPI API   |
|  (mygpt.app)   |
+--------+---------+
         |
         v
+------------------+
|  Core Services  |
|  chat / rag /   |
|  sessions       |
+--------+---------+
         |
         v
+------------------+
| External Local   |
| Services         |
| - Ollama         |
| - Cassandra      |
+------------------+
```

---

## CLI

The CLI (`mygpt`) is the primary user interface today.

Responsibilities:
- Parse user input
- Load configuration
- Initialize logging
- Invoke core services (chat, sessions, RAG)

The CLI communicates:
- **Directly** with core logic for chat and sessions
- **Indirectly** with Ollama and Cassandra through shared client modules

---

## FastAPI backend

The FastAPI application (`mygpt.app`) provides a local HTTP interface over the same core services used by the CLI.

Responsibilities:
- Expose chat and RAG endpoints
- Manage request/response schemas
- Handle background service lifecycle
- Provide a stable interface for future UIs

The API is:
- versioned (`/api/v1`)
- local-only by default
- designed to be run as a background service

---

## Core services

Core logic lives in reusable modules and is shared by CLI and API.

### Chat

- Prompt assembly
- Optional RAG context injection
- Calls Ollama via HTTP
- Session persistence

#### Streaming responses

Chat supports **token-by-token streaming** as an optional execution mode.

Streaming flow:

- Prompt assembly and optional RAG context injection occur **before** generation
- Ollama is called with streaming enabled
- Text chunks are yielded incrementally
- The full assistant reply is assembled internally
- Session persistence happens **after** streaming completes

Streaming is exposed consistently across:

- Core chat logic (`chat_stream`)
- CLI (default behavior)
- FastAPI (`/api/v1/chat/stream`)

### Sessions

- File-based session storage
- Message history management
- Metadata handling
- Validation and safety checks

### RAG

- Document chunking
- Embedding generation
- Vector storage in Cassandra
- Retrieval and context assembly

---

## External services

### Ollama

- Provides LLM inference and embedding generation
- Runs locally
- Accessed via HTTP API

### Cassandra

- Used as a vector database
- Stores embeddings and text chunks
- Queried via native vector search (SAI)
- Runs locally via Docker

---

## Configuration & logging

- All components load configuration from a single INI file
- Logging is centralized and shared across CLI, API, and tests
- Logs are written to `~/.myGPT/logs`

---

## Testing architecture

- **Unit tests** exercise core services in isolation
- **Integration tests** validate end-to-end behavior with real services
- Shared logging makes failures easier to diagnose

---

## Future extensions

The architecture intentionally supports:

- Streaming responses
- Web UI (React / Next.js)
- Terminal UI (TUI)
- Pluggable memory backends
- Additional vector databases

No architectural changes are required to add these features.
