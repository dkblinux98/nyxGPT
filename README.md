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
- **Message editing and regeneration** - Edit messages and fork conversations, regenerate responses
- **Message search** - Full-text search across all sessions with filters for role, session, and case-sensitivity
- **Automatic session naming** with LLM‑generated titles and smart filename sync
- **Session management** with right-click context menus, rename, export, delete, and pin
- Optional **RAG** using Cassandra 5.0 native vector search
- **Per‑session RAG controls** via WebUI, TUI, and API
- Config‑driven RAG context pruning and prompt optimization
- Streaming responses (CLI, TUI, API, Web UI)
- Unified core shared between CLI and FastAPI
- Optional **API rate limiting** (disabled by default for localhost use)
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

All runtime configuration lives **outside the repository**.

#### Option 1: Interactive Wizard (Recommended)

Run the interactive configuration wizard for guided setup:

```bash
mygpt wizard
```

The wizard will:
- Test your Ollama connection and detect available models
- Help you select a default model
- Configure RAG settings (optional)
- Generate a production-ready `~/.myGPT/config.ini`

#### Option 2: Manual Configuration

Manually create the config file from the example template:

```bash
mkdir -p ~/.myGPT
cp example.config.ini ~/.myGPT/config.ini
chmod 600 ~/.myGPT/config.ini
```

Edit `~/.myGPT/config.ini` to select models, logging options, RAG settings, and service paths.

---

## Running myGPT

### First-time Setup

1. **Run the configuration wizard** (interactive setup):
   ```bash
   mygpt wizard
   ```

2. **Install services** (API, web UI, logs, Cassandra helpers):
   ```bash
   mygpt ops install
   ```

3. **Check system health**:
   ```bash
   mygpt ops doctor
   ```

---

### CLI

**Chat:**
```bash
mygpt chat "Hello"
```

**Model Management:**
```bash
# List available models
mygpt models list

# Pull (download) a model
mygpt models pull llama3.1:8b

# Delete a model
mygpt models delete mistral:7b

# Show detailed model information
mygpt models show llama3.1:8b
```

**Message Search:**
```bash
# Search across all sessions for message content
mygpt sessions search "Python programming"

# Case-sensitive search
mygpt sessions search "Python" --case-sensitive

# Filter by role (user, assistant, or system)
mygpt sessions search "error" --role user

# Search within a specific session
mygpt sessions search "database" specific-session-name

# Limit number of results
mygpt sessions search "test" --limit 10
```

The search command finds messages containing the query text and displays:
- Session name and title
- Message index and role
- Number of matches per message
- Content preview with surrounding context

**Session Statistics:**
```bash
# View detailed statistics for a session
mygpt sessions stats my-session-name
```

The stats command displays comprehensive session information:
- Message counts (total, by role: user/assistant/system)
- Token estimates (approximate token usage)
- Session age (time since creation)
- Last activity (time since last update)
- RAG status (enabled/disabled)
- Model used
- Additional metadata (title, summary, tags, pinned status)

---

### Terminal UI (TUI)

```bash
mygpt tui
```

The TUI streams responses, persists sessions, and supports RAG‑assisted chat.

**Keybindings:**
- `Ctrl+S` - Session picker (search and switch)
- `Ctrl+F` - Search messages across sessions
- `Ctrl+R` - Toggle RAG for current session
- `Ctrl+M` - Manage models
- `Ctrl+N` - Rename current session
- `Ctrl+C` - Quit

---

### Session Management

myGPT automatically organizes your conversations with intelligent session management:

**Automatic Session Naming:**
- After 5 messages (configurable), sessions are auto‑named using your local LLM
- Generates concise titles, summaries, and relevant tags
- Filenames automatically sync with titles for easy browsing

**Manual Rename:**
- **WebUI**: Click the "✏️ Rename" button in the chat interface
- **TUI**: Press `Ctrl+N` to rename the current session
- **API**: Use `POST /api/v1/sessions/{name}/rename`

**Batch Operations:**
- **Batch Delete**: `mygpt sessions batch-delete session1 session2 session3`
- **Batch Tag**: `mygpt sessions batch-tag-add "tag1 tag2" session1 session2`
- **Batch Tag Remove**: `mygpt sessions batch-tag-rm "tag1" session1 session2`
- **Batch Export**: `mygpt sessions batch-export --output /path/to/dir --format markdown session1 session2`
- **Batch Pin**: `mygpt sessions batch-pin session1 session2`
- **Batch Unpin**: `mygpt sessions batch-unpin session1 session2`
- **Batch Update Metadata**: `mygpt sessions batch-update-meta --model mistral:7b --rag-enabled true session1 session2`

**Configuration** (in `~/.myGPT/config.ini`):

```ini
[mygpt]
# Enable/disable automatic session naming
auto_summarize_enabled = true

# Trigger auto-summarization after N messages
auto_summarize_after_messages = 5

# Automatically sync filename with session title
auto_sync_filename = true
```

**How it works:**
1. After the configured number of messages, myGPT automatically generates a title
2. The session filename is updated to match the sanitized title using atomic operations with file locking
3. Sessions remain easily browsable in `~/.myGPT/sessions/`

**Safety:** File renames use exclusive file locks to prevent race conditions during concurrent access. If a session is actively being written when a rename is triggered, the rename will wait up to 10 seconds for the lock or fail gracefully with a "Session is busy" message.

---

### FastAPI backend

The API service is managed via the `mygpt ops` command. Start all services (including the API):

```bash
mygpt ops install
```

Or restart just the API:

```bash
mygpt ops restart api
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

#### Rate limiting

The FastAPI backend includes optional rate limiting to protect against abuse and DoS attacks. **Disabled by default** for localhost-only usage.

To enable rate limiting, edit `~/.myGPT/config.ini`:

```ini
[rate_limit]
enabled = true
requests_per_second = 10
burst_size = 20
```

Rate limiting uses a token bucket algorithm to track requests per IP address. When enabled, all API responses include rate limit headers:

- `X-RateLimit-Limit` – Maximum requests allowed
- `X-RateLimit-Remaining` – Remaining requests in current window
- `X-RateLimit-Reset` – Unix timestamp when limit resets

If the limit is exceeded, the API returns a `429 Too Many Requests` error:

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Too many requests. Please try again later.",
    "request_id": "..."
  }
}
```

---

### Local Web UI (Next.js)

The web UI is managed via the `mygpt ops` command:

```bash
mygpt ops restart web
```

Open in your browser:

```bash
open http://127.0.0.1:3000
```

The web UI connects to FastAPI and supports streaming chat, session browsing, and model management.

**Features:**
- Chat interface with streaming responses
- **Message editing and regeneration** - Edit any message and fork the conversation from that point, or regenerate assistant responses
- Session picker and management
- **Client-side session metadata cache** - Stale-while-revalidate pattern for faster UI updates with automatic background refresh
- **Optimistic UI updates** for instant feedback on session operations (pin, rename, delete, create)
- RAG document upload and toggle
- **Configuration wizard** at `/admin` for step-by-step setup
- **Log viewer** at `/admin/logs` for debugging and monitoring
- Model management (pull, delete, list) at `/models`
- **Keyboard shortcuts** for productivity:
  - `Cmd/Ctrl+K` - Create new chat
  - `Cmd/Ctrl+/` - Toggle sidebar visibility
  - `/` - Focus search input
  - `Esc` - Close menus and dialogs

---

### Configuration Wizard

The web UI includes a step-by-step configuration wizard for easy system setup. Access it via the **⚙️ Settings** button in the sidebar or navigate to `http://127.0.0.1:3000/admin`.

**Wizard Steps:**

1. **Model Selection** - Choose your default LLM model from available Ollama models
2. **RAG Configuration** - Enable/disable retrieval-augmented generation
3. **API Settings** - Configure log level and test API connectivity
4. **Summary** - Review and save your configuration

**Features:**
- Visual progress indicator showing current step
- Form validation for required fields
- Connection testing to verify API connectivity
- Hot-reloadable settings (no service restart required)
- Clear navigation between steps
- **Keyboard shortcuts:**
  - `←` / `→` - Navigate between steps
  - `Enter` - Advance to next step or save configuration

**Configuration Changes:**
The wizard updates your `~/.myGPT/config.ini` file with the following settings:
- `default_model` - Default LLM model for new sessions
- `rag_enabled` - Enable/disable RAG globally
- `log_level` - Logging verbosity (DEBUG, INFO, WARNING, ERROR)

Changes take effect immediately without requiring a service restart.

**Prerequisites:**
- FastAPI backend must be running (`mygpt ops install` or `mygpt ops restart api`)
- If configuration fails to load, verify API is accessible at `http://127.0.0.1:8000/health`
- See **Troubleshooting** section in docs/troubleshooting.md for common issues

---

### Message Editing and Regeneration

myGPT allows you to edit messages and regenerate responses, enabling you to explore different conversation paths.

#### Web UI

Each message in the chat interface has edit and regenerate controls:

- **✏️ Edit** - Edit any message (user or assistant)
  - Click the Edit button on any message
  - Modify the content in the textarea
  - Save to update the message
  - By default, editing **forks the conversation** (truncates messages after the edited one)
  - Edited messages are marked with an "(edited)" indicator
  - Original content is preserved in the message metadata

- **🔄 Regenerate** (user messages only) - Generate a new response from a specific point
  - Click the Regenerate button on a user message
  - The conversation is truncated after that message
  - A new assistant response is generated using the current model
  - Useful for exploring different response variations

#### API Endpoints

Edit message:
```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/sessions/{session_name}/messages/{index} \
  -H "Content-Type: application/json" \
  -d '{"content": "New message content", "fork": true}'
```

Regenerate response:
```bash
curl -X POST http://127.0.0.1:8000/api/v1/sessions/{session_name}/messages/{index}/regenerate \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b"}'
```

**Conversation Forking:**
- When you edit a message with `fork: true` (default), all messages after the edited one are removed
- This creates a new conversation branch from that point
- The original conversation is lost, so edit carefully
- Set `fork: false` to edit without truncating (preserves following messages)

**Message Search:**
```bash
# Search across all sessions
curl -X GET "http://127.0.0.1:8000/api/v1/sessions/search?query=Python&limit=50"

# Case-sensitive search
curl -X GET "http://127.0.0.1:8000/api/v1/sessions/search?query=Python&case_sensitive=true"

# Filter by role
curl -X GET "http://127.0.0.1:8000/api/v1/sessions/search?query=error&role_filter=user"

# Search within specific session
curl -X GET "http://127.0.0.1:8000/api/v1/sessions/search?query=test&session_filter=my-session"
```

Search API response includes:
- Query string and total result count
- For each match: session name/title, message index, role, full content, preview snippet, match count

---

### RAG (Retrieval-Augmented Generation) Controls

myGPT supports per-session RAG to inject relevant context from uploaded documents into chat conversations.

**Supported file types:** `.txt`, `.md` (with frontmatter parsing), `.json`, `.pdf`

#### Web UI

Use the RAG controls in the chat interface (left of the message input):
- **RAG Toggle** button to enable/disable RAG for the current session
- **File Upload** to ingest documents into the RAG database
- RAG status displays current state (ON/OFF)
- **RAG Citations** displayed inline with responses showing:
  - Retrieved source chunks
  - Relevance scores
  - Document IDs and chunk numbers
  - Expandable/collapsible citation view

#### Terminal UI (TUI)

Press `Ctrl+R` to toggle RAG on/off for the current session. The RAG status is displayed in the UI.

#### CLI / API

Enable RAG globally via config (`~/.myGPT/config.ini`):

```ini
[rag]
enable_chat_context = true
```

Or override per-request via the API:

```json
{
  "session": "my-session",
  "prompt": "Your question here",
  "rag_enabled": true
}
```

Upload documents via API:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/upload \
  -F "file=@document.md"
```

**Priority chain:** Explicit API parameter > Session metadata > Global config

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
- **Performance Tuning** – `docs/performance.md`
- **Testing** – `docs/testing.md`
- **Architecture** – `docs/architecture.md`
- **Development** – `docs/development.md`
- **Troubleshooting** – `docs/troubleshooting.md`

If you are new to the project, start with **architecture**, then **api**, then **ui**.

---

## GitHub Automation

### AI-Assisted Development with Claude Code

This repository uses GitHub Actions to enable AI-assisted development. Mention `@claude` in issues, PR comments, or reviews to get on-demand help:

**The `@claude` workflow runs only when you include `@claude` in:**
- An issue body or title (when opened or assigned)
- An issue comment
- A PR review comment
- A PR review body

**Example usage:**
```markdown
@claude Please implement this feature according to the specifications.
@claude Review this PR for security concerns.
@claude Help debug the failing test in CI.
```

**Automatic code review:**
All pull requests automatically receive AI code review feedback focusing on quality, bugs, performance, security, and test coverage.

### Automated Agent Workflows

This repository includes automated agent workflows for continuous development:

**Scrummaster Agent** - Selects and dispatches the next backlog issue
**Developer Agent** - Implements issues end-to-end with Claude Code
**Review Agent** - Reviews PRs and manages merge workflow

**To trigger the workflow:**

```bash
./scripts/trigger_next_issue.sh <release_issue_number>
```

Or manually post a comment containing `READY_FOR_NEXT_ISSUE` in the **Release tracking issue**.

The workflow will:
1. Select the next backlog issue (lowest Phase, lowest issue number)
2. Move it to In Progress and assign to developer-agent
3. Auto-implement the issue with Claude Code
4. Create a PR and submit for review

**Monitor agent activity in real-time:**
```bash
./scripts/watch_agents.sh
```

For details, see **docs/development.md** and **RUNBOOKS/**.

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

