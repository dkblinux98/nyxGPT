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

### Run as a background service (recommended)

The API can be run persistently using **Homebrew services**, similar to Ollama.

Once installed and started via Homebrew:

```bash
brew services start mygpt-api
brew services info mygpt-api
```

The API will:
- start automatically at login
- run in the background
- log output via Homebrew (e.g. `var/log/mygpt-api.log`)

To stop or restart:

```bash
brew services stop mygpt-api
brew services restart mygpt-api
```

## Tools

`mygpt` includes a few explicit, user-invoked local filesystem tools (no agentic behavior):

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