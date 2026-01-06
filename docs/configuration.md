# Configuration

myGPT is configured via an INI file, typically located at:

```
~/.myGPT/config.ini
```

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
default_model = qwen2.5:0.5b
sessions_dir = ~/.myGPT/sessions
chat_timeout_seconds = 60
```

| Key | Description |
|---|---|
| `default_model` | Ollama model name used when none is specified |
| `sessions_dir` | Directory for chat session storage |
| `chat_timeout_seconds` | Timeout for a single chat request |

**Note:** `default_model` is **hot-reloadable** and does not require a restart.

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

Centralized logging configuration. This is the **single source of truth** for all logging settings, managed by `src/mygpt/logging.py`.

```ini
[logging]
level = INFO
dir = ~/.myGPT/logs
```

| Key | Description |
|---|---|
| `level` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, etc.) |
| `dir` | Directory where logs are written (default: `~/.myGPT/logs`) |

All components (CLI, API, tests) use this centralized configuration. Logs are written to `{dir}/mygpt.log` with automatic rotation.

**Note:** Changes to the logging `level` are **applied at runtime without restart**.

---

## `[auth]` section

Optional API key authentication for the FastAPI backend. Authentication is **disabled by default** for local-only usage.

```ini
[auth]
enabled = false
api_key =
header = X-API-Key
```

| Key | Description |
|---|---|
| `enabled` | Enable/disable API key authentication (default: `false`) |
| `api_key` | Shared secret required when authentication is enabled |
| `header` | HTTP header name for the API key (default: `X-API-Key`) |

**When enabled:**
- All `/api/v1/*` endpoints require the API key
- Health check (`/health`) and documentation endpoints remain public
- Invalid or missing API keys return `401 Unauthorized`
- API keys are compared using constant-time comparison to prevent timing attacks

**Security best practices:**
- Generate strong, random keys using `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- Restrict file permissions: `chmod 600 ~/.myGPT/config.ini`
- Never commit `~/.myGPT/config.ini` to version control
- Rotate keys regularly and immediately if compromise is suspected

**Note:** Authentication configuration is **hot-reloadable** and takes effect immediately without restart.

For detailed usage, examples, and security recommendations, see [`docs/api.md`](api.md#authentication).

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

**Note:** `enabled` is **hot-reloadable** and takes effect on the next request. Changes to embedding schema require re-ingestion of documents.

---

## `[paths]` section

Absolute paths for operational components.

```ini
[paths]
repo_dir = /path/to/myGPT
venv_python = /path/to/myGPT/.venv/bin/python
node_bin = /usr/local/bin/node
npm_bin = /usr/local/bin/npm
```

| Key | Description |
|---|---|
| `repo_dir` | Absolute path to the myGPT repository |
| `venv_python` | Path to the Python executable in the project venv |
| `node_bin` | Path to Node.js executable |
| `npm_bin` | Path to npm executable |

---

## Hot-reloadable settings

- `mygpt.default_model`
- `logging.level`
- `rag.enabled`
- `auth.enabled`, `auth.api_key`, `auth.header`

All other settings require a service restart unless otherwise noted.

---

## Notes

- All paths support `~` expansion.
- Missing configuration values fall back to sensible defaults.
- Changes to RAG embedding settings require re-ingesting documents.