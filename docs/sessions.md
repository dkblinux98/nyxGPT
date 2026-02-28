# Sessions & Memory

nyxGPT persists conversations using **sessions**. Sessions allow conversations to continue across CLI and API invocations and form the basis for future memory features.

---

## What is a session?

A session represents a named conversation thread. Each session stores:

- message history (user + assistant turns)
- metadata (timestamps, model, optional future fields)

Sessions are stored on disk as JSON files and are automatically created on first use.

---

## Session storage

By default, sessions are stored under:

```
~/.nyxGPT/sessions
```

Each session consists of:

- `<name>.json` — message history
- `<name>.meta.json` — metadata

Example:

```
default.json
default.meta.json
research.json
research.meta.json
```

The location can be changed via configuration:

```ini
[nyxgpt]
sessions_dir = ~/.nyxGPT/sessions
```

---

## CLI usage

### Start or continue a session

```bash
nyxgpt chat "Hello" --session default
```

If the session does not exist, it is created automatically.

---

### List sessions

```bash
nyxgpt sessions list
```

Example output:

```
name        messages  modified
--------------------------------
default     12        2025-01-10
research    4         2025-01-11
```

---

### Show session details

```bash
nyxgpt sessions show default
```

Displays message count, timestamps, and metadata.

---

### Delete a session

```bash
nyxgpt sessions delete default
```

Deletes both the message and metadata files.

---

### Export a session

Export session conversations to various formats:

```bash
# Export to markdown (default)
nyxgpt sessions export default

# Export to JSON
nyxgpt sessions export default --format json

# Export to HTML
nyxgpt sessions export default --format html

# Save to file instead of stdout
nyxgpt sessions export default --format markdown --output session.md
```

Supported export formats:

- **markdown** — Formatted markdown with headers for each message role
- **json** — Complete session data including messages and metadata
- **html** — Styled HTML page with conversation and metadata

All exports include full session metadata (title, timestamps, tags, model, etc.).

---

## Session metadata

Each session has a metadata file (`<name>.meta.json`) containing comprehensive information:

**Core metadata:**
- `name` - Session identifier
- `created_at` - ISO 8601 timestamp of creation
- `modified_at` - ISO 8601 timestamp of last modification
- `model` - Model used for this session

**Organizational metadata:**
- `pinned` (bool) - Pin session to top of list
- `tags` (list) - User-defined tags for categorization
- `title` (string) - Human-readable session title
- `summary` (string) - Auto-generated or manual summary

**Analysis metadata:**
- `token_estimate` (int) - Estimated total tokens in conversation
- `rag_enabled` (bool) - Whether RAG is enabled for this session
- `attached_doc_ids` (list, optional) - Document IDs force-included in every RAG retrieval (see [Force-Include Mode](#force-include-document-attachment))

Metadata is updated automatically on each interaction and supports rich organizational features.

---

## Metadata management

### Auto-summarization

Sessions can automatically generate titles, summaries, and tags using the LLM:

```bash
# Manual summarization
nyxgpt sessions summarize default

# Auto-summarization (configured in config.ini)
[nyxgpt]
auto_summarize_enabled = true
auto_summarize_after_messages = 5
```

When enabled, sessions are automatically summarized after reaching the configured message count.

### Pin sessions

Pin important sessions to keep them at the top of your session list:

```bash
# Pin a session
nyxgpt sessions pin default

# Unpin a session
nyxgpt sessions unpin default
```

### Tags

Organize sessions with user-defined tags:

```bash
# Add tags
nyxgpt sessions tag-add default python debugging tutorial

# Remove tags
nyxgpt sessions tag-rm default tutorial

# Batch tag operations
nyxgpt sessions batch-tag-add python debugging -- session1 session2 session3
nyxgpt sessions batch-tag-rm tutorial -- session1 session2
```

### Session titles

Set custom titles for your sessions:

```bash
# Set title
nyxgpt sessions title default "Python Debugging Session"
```

### Filename sync

Sync session filenames with their titles for better organization:

```bash
# Manual sync
nyxgpt sessions sync-filename default

# Auto-sync (configured in config.ini)
[nyxgpt]
auto_sync_filename = true
```

When enabled, session filenames automatically update to match their titles (sanitized for filesystem compatibility).

### Session statistics

View detailed statistics about a session:

```bash
nyxgpt sessions stats default
```

**Output includes:**
- Message count (total, user, assistant, system)
- Token estimate (total and per message)
- RAG usage statistics
- Session age and activity
- Metadata (title, tags, model)

### Search sessions

Search for messages across all sessions or within specific sessions:

```bash
# Search all sessions
nyxgpt sessions search "python error"

# Search specific session
nyxgpt sessions search "function" --session default

# Filter by role
nyxgpt sessions search "help" --role user

# Case-sensitive search
nyxgpt sessions search "ValueError" --case-sensitive

# Limit results
nyxgpt sessions search "debug" --limit 10
```

---

## Batch operations

Perform operations on multiple sessions at once for efficient management.

### Batch delete

Delete multiple sessions in one command:

```bash
nyxgpt sessions batch-delete -- session1 session2 session3
```

**Note:** The `--` separator is required to distinguish session names from flags.

### Batch export

Export multiple sessions simultaneously:

```bash
# Export to markdown (default)
nyxgpt sessions batch-export -- session1 session2 session3

# Export to JSON
nyxgpt sessions batch-export --format json -- session1 session2

# Export to HTML
nyxgpt sessions batch-export --format html -- session1 session2

# Specify output directory
nyxgpt sessions batch-export --output-dir ./exports -- session1 session2
```

### Batch pin/unpin

Pin or unpin multiple sessions:

```bash
# Pin multiple sessions
nyxgpt sessions batch-pin -- important1 important2 important3

# Unpin multiple sessions
nyxgpt sessions batch-unpin -- old1 old2 old3
```

### Batch tag operations

Add or remove tags from multiple sessions:

```bash
# Add tags to multiple sessions
nyxgpt sessions batch-tag-add python tutorial -- session1 session2 session3

# Remove tags from multiple sessions
nyxgpt sessions batch-tag-rm outdated -- session1 session2 session3
```

### Batch metadata update

Update model or RAG settings for multiple sessions:

```bash
# Update model for multiple sessions
nyxgpt sessions batch-update-meta --model llama3.1:8b -- session1 session2

# Enable RAG for multiple sessions
nyxgpt sessions batch-update-meta --rag-enabled true -- session1 session2

# Disable RAG for multiple sessions
nyxgpt sessions batch-update-meta --rag-enabled false -- session1 session2
```

---

## Force-Include Document Attachment

Attach specific documents to a session so their chunks are **always** retrieved during RAG, regardless of query relevance. See [docs/rag.md — Force-Include Mode](rag.md#force-include-mode-per-session-document-attachment) for full details.

### API Endpoints

#### List attached documents

```bash
GET /api/v1/sessions/{name}/documents
```

**Response:**
```json
{
  "session": "my-session",
  "attached_doc_ids": ["report-2025.pdf", "spec-v2.md"]
}
```

#### Attach a document

```bash
POST /api/v1/sessions/{name}/documents
Content-Type: application/json

{"doc_id": "report-2025.pdf"}
```

**Response:**
```json
{
  "session": "my-session",
  "attached_doc_ids": ["report-2025.pdf"]
}
```

Attaching the same `doc_id` twice is idempotent — the ID appears only once in the list.

#### Detach a document

```bash
DELETE /api/v1/sessions/{name}/documents/{doc_id}
```

**Response:**
```json
{
  "session": "my-session",
  "attached_doc_ids": []
}
```

### Notes

- Attached doc IDs are stored in the session's `.meta.json` file under `attached_doc_ids`. The field is absent when no documents are attached.
- The doc must already be ingested into the RAG index; attaching an unknown ID produces no chunks but does not error.
- Force-included chunks appear **before** normal RAG chunks in the merged context.

---

## Safety & validation

- Session names are validated to prevent path traversal
- Invalid session names raise errors
- All file operations are confined to `sessions_dir`

---

## Notes

- Sessions are shared between CLI and API
- Deleting a session is irreversible
- Sessions are lightweight JSON files and safe to back up
