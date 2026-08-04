# Architecture

This document describes the high-level architecture of **nyxGPT**, how its components fit together, and the design principles guiding the project.

---

## Design goals

nyxGPT is designed to be:

- **Local-first** — runs entirely on your machine by default
- **Private** — no required external APIs or cloud services
- **Composable** — clear separation between CLI, API, and future UIs
- **Extensible** — easy to add features such as RAG, streaming, or new UIs
- **Testable** — strong separation of concerns enables robust unit and integration tests

---

## Architectural invariants

These are the constraints agents and contributors must not violate (formerly
the root `ARCHITECTURE.md`; this section is now the single source of truth).

### Core invariants
- CLI remains functional and is a first-class interface.
- FastAPI is the stable integration surface for UIs/automation.
- UIs (web) are clients; they must not become required for core operation.
- Model runtime is pluggable behind stable interfaces.
- Persistence is explicit and configurable.

### Dependency flow
UI -> API -> domain -> adapters (IO)
- IO (HTTP, filesystem, DB, LLM calls) must be isolated behind interfaces.
- No "god modules"; keep boundaries clear and testable.

### Branching
- See `CLAUDE.md` (Branch Rules) — the authority on branch/merge policy.

### Config & secrets
- No secrets committed to the repo.
- New required external dependencies require human approval.

### Quality
- New features require appropriate tests (unit and/or integration).
- CI must be green prior to merge (unless human explicitly authorizes an exception).

---

## High-level components

```
+------------------+
|  CLI / Clients  |
|  (nyxgpt chat)   |
+--------+---------+
         |
         v
+------------------+
|   FastAPI API   |
|  (nyxgpt.app)   |
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

The CLI (`nyxgpt`) is the primary user interface today.

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

The FastAPI application (`nyxgpt.app`) provides a local HTTP interface over the same core services used by the CLI.

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
- Logs are written to `~/.nyxGPT/logs`

---

## Observability

Metrics, logs, traces, and error tracking are four separate, local-only
opt-in Compose profiles (`monitoring`, `logging`, `tracing`, `errors`) --
nothing is ever sent to an external/cloud endpoint. See
[docs/docker-compose.md](docker-compose.md) for the full per-profile detail;
this section covers how the pieces correlate across the API and web tiers
(#3430):

- **Metrics**: Prometheus scrapes the API's `/metrics`; Grafana visualizes.
  Not migrated to OTLP metrics -- Prometheus stays the metrics pipeline.
- **Logs**: both the API (`nyxgpt.logging`) and the web tier
  (`web/src/lib/logger.ts`) emit the same line shape -- UTC timestamp,
  level, `[request_id]`, logger name, message -- so promtail's single regex
  (`docker/promtail-config.yml`) extracts `level`/`logger` labels from
  either tier's output identically. Not migrated to an OTLP logs pipeline
  -- file + promtail → Loki stays the logs pipeline.
- **Traces**: OpenTelemetry is the correlation backbone, not a metrics or
  logs replacement. W3C `traceparent` propagates browser → Next.js server
  → FastAPI → Ollama (browser `WebTracerProvider`, Next.js `@vercel/otel`,
  Python `FastAPIInstrumentor`/`URLLibInstrumentor`), so one chat request
  is one trace in Jaeger end to end. `request_id` (the human-facing id in
  logs and the `X-Request-Id` header) is derived from the active trace id
  on both tiers when present, so it isn't a second, disconnected identifier
  -- and `trace_id=`/`span_id=` appended to log lines drive Grafana's
  Loki→Jaeger derived-field navigation.
- **Errors**: the Python API (`sentry_sdk`) and the web tier
  (`@sentry/nextjs`, browser + Next.js server) both report into the same
  self-hosted GlitchTip instance, independently of the tracing SDKs (each
  side's Sentry setup runs with tracing disabled to avoid two libraries
  contending for the global TracerProvider).
- **Ops/self-heal correlation**: `nyxgpt ops`/`canary` CLI invocations and
  autonomous self-heal restarts mint a correlation id
  (`NYXGPT_CORRELATION_ID`) that every subprocess they spawn inherits via
  the environment, and that the `#3390` ops-lifecycle events / self-heal
  `HealEvent` log carry -- joining "what command/heal attempt caused this
  restart" independently of (and in addition to) the trace/request id.

Every piece above degrades gracefully by design: with the observability
stack not yet up (fresh install, `--skip-observability`, a collector
restart), spans/logs/errors are dropped rather than blocking a request, and
`nyxgpt ops doctor` is the diagnostic surface for "enabled but not actually
reaching anything."

---

## Testing architecture

- **Unit tests** exercise core services in isolation
- **Integration tests** validate end-to-end behavior with real services
- Shared logging makes failures easier to diagnose

### Code coverage

Code coverage is collected using `pytest-cov` (a pytest plugin for `coverage.py`).

**How it works:**

- `coverage.py` uses **sqlite3** as an embedded database to store coverage data
- Coverage data is written to `.coverage` (a SQLite database file)
- No service startup, configuration, or installation required

**Key technical details:**

- **sqlite3 is part of Python's standard library** — included by default with Python
- **Embedded database** — no daemon, server process, or configuration files
- **Direct file access** — coverage.py opens `.coverage` file directly using `sqlite3.connect()`
- **No `nyxgpt ops` integration needed** — works out of the box

**What is the `.coverage` file?**

- SQLite 3.x database created by `coverage.py`
- Stores line-by-line execution data for each Python file
- Used to generate coverage reports via `pytest --cov`
- Safe to delete (will be regenerated on next test run)

---

## Implemented Extensions

The following features have been successfully implemented:

### Streaming responses

**Status:** Fully implemented and production-ready.

Token-by-token streaming is available across all interfaces:
- Core chat logic (`chat_stream`)
- CLI (default behavior)
- FastAPI (`/api/v1/chat/stream`)

Streaming includes:
- RAG context injection before generation
- Incremental text chunk delivery
- Full session persistence after streaming completes
- RAG metadata in first chunk (when enabled)

### Web UI (Next.js)

**Status:** Fully implemented and available.

The Next.js web UI provides:
- Modern React-based interface
- Real-time streaming chat
- Session management and organization
- RAG document upload and management
- Comprehensive settings and configuration
- Runs as a background service via Homebrew (macOS) or systemd (Linux)

Access at `http://127.0.0.1:3000` after starting the service.

---
## Proposed / future architecture

Proposed extensions and forward-looking architecture discussion live in
[`product_management/PROPOSED_ARCHITECTURE.md`](../product_management/archived_product_docs/PROPOSED_ARCHITECTURE.md).
