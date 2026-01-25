# nyxGPT

**nyxGPT** is a local-first, private, extensible ChatGPT-style system designed to run entirely on your own machine.

It uses **Ollama** for local LLM inference, supports persistent **conversation sessions**, optional **Retrieval‑Augmented Generation (RAG)** backed by **Apache Cassandra**, a powerful **CLI**, a **FastAPI backend**, a rich **terminal UI (TUI)**, and a lightweight **local web UI** built with Next.js.

Your data stays on your machine. No cloud dependency is required.

---

## Why nyxGPT?

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
nyxgpt wizard
```

The wizard will:
- Test your Ollama connection and detect available models
- Help you select a default model
- Configure RAG settings (optional)
- Generate a production-ready `~/.nyxGPT/config.ini`

#### Option 2: Manual Configuration

Manually create the config file from the example template:

```bash
mkdir -p ~/.nyxGPT
cp example.config.ini ~/.nyxGPT/config.ini
chmod 600 ~/.nyxGPT/config.ini
```

Edit `~/.nyxGPT/config.ini` to select models, logging options, RAG settings, and service paths.

---

## Running nyxGPT

### First-time Setup

1. **Run the configuration wizard** (interactive setup):
   ```bash
   nyxgpt wizard
   ```

2. **Install services** (API, web UI, logs, Cassandra helpers):
   ```bash
   nyxgpt ops install
   ```

3. **Check system health**:
   ```bash
   nyxgpt ops doctor
   ```

---

### CLI

**Chat:**
```bash
nyxgpt chat "Hello"
```

**Model Management:**
```bash
# List available models
nyxgpt models list

# Pull (download) a model
nyxgpt models pull llama3.1:8b

# Delete a model
nyxgpt models delete mistral:7b

# Show detailed model information
nyxgpt models show llama3.1:8b
```

**Message Search:**
```bash
# Search across all sessions for message content
nyxgpt sessions search "Python programming"

# Case-sensitive search
nyxgpt sessions search "Python" --case-sensitive

# Filter by role (user, assistant, or system)
nyxgpt sessions search "error" --role user

# Search within a specific session
nyxgpt sessions search "database" specific-session-name

# Limit number of results
nyxgpt sessions search "test" --limit 10
```

The search command finds messages containing the query text and displays:
- Session name and title
- Message index and role
- Number of matches per message
- Content preview with surrounding context

**Session Statistics:**
```bash
# View detailed statistics for a session
nyxgpt sessions stats my-session-name
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
nyxgpt tui
```

The TUI streams responses, persists sessions, and supports RAG‑assisted chat. The status bar displays current session information including session name, message count, active model, and RAG status.

**Keybindings:**
- `Ctrl+H` / `F1` - Show help overlay with all shortcuts
- `Ctrl+P` - Command palette (quick command access with search)
- `Tab` - Navigate to next pane
- `Shift+Tab` - Navigate to previous pane
- `Ctrl+S` - Session picker (search and switch)
- `Ctrl+F` - Search messages across sessions
- `Ctrl+R` - Toggle RAG for current session
- `Ctrl+M` - Manage models
- `Ctrl+N` - Rename current session
- `Ctrl+D` - Delete current session  (with confirmation)
- `Ctrl+L` - Clear output buffer
- `Ctrl+C` - Quit

**Commands:**
- `/clear` - Clear the output buffer

---

### Session Management

nyxGPT automatically organizes your conversations with intelligent session management:

**Automatic Session Naming:**
- After 5 messages (configurable), sessions are auto‑named using your local LLM
- Generates concise titles, summaries, and relevant tags
- Filenames automatically sync with titles for easy browsing

**Manual Rename:**
- **WebUI**: Click the "✏️ Rename" button in the chat interface
- **TUI**: Press `Ctrl+N` to rename the current session
- **API**: Use `POST /api/v1/sessions/{name}/rename`

**Batch Operations:**
- **Batch Delete**: `nyxgpt sessions batch-delete session1 session2 session3`
- **Batch Tag**: `nyxgpt sessions batch-tag-add "tag1 tag2" session1 session2`
- **Batch Tag Remove**: `nyxgpt sessions batch-tag-rm "tag1" session1 session2`
- **Batch Export**: `nyxgpt sessions batch-export --output /path/to/dir --format markdown session1 session2`
  - Exports include RAG citations with source references and confidence scores
  - Supported formats: `markdown`, `json`, `html`
- **Batch Pin**: `nyxgpt sessions batch-pin session1 session2`
- **Batch Unpin**: `nyxgpt sessions batch-unpin session1 session2`
- **Batch Update Metadata**: `nyxgpt sessions batch-update-meta --model mistral:7b --rag-enabled true session1 session2`

**Configuration** (in `~/.nyxGPT/config.ini`):

```ini
[nyxgpt]
# Enable/disable automatic session naming
auto_summarize_enabled = true

# Trigger auto-summarization after N messages
auto_summarize_after_messages = 5

# Automatically sync filename with session title
auto_sync_filename = true
```

**How it works:**
1. After the configured number of messages, nyxGPT automatically generates a title
2. The session filename is updated to match the sanitized title using atomic operations with file locking
3. Sessions remain easily browsable in `~/.nyxGPT/sessions/`

**Safety:** File renames use exclusive file locks to prevent race conditions during concurrent access. If a session is actively being written when a rename is triggered, the rename will wait up to 10 seconds for the lock or fail gracefully with a "Session is busy" message.

---

### FastAPI backend

The API service is managed via the `nyxgpt ops` command. Start all services (including the API):

```bash
nyxgpt ops install
```

Or restart just the API:

```bash
nyxgpt ops restart api
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

To enable rate limiting, edit `~/.nyxGPT/config.ini`:

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

The web UI is managed via the `nyxgpt ops` command:

```bash
nyxgpt ops restart web
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
- **RAG Collections management** at `/admin/collections` for multi-model embedding support
- **RAG Playground** at `/admin/playground` for interactive query testing and A/B comparison
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
The wizard updates your `~/.nyxGPT/config.ini` file with the following settings:
- `default_model` - Default LLM model for new sessions
- `rag_enabled` - Enable/disable RAG globally
- `log_level` - Logging verbosity (DEBUG, INFO, WARNING, ERROR)

Changes take effect immediately without requiring a service restart.

**Prerequisites:**
- FastAPI backend must be running (`nyxgpt ops install` or `nyxgpt ops restart api`)
- If configuration fails to load, verify API is accessible at `http://127.0.0.1:8000/health`
- See **Troubleshooting** section in docs/troubleshooting.md for common issues

---

### Message Editing and Regeneration

nyxGPT allows you to edit messages and regenerate responses, enabling you to explore different conversation paths.

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

nyxGPT supports per-session RAG to inject relevant context from uploaded documents into chat conversations.

**Supported file types:** `.txt`, `.md` (with frontmatter parsing), `.json`, `.pdf` (with OCR support for image-based PDFs), `.pptx` (PowerPoint presentations with speaker notes), `.docx` (Microsoft Word), `.epub` (eBooks with metadata and chapter structure), `.html`/`.htm` (web pages with boilerplate removal and semantic structure preservation), and **code repositories** with language-aware parsing for `.py`, `.js`, `.ts`, `.tsx`, `.java`, `.c`, `.cpp`, `.cs`, `.go`, `.rb`, `.rs`, `.sh`, `.php` files

#### Web UI

Use the RAG controls in the chat interface (left of the message input):
- **RAG Toggle** button to enable/disable RAG for the current session
- **File Upload** to ingest documents into the RAG database
- RAG status displays current state (ON/OFF)
- **Document Filters** button to filter which documents are searched (available when RAG is enabled):
  - Select specific documents by checkbox
  - Filter by filename (partial match, case-insensitive)
  - Filter by date range (ingestion date)
  - Filters persist across page reloads via session storage
  - Active filter indicators show when filters are applied
- **RAG Citations** displayed inline with responses showing:
  - Retrieved source chunks with click-to-expand for full text
  - Relevance scores with quality indicators (High/Medium/Low)
  - Document IDs and chunk numbers
  - Expandable/collapsible citation view
  - Export citations to separate files (JSON, Markdown)

#### Terminal UI (TUI)

Press `Ctrl+R` to toggle RAG on/off for the current session. The RAG status is displayed in the UI.

**RAG Citations** are displayed inline when RAG is enabled:
- Compact citation summary showing number of sources retrieved
- Document IDs, chunk references, and confidence scores
- Color-coded quality indicators (green/yellow/red based on score)

#### CLI / API

Enable RAG globally via config (`~/.nyxGPT/config.ini`):

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

**Filter RAG queries by document metadata:**

```json
{
  "session": "my-session",
  "prompt": "Your question here",
  "rag_enabled": true,
  "rag_filters": {
    "doc_ids": ["README.md", "ARCHITECTURE.md"],
    "filename": "README",
    "date_from": "2025-01-01",
    "date_to": "2025-12-31"
  }
}
```

Upload documents via API:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/upload \
  -F "file=@document.md"
```

**List available documents:**

```bash
curl http://127.0.0.1:8000/api/v1/rag/documents
```

**Export session citations:**

```bash
# Export all citations in JSON format
curl http://127.0.0.1:8000/api/v1/sessions/my-session/citations/export?format=json

# Export citations as Markdown
curl http://127.0.0.1:8000/api/v1/sessions/my-session/citations/export?format=markdown
```

The citations export endpoint extracts all RAG citations from assistant messages in a session, providing a complete bibliography of sources used. Useful for generating reference lists or tracking which documents contributed to responses.

#### Collection Management

Collections allow you to use different embedding models for different sets of documents. Each collection maintains its own vector index optimized for the embedding model and dimension you choose.

**Create a new collection:**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/collections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-collection",
    "embedding_dim": 768,
    "embedding_model": "nomic-embed-text"
  }'
```

**List all collections:**

```bash
curl http://127.0.0.1:8000/api/v1/rag/collections
```

**View collection settings:**

```bash
curl http://127.0.0.1:8000/api/v1/rag/collections/my-collection/settings
```

**Clear a collection** (remove all documents):

```bash
curl -X DELETE http://127.0.0.1:8000/api/v1/rag/collections/my-collection
```

**WebUI Management:**
Navigate to Settings → Collections in the WebUI to:
- View all collections and their statistics
- Create new collections with custom embedding dimensions
- View collection settings (embedding model, chunk size, overlap)
- Clear collections (removes all documents and chunks)

**Note:** Re-indexing collections and updating per-collection settings are planned features but not yet fully implemented. Currently, embedding models are determined during document ingestion, and chunk settings are global (configured in `config.ini`).

#### Document Metadata Filtering

RAG queries can be filtered by document metadata to narrow search scope and retrieve context from specific documents. Metadata is automatically stored during ingestion (filename, upload date) and can be extended with custom tags.

**Supported filters:**
- **doc_ids**: Filter by specific document IDs (OR logic)
- **filename**: Partial filename match (case-insensitive)
- **tags**: Filter by tags (document must have ALL specified tags)
- **date_from/date_to**: Filter by ingestion date range

All filters use AND logic when combined. Metadata filtering is supported via CLI, API, and internally by `retrieve_context()`.

**Note:** When uploading documents via the API (`/api/v1/rag/upload`), the filename is automatically stored in metadata. For CLI ingestion, metadata can be set programmatically by calling `ingest_document()` with a `metadata` dict parameter (e.g., `{"filename": "notes.txt", "tags": ["python", "tutorial"]}`).

#### Multi-Model Embedding Support

nyxGPT supports using different embedding models for RAG by organizing documents into collections. Each collection can use its own embedding model and dimension, allowing you to compare model performance and choose the best fit for your use case.

**Ingest documents with different models:**

```bash
# Default collection (uses config default: nomic-embed-text 768d)
nyxgpt rag ingest doc1 document.txt --ensure-schema

# Use a smaller, faster model
nyxgpt rag ingest doc2 document.txt \
  --collection all-minilm \
  --model all-minilm:latest \
  --dimension 384 \
  --ensure-schema

# Use a high-quality model
nyxgpt rag ingest doc3 document.txt \
  --collection mxbai \
  --model mxbai-embed-large:latest \
  --dimension 1024 \
  --ensure-schema
```

**Query specific collections:**

```bash
# Query the default collection
nyxgpt rag query "What is the architecture?"

# Query a specific collection
nyxgpt rag query "What is the architecture?" \
  --collection all-minilm \
  --model all-minilm:latest \
  --dimension 384
```

**Filter queries by document metadata:**

```bash
# Filter by document ID(s)
nyxgpt rag query "What is RAG?" --doc-ids "doc1,doc2"

# Filter by filename (partial match, case-insensitive)
nyxgpt rag query "summarize notes" --filename "myGPT Notes"

# Filter by tags (must have ALL specified tags)
nyxgpt rag query "python tutorial" --tags "python,tutorial"

# Filter by date range (ISO format: YYYY-MM-DD)
nyxgpt rag query "recent updates" --date-from "2024-01-01" --date-to "2024-12-31"

# Combine multiple filters
nyxgpt rag query "database docs" \
  --filename "database" \
  --tags "documentation" \
  --date-from "2024-06-01"
```

**List available collections:**

```bash
nyxgpt rag collections
```

**List documents in a collection:**

```bash
nyxgpt rag list --collection all-minilm
```

**Compare embedding models:**

```bash
nyxgpt rag compare test-doc.txt \
  nomic-embed-text:768:default \
  all-minilm:latest:384:all-minilm \
  mxbai-embed-large:latest:1024:mxbai
```

The compare command benchmarks embedding speed and query performance, helping you choose the optimal model for your requirements.

#### Collections Management UI

The web UI includes a dedicated collections management page at `/admin/collections` for visualizing and managing RAG collections.

**Access:**
- Navigate to `http://127.0.0.1:3000/admin/collections`
- Or click **⚙️ Settings** → **RAG Collections** in the main chat interface

**Features:**
- **View all collections** with real-time statistics:
  - Document count
  - Total chunk count
  - Embedding models used in each collection
- **Clear collections** to remove all documents and chunks (with confirmation)
- **Collection insights** showing which embedding models are active
- **Protected default collection** cannot be cleared to prevent accidental data loss

**Use Cases:**
- Monitor collection growth and usage
- Clean up test collections
- Verify which embedding models are in use
- Understand document distribution across collections

**Note on collection lifecycle:**
- **Creation**: Collections are created automatically when you ingest documents with specific embedding models using the CLI (see Multi-Model Embedding Support section above). No manual collection creation is needed.
- **Deletion**: Collections can be cleared (truncated) via the UI, removing all documents and chunks while preserving the table structure. To fully drop a collection table, use Cassandra admin tools directly.

#### RAG Playground

The RAG Playground provides an interactive testing environment for optimizing your RAG system. Access it at `http://127.0.0.1:3000/admin/playground`.

**Features:**

**Query Builder (Left Panel):**
- Text input for search queries
- Adjustable parameters:
  - `top_k` slider (1-50): Number of results to retrieve
  - `min_score` slider (0.0-1.0): Minimum relevance threshold
  - Collection selector: Choose which collection to query
- Feature toggles:
  - Enable debug mode: Collect detailed timing and performance metrics
  - Collect evaluation metrics: Gather comprehensive retrieval accuracy and latency data

**Results Display (Center Panel):**
- **Results Tab**: Retrieved chunks with relevance scores
  - Color-coded score indicators (green: high, yellow: medium, red: low)
  - Document ID, chunk ID, and full text for each result
  - Expandable result cards
- **Metrics Tab**: Performance analytics (requires metrics collection)
  - Retrieval accuracy: Results returned, unique documents, score distribution
  - Latency breakdown: Timing for embedding, vector search, keyword search, fusion, reranking
  - Hit rate: Success rate and threshold performance
- **Debug Tab**: Detailed debugging information (requires debug mode)
  - Query processing: Original query, query variants, total queries executed
  - Timing breakdown: Per-stage execution times
  - Results filtering pipeline: Raw results count, filtering stages, score statistics
  - Full JSON debug output

**Query History & Comparison (Bottom/Right Panel):**
- Automatic query history storage (last 20 queries in browser localStorage)
- Select multiple historical queries for A/B comparison
- Side-by-side comparison view showing:
  - Query parameters (top_k, min_score, collection)
  - Result counts and timing metrics
  - Average scores and performance differences
- Clear history option

**Use Cases:**
- **Parameter optimization**: Test different top_k and min_score values to find optimal settings
- **A/B testing**: Compare query performance with different parameters or collections
- **Performance tuning**: Identify bottlenecks using latency breakdown and debug metrics
- **Query refinement**: Experiment with query phrasing and see which variants perform best
- **Collection comparison**: Test the same query across different embedding model collections

**Access:**
- Navigate to `http://127.0.0.1:3000/admin/playground`
- Or use the admin navigation menu in the WebUI

**Prerequisites:**
- FastAPI backend must be running
- Cassandra must be available for RAG queries
- At least one collection with ingested documents

#### Debug Mode

RAG debug mode provides detailed troubleshooting metrics for performance tuning and understanding retrieval behavior.

**Enable via config** (`~/.nyxGPT/config.ini`):

```ini
[rag]
debug_mode = true
```

**Or enable per-request via API:**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "test query", "debug_mode": true}'
```

**Debug information includes:**
- **Timing metrics** - Query expansion, embedding generation, vector search, filtering, composition
- **Query analysis** - Original query, expanded variants, number of queries executed
- **Embedding details** - Model name, dimensions, batch size, processing time
- **Vector search results** - Result counts, score distribution (min, max, mean)
- **Filtering stats** - Results after each filter stage (min score, deduplication, max chunks)
- **Context composition** - Character counts before/after truncation, chunks included

**Priority chain:** Explicit API parameter > Session metadata > Global config

---

### PDF OCR Support

nyxGPT includes OCR (Optical Character Recognition) support for image-based PDFs that have minimal or no extractable text.

**Features:**
- **Automatic detection** - OCR is triggered when PDF text extraction produces less than the configured threshold (default: 50 characters)
- **Configurable quality** - Adjust DPI, language, and page segmentation mode for optimal results
- **Smart fallback** - Uses standard text extraction first, only applying OCR when needed
- **Multi-language support** - Supports multiple Tesseract language packs

**Prerequisites:**

OCR requires Tesseract to be installed on your system:

```bash
# macOS (Homebrew)
brew install tesseract

# Ubuntu/Debian
sudo apt-get install tesseract-ocr

# Install additional language packs if needed
# macOS
brew install tesseract-lang

# Ubuntu/Debian
sudo apt-get install tesseract-ocr-[lang]
# Example: tesseract-ocr-spa for Spanish
```

**Configuration** (in `~/.nyxGPT/config.ini`):

```ini
[pdf]
# Enable OCR for image-based PDFs
ocr_enabled = true

# Minimum text threshold before triggering OCR (characters)
ocr_min_text_threshold = 50

# OCR DPI resolution (300 recommended for standard docs)
ocr_dpi = 300

# OCR language (ISO 639-2 codes: eng, spa, fra, deu, etc.)
ocr_lang = eng

# Page Segmentation Mode (3 = automatic, recommended)
ocr_psm = 3

# Optional: path to tesseract executable if not in PATH
# tesseract_cmd = /usr/bin/tesseract
```

**How it works:**
1. Standard text extraction is attempted using pdfplumber and pypdf
2. If extracted text is below the threshold, OCR is triggered automatically
3. PDF pages are converted to images at the specified DPI
4. Tesseract OCR extracts text from each image
5. If OCR produces more text than standard extraction, it's used instead

**Language Support:**

Use multiple languages by specifying them with `+`:
```ini
ocr_lang = eng+spa  # English and Spanish
```

Download additional language packs from: https://github.com/tesseract-ocr/tessdata

---

## Logs & runtime data

All runtime state lives under:

```text
~/.nyxGPT/
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

- Distribution name: **nyxGPT**
- Python package name: **nyxgpt**
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



