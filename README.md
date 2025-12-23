# myGPT

A local, private "ChatGPT-style" project scaffold.

## Requirements

- Python (project currently uses a local `.venv`)

## Configuration

Runtime configuration is stored **outside** the repository:

- Real config: `~/.myGPT/config.ini`
- Template (checked in): `example.config.ini`


Create your local config:

```bash
mkdir -p ~/.myGPT
cp example.config.ini ~/.myGPT/config.ini
chmod 600 ~/.myGPT/config.ini
```

### Config keys

The following sections are supported in `~/.myGPT/config.ini`:

#### `[mygpt]`
- `default_model` — default Ollama model to use for chat and summaries
- `sessions_dir` — directory where session JSON and metadata files are stored
- `vectorstore_dir` — directory where future RAG / vector data will be stored

#### `[ollama]`
- `base_url` — Ollama API base URL (usually `http://127.0.0.1:11434`)


#### `[api]`
- `host` — interface the FastAPI server binds to
- `port` — port the FastAPI server listens on

#### `[logging]`
- `level` — log level for the FastAPI backend (`DEBUG`, `INFO`, `WARNING`, `ERROR`)

The log level is read at API startup from `config.ini` and controls verbosity for:
- FastAPI request handling
- startup diagnostics
- unhandled exception logging

#### `[auth]`
- `enabled` — enable API key authentication for the FastAPI backend (default: `false`)
- `api_key` — shared secret required when auth is enabled
- `header` — HTTP header name used to pass the API key (default: `X-API-Key`)

When authentication is enabled, all requests to `/api/v1/*` must include the configured API key. The `/health` endpoint and API documentation routes remain unauthenticated.

#### `[rag]`
- `cassandra_hosts` — comma-separated Cassandra hosts (default: `127.0.0.1`)
- `cassandra_port` — Cassandra native transport port (default: `9042`)
- `cassandra_keyspace` — keyspace used for RAG data (default: `mygpt`)
- `cassandra_table` — table used for RAG chunks (default: `rag_chunks`)

- `embedding_model` — Ollama model used for embeddings (defaults to `[mygpt] default_model` if unset)
- `embedding_dim` — embedding vector dimension (must match Cassandra `VECTOR<FLOAT,N>` schema)

Example embedding configuration:

```ini
[rag]
embedding_model = nomic-embed-text
embedding_dim = 768
```

##### Embedding performance tuning

The following settings control embedding performance and stability when ingesting large documents:

- `embedding_batch_size` — number of chunks embedded per request to Ollama. Smaller values are slower but safer; larger values are faster but heavier.
- `embedding_timeout_seconds` — timeout (in seconds) for each embedding batch request.

Recommended defaults (validated locally):

```ini
[rag]
embedding_batch_size = 16
embedding_timeout_seconds = 120
```

- `chunk_size` — maximum characters per chunk when ingesting documents
- `chunk_overlap` — overlapping characters between adjacent chunks

- `top_k` — number of similar chunks retrieved per query

These settings control how documents are chunked, embedded, stored in Cassandra, and retrieved for RAG.

⚠️ **Re-ingest required**

Any change to the following settings requires re-ingesting documents:

- `chunk_size`
- `chunk_overlap`
- `embedding_model`
- `embedding_dim`
- `embedding_batch_size`

Previously ingested chunks will not be automatically migrated.

#### `[paths]`
- `repo_dir` — absolute path to the myGPT repository
- `venv_python` — absolute path to the Python executable used to run the API service

The Homebrew-managed API service reads these values at startup, so moving the repository or virtual environment only requires updating `config.ini`.

## Install (dev / editable)

From the repo root with your venv active:

```bash
pip install -e .
```

## Run

```bash
python -m mygpt
```

Or via the console script:

```bash
mygpt
```

## FastAPI Backend (Local API)

myGPT also exposes a local FastAPI backend, used by future TUIs and web UIs.

### Run manually (development)

With your virtual environment active:

```bash
uvicorn mygpt.app:app --reload --host 127.0.0.1 --port 8000
```

- Health check: http://127.0.0.1:8000/health
- API docs (Swagger UI): http://127.0.0.1:8000/docs
- Versioned API base: http://127.0.0.1:8000/api/v1

### RAG API endpoints

The FastAPI backend exposes RAG functionality over HTTP for use by future TUIs and web UIs.

All endpoints are versioned under `/api/v1` and respect API authentication when enabled.

#### POST `/api/v1/rag/ingest`

Ingest a document into the RAG vector store.

Request body:

```json
{
  "doc_id": "readme-v3",
  "text": "Full document text goes here",
  "metadata": {"source": "README"},
  "ensure_schema": false
}
```

Response:

```json
{
  "doc_id": "readme-v3",
  "chunks_ingested": 17
}
```

#### POST `/api/v1/rag/query`

Retrieve relevant context chunks from the RAG store.

Request body:

```json
{
  "query": "How do I run Cassandra for RAG?",
  "top_k": 3
}
```

Response:

```json
{
  "results": [
    {
      "doc_id": "readme-v3",
      "chunk_id": 4,
      "text": "Apache Cassandra 5.0 supports native vector search using SAI indexes...",
      "score": 0.82
    }
  ]
}
```

When API authentication is enabled, requests to these endpoints must include the configured API key header.

### Run as a background service (recommended)

The API can be run persistently using **Homebrew services**, similar to Ollama. This avoids keeping a terminal open.

### Startup diagnostics

On startup, the FastAPI backend performs a few non-fatal diagnostics and logs the results:

- Ensures the configured sessions directory exists (creates it if missing)
- Reads the configured log level from `[logging] level`
- Performs a **warn-only** connectivity check to Ollama

If Ollama is not reachable at startup, the API will still start and serve requests. Chat requests will fail until Ollama becomes available, but this avoids the API crashing or failing to start under Homebrew.

Startup diagnostics and warnings are written to the Homebrew service logs.

### API authentication (optional)

The FastAPI backend supports optional API-key authentication, intended for cases where the API is exposed beyond localhost or used by external tools.

Authentication is **disabled by default** for local-only usage. To enable it:

1. Set the following in `~/.myGPT/config.ini`:

```ini
[auth]
enabled = true
api_key = your-secret-key
header = X-API-Key
```

2. Restart the API service:

```bash
brew services restart mygpt-api
```

When enabled, requests to `/api/v1/*` must include the configured header:

```http
X-API-Key: your-secret-key
```

If authentication is enabled but no `api_key` is configured, all `/api/v1` requests will be rejected.

#### 1) Create a local Homebrew tap

This creates a local tap (a place where custom formulae live):

```bash
brew tap-new dkblinux98/mygpt-local
```

#### 2) Create the formula file

```bash
TAP_DIR="$(brew --repo dkblinux98/mygpt-local)"
mkdir -p "$TAP_DIR/Formula"
open "$TAP_DIR/Formula/mygpt-api.rb"
```

Because this repository is the source of truth, the Homebrew formula is versioned **inside this repo** at:

```
homebrew/mygpt-api.rb
```

Homebrew services still require the formula to live inside a tap, so we copy it into the tap directory.

1) Copy the formula into the tap:

```bash
cp homebrew/mygpt-api.rb "$TAP_DIR/Formula/mygpt-api.rb"
```

2) Open the file to confirm or adjust version / SHA if needed:

```bash
open "$TAP_DIR/Formula/mygpt-api.rb"
```

#### 3) Install the service formula

```bash
brew install dkblinux98/mygpt-local/mygpt-api
```

If you edit the formula later, reinstall to apply changes:

```bash
brew reinstall --build-from-source dkblinux98/mygpt-local/mygpt-api
```

#### 4) Start the service

```bash
brew services start mygpt-api
brew services info mygpt-api
```

#### 5) Verify it is running

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/info
```

All functional API endpoints are versioned under `/api/v1`. The root `/health` endpoint is intentionally left unversioned so service managers (like Homebrew and launchd) can perform simple health checks without tracking API versions.

#### 6) Logs

Homebrew writes logs under its prefix:

- Stdout: `/usr/local/var/log/mygpt-api.log`
- Stderr: `/usr/local/var/log/mygpt-api.err.log`

Tail logs:

```bash
tail -n 200 /usr/local/var/log/mygpt-api.log
tail -n 200 /usr/local/var/log/mygpt-api.err.log
```

#### 7) Stop or restart

```bash
brew services stop mygpt-api
brew services restart mygpt-api
```

#### 8) Important: config-driven paths

The Homebrew service reads `~/.myGPT/config.ini` at startup. Ensure you have:

- `[api] host` and `[api] port`
- `[paths] repo_dir` and `[paths] venv_python`

If you move the repository or rebuild the virtual environment, update `config.ini` and restart:

```bash
brew services restart mygpt-api
```

## RAG (Retrieval-Augmented Generation)

myGPT will use **Apache Cassandra 5.0** as the vector database for RAG (chunk storage + embeddings + similarity search).

### Why Cassandra (vs. a lightweight local vector DB)

- You already know Cassandra, and Cassandra 5.0 has **native vector search**.
- This keeps chunks, metadata, and embeddings in one place using familiar CQL.
- It scales cleanly later if your RAG store becomes "real infrastructure".

### Run Cassandra 5.0 locally (Docker)

Docker is the recommended local dev setup because it avoids installing and managing Java/Cassandra directly on macOS.

1) Start Cassandra:

```bash
docker run -d --name mygpt-cassandra \
  -p 9042:9042 \
  -e CASSANDRA_CLUSTER_NAME=mygpt \
  cassandra:5.0
```

2) Wait until Cassandra is ready:

```bash
docker logs -f mygpt-cassandra | tail -n 50
```

3) Open `cqlsh`:

```bash
docker exec -it mygpt-cassandra cqlsh
```

### Create keyspace, table, and vector index

In `cqlsh`:

```sql
CREATE KEYSPACE IF NOT EXISTS mygpt
WITH REPLICATION = {'class':'SimpleStrategy','replication_factor':1};

USE mygpt;

-- NOTE: the embedding dimension (768) must match your embedding model.
CREATE TABLE IF NOT EXISTS rag_chunks (
  doc_id text,
  chunk_id int,
  text text,
  metadata text,
  embedding VECTOR<FLOAT, 768>,
  PRIMARY KEY (doc_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS rag_chunks_embedding_sai
ON rag_chunks(embedding) USING 'sai';
```

### Cleaning up old ingests

If you change chunking or embedding settings, delete old chunks before re-ingesting:

```sql
DELETE FROM mygpt.rag_chunks WHERE doc_id = 'your-doc-id';
-- or wipe everything (early development only)
TRUNCATE mygpt.rag_chunks;
```

### Managing ingested RAG documents (CLI)

The CLI provides commands to inspect and manage documents stored in the RAG vector store.

List all ingested documents and their chunk counts:

```bash
mygpt rag list

doc_id                         chunks
----------------------------------------
readme-v3                     17
test-doc                      3

mygpt rag delete readme-v3
mygpt rag wipe --yes-really
```

### Optional: persistent storage

If you want Cassandra data to persist across container recreations, use a Docker volume:

```bash
docker volume create mygpt_cassandra_data

docker rm -f mygpt-cassandra

docker run -d --name mygpt-cassandra \
  -p 9042:9042 \
  -e CASSANDRA_CLUSTER_NAME=mygpt \
  -v mygpt_cassandra_data:/var/lib/cassandra \
  cassandra:5.0
```

### Notes

- Similarity queries will use Cassandra's ANN syntax (vector search).
- We will keep all RAG configuration in `~/.myGPT/config.ini` and document it as the RAG modules are added.

## Tools

`mygpt` includes a few explicit, user-invoked local filesystem tools (no agentic behavior):

These tools are exposed via the CLI only; they are not HTTP endpoints.

```bash
mygpt tools ls PATH
mygpt tools cat PATH [--head N | --tail N]
mygpt tools grep PATTERN PATH [--max N]
```

Examples:

```bash
mygpt tools ls .
mygpt tools cat README.md --head 40
mygpt tools grep "sessions" README.md --max 20
```

## Sessions & Memory

Conversation history is stored **outside the repository** so that no generated data ever lives in the project tree.

- Sessions directory: `~/.myGPT/sessions/`
- Each session is stored as a JSON file: `<session-name>.json`
- Session metadata is stored alongside it: `<session-name>.meta.json`

Examples:

```bash
mygpt chat                    # uses ~/.myGPT/sessions/default.json
mygpt chat --session work     # uses ~/.myGPT/sessions/work.json
mygpt chat --session work --new
```

### Sessions CLI

You can manage stored sessions directly from the command line:

```bash
mygpt sessions                         # list all sessions (shows title/summary/tags/pin)
mygpt sessions show NAME               # show full metadata for a session
mygpt sessions summarize NAME          # generate title/summary/tags using the model
mygpt sessions title NAME "New Title"  # set title manually
mygpt sessions pin NAME                # pin (pinned sessions sort first)
mygpt sessions unpin NAME              # unpin
mygpt sessions tag-add NAME tag1 tag2  # add tags
mygpt sessions tag-rm NAME tag1 tag2   # remove tags
mygpt sessions rename OLD NEW          # rename a session
mygpt sessions delete NAME             # delete a session (and its metadata)
```

Examples:

```bash
mygpt sessions
mygpt sessions summarize default
mygpt sessions pin default
mygpt sessions tag-add default chess training
mygpt sessions show default
mygpt sessions rename default brainstorming
mygpt sessions delete brainstorming
```

Because sessions live outside the repo:
- no `.gitignore` rules are required
- generated data is never committed by accident

## Architecture

Core logic is intentionally split into small, reusable modules:

- `cli.py` — command-line parsing and dispatch only
- `ollama_client.py` — Ollama HTTP and chat interface
- `sessions.py` — session storage, metadata, summaries, tagging, pinning
- `tools_fs.py` — explicit filesystem tools (`ls`, `cat`, `grep`)
- `app.py` — reserved for FastAPI backend (shared by TUI and web UI)

This separation allows the same core logic to be reused by:
- CLI
- future TUI
- local web UI (FastAPI + React/Next.js)

## Notes

- Do **not** commit generated packaging metadata like `*.egg-info/`.
- Project name (distribution) is `myGPT`; the importable Python package is `mygpt`.