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

Each session has a metadata file containing:

- session name
- creation time
- last modified time
- model used

Metadata is updated automatically and is intended to support future features such as:

- pinned sessions
- tags
- summarization
- long-term memory

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
