# Sessions & Memory

myGPT persists conversations using **sessions**. Sessions allow conversations to continue across CLI and API invocations and form the basis for future memory features.

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
~/.myGPT/sessions
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
[mygpt]
sessions_dir = ~/.myGPT/sessions
```

---

## CLI usage

### Start or continue a session

```bash
mygpt chat "Hello" --session default
```

If the session does not exist, it is created automatically.

---

### List sessions

```bash
mygpt sessions list
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
mygpt sessions show default
```

Displays message count, timestamps, and metadata.

---

### Delete a session

```bash
mygpt sessions delete default
```

Deletes both the message and metadata files.

---

### Export a session

Export session conversations to various formats:

```bash
# Export to markdown (default)
mygpt sessions export default

# Export to JSON
mygpt sessions export default --format json

# Export to HTML
mygpt sessions export default --format html

# Save to file instead of stdout
mygpt sessions export default --format markdown --output session.md
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

Metadata is updated automatically on each interaction and supports rich organizational features.

---

## Metadata management

### Auto-summarization

Sessions can automatically generate titles, summaries, and tags using the LLM:

```bash
# Manual summarization
mygpt sessions summarize default

# Auto-summarization (configured in config.ini)
[mygpt]
auto_summarize_enabled = true
auto_summarize_after_messages = 5
```

When enabled, sessions are automatically summarized after reaching the configured message count.

### Pin sessions

Pin important sessions to keep them at the top of your session list:

```bash
# Pin a session
mygpt sessions pin default

# Unpin a session
mygpt sessions unpin default
```

### Tags

Organize sessions with user-defined tags:

```bash
# Add tags
mygpt sessions tag-add default python debugging tutorial

# Remove tags
mygpt sessions tag-rm default tutorial

# Batch tag operations
mygpt sessions batch-tag-add python debugging -- session1 session2 session3
mygpt sessions batch-tag-rm tutorial -- session1 session2
```

### Session titles

Set custom titles for your sessions:

```bash
# Set title
mygpt sessions title default "Python Debugging Session"
```

### Filename sync

Sync session filenames with their titles for better organization:

```bash
# Manual sync
mygpt sessions sync-filename default

# Auto-sync (configured in config.ini)
[mygpt]
auto_sync_filename = true
```

When enabled, session filenames automatically update to match their titles (sanitized for filesystem compatibility).

### Session statistics

View detailed statistics about a session:

```bash
mygpt sessions stats default
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
mygpt sessions search "python error"

# Search specific session
mygpt sessions search "function" --session default

# Filter by role
mygpt sessions search "help" --role user

# Case-sensitive search
mygpt sessions search "ValueError" --case-sensitive

# Limit results
mygpt sessions search "debug" --limit 10
```

---

## Batch operations

Perform operations on multiple sessions at once for efficient management.

### Batch delete

Delete multiple sessions in one command:

```bash
mygpt sessions batch-delete -- session1 session2 session3
```

**Note:** The `--` separator is required to distinguish session names from flags.

### Batch export

Export multiple sessions simultaneously:

```bash
# Export to markdown (default)
mygpt sessions batch-export -- session1 session2 session3

# Export to JSON
mygpt sessions batch-export --format json -- session1 session2

# Export to HTML
mygpt sessions batch-export --format html -- session1 session2

# Specify output directory
mygpt sessions batch-export --output-dir ./exports -- session1 session2
```

### Batch pin/unpin

Pin or unpin multiple sessions:

```bash
# Pin multiple sessions
mygpt sessions batch-pin -- important1 important2 important3

# Unpin multiple sessions
mygpt sessions batch-unpin -- old1 old2 old3
```

### Batch tag operations

Add or remove tags from multiple sessions:

```bash
# Add tags to multiple sessions
mygpt sessions batch-tag-add python tutorial -- session1 session2 session3

# Remove tags from multiple sessions
mygpt sessions batch-tag-rm outdated -- session1 session2 session3
```

### Batch metadata update

Update model or RAG settings for multiple sessions:

```bash
# Update model for multiple sessions
mygpt sessions batch-update-meta --model llama3.1:8b -- session1 session2

# Enable RAG for multiple sessions
mygpt sessions batch-update-meta --rag-enabled true -- session1 session2

# Disable RAG for multiple sessions
mygpt sessions batch-update-meta --rag-enabled false -- session1 session2
```

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
