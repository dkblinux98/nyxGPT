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