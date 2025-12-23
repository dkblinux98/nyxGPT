

# Configuration

myGPT is configured via an INI file, typically located at:

```
~/.myGPT/config.ini
```

An example configuration file is provided in the repository as `example.config.ini`.

---

## Configuration file location

- Default: `~/.myGPT/config.ini`
- Override per invocation:

```bash
mygpt chat --config /path/to/config.ini
```

The same configuration file is used by:
- the CLI
- the FastAPI backend
- tests (via explicit overrides)

---

## `[mygpt]` section

General application behavior.

```ini
[mygpt]
default_model = llama3.1:8b
sessions_dir = ~/.myGPT/sessions
chat_timeout_seconds = 60
```

| Key | Description |
|---|---|
| `default_model` | Ollama model name used when none is specified |
| `sessions_dir` | Directory for chat session storage |
| `chat_timeout_seconds` | Timeout for a single chat request |

---

## `[ollama]` section

Connection details for the Ollama server.

```ini
[ollama]
base_url = http://127.0.0.1:11434
```

| Key | Description |
|---|---|
| `base_url` | Base URL of the Ollama HTTP API |

---

## `[api]` section

FastAPI backend configuration.

```ini
[api]
host = 127.0.0.1
port = 8000
```

| Key | Description |
|---|---|
| `host` | Bind address for the API server |
| `port` | Port for the API server |

---

## `[logging]` section

Centralized logging configuration.

```ini
[logging]
log_dir = ~/.myGPT/logs
log_level = INFO
```

| Key | Description |
|---|---|
| `log_dir` | Directory where logs are written |
| `log_level` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, etc.) |

All components (CLI, API, tests) write logs under this directory.

---

## `[rag]` section

Retrieval-Augmented Generation (RAG) settings.

```ini
[rag]
enabled = true
embedding_model = nomic-embed-text
embedding_dim = 768
chat_top_k = 3
chat_context_max_chars = 4000
cassandra_host = 127.0.0.1
cassandra_port = 9042
keyspace = mygpt
```

| Key | Description |
|---|---|
| `enabled` | Enable RAG features |
| `embedding_model` | Ollama embedding model |
| `embedding_dim` | Vector dimensionality (must match schema) |
| `chat_top_k` | Number of chunks retrieved per query |
| `chat_context_max_chars` | Max characters injected into prompt |
| `cassandra_host` | Cassandra host |
| `cassandra_port` | Cassandra port |
| `keyspace` | Cassandra keyspace for RAG |

---

## `[paths]` section (optional)

Override default filesystem locations.

```ini
[paths]
sessions = ~/.myGPT/sessions
logs = ~/.myGPT/logs
```

---

## Notes

- All paths support `~` expansion.
- Missing configuration values fall back to sensible defaults.
- Changes to RAG embedding settings require re-ingesting documents.