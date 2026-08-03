# CLI Reference

The `nyxgpt` command is the primary interface for local use. This page
covers the general-purpose commands; command groups with their own deep
documentation are linked out to instead of duplicated here:

- Sessions (`nyxgpt sessions ...`) — see [Sessions](sessions.md)
- RAG (`nyxgpt rag ...`) — see [RAG](rag.md)
- Bring the stack up/down (`nyxgpt up` / `nyxgpt down`) — thin aliases for
  `nyxgpt ops install`/`nyxgpt ops down`, see
  [Ops helpers](ops.md#nyxgpt-up--nyxgpt-down)
- Ops (`nyxgpt ops ...`) — see [Ops helpers](ops.md)
- Canary (`nyxgpt canary ...`) — see [Kubernetes](kubernetes.md)
- Configuration wizard (`nyxgpt wizard`) — see [Configuration](configuration.md)

---

## Info

`nyxgpt info` is the default command when run with no arguments:

```bash
nyxgpt info
```

Prints config-derived defaults (Ollama base URL, default model).

---

## Chat

```bash
nyxgpt chat "Hello"
```

Continue or create a named session with `--session`, and force RAG on for a
single request with `--rag-mode` — see [Sessions — CLI usage](sessions.md#cli-usage).

---

## Model Management

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

These map to the `/api/v1/models*` endpoints — see
[API — Models endpoints](api.md#models-endpoints).

---

## Message Search

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

The search command finds messages containing the query text and displays
the session name/title, message index and role, number of matches per
message, and a content preview with surrounding context. See
[Sessions — Search sessions](sessions.md#search-sessions) for the full
reference (including the equivalent `GET /api/v1/sessions/search` API).
