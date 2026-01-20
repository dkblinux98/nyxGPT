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
vectorstore_dir = ~/.myGPT/vectorstore
chat_timeout_seconds = 60
auto_summarize_enabled = true
auto_summarize_after_messages = 5
auto_sync_filename = true
system_prompt_minimize = false
```

| Key | Description |
|---|---|
| `default_model` | Ollama model name used when none is specified |
| `sessions_dir` | Directory for chat session storage |
| `vectorstore_dir` | Directory for vector embeddings and RAG data |
| `chat_timeout_seconds` | Timeout for a single chat request |
| `auto_summarize_enabled` | Automatically generate session title/summary/tags |
| `auto_summarize_after_messages` | Trigger auto-summarization after N messages (0 to disable) |
| `auto_sync_filename` | Automatically sync session filename with title |
| `system_prompt_minimize` | Minimize system prompts to reduce token usage |

**Note:** `default_model`, `auto_summarize_enabled`, and `auto_summarize_after_messages` are **hot-reloadable** and do not require a restart.

---

## `[ollama]` section

Connection details for the Ollama server.

```ini
[ollama]
base_url = http://127.0.0.1:11434
default_model = qwen2.5:latest
```

| Key | Description |
|---|---|
| `base_url` | Base URL of the Ollama HTTP API |
| `default_model` | Default model for Ollama (optional override of `mygpt.default_model`) |

**Note:** If `default_model` is not set, myGPT uses `mygpt.default_model` instead.

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

## `[prompt]` section

Adaptive prompt mode configuration for dynamic system prompts.

```ini
[prompt]
adaptive_mode_enabled = false
short_threshold = 3
long_threshold = 10
```

| Key | Description |
|---|---|
| `adaptive_mode_enabled` | Enable adaptive prompt mode (adjusts prompts based on conversation length) |
| `short_threshold` | Message count threshold for short mode (concise prompts) |
| `long_threshold` | Message count threshold for long mode (comprehensive prompts) |

**Behavior:**
- Disabled by default - only applies when no custom system_prompt is set
- Short mode (< `short_threshold` messages): Concise prompts for quick interactions
- Medium mode (`short_threshold` to `long_threshold` messages): Balanced prompts
- Long mode (> `long_threshold` messages): Comprehensive prompts for complex discussions

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

## `[context]` section

Context window budget enforcement to prevent exceeding model token limits.

```ini
[context]
default_window_size = 8192
warning_threshold = 0.8
# Model-specific overrides (optional)
context_window_llama3_1_8b = 131072
context_window_qwen2_5_0_5b = 32768
```

| Key | Description |
|---|---|
| `default_window_size` | Maximum context window in tokens (default: 8192) |
| `warning_threshold` | Warn when usage exceeds this fraction (0.0-1.0, default: 0.8) |
| `context_window_<model>` | Model-specific override (replace `:` `.` `/` with `_` in model name) |

**Behavior:**
- Token counting uses tiktoken (cl100k_base encoding) for estimation
- When context exceeds the budget, oldest messages are automatically removed
- System messages and current prompt are always preserved
- Warnings are logged when approaching the threshold (default: 80% capacity)

**Common context window sizes:**
- `llama3.1` (8b/70b): 128k tokens
- `llama2`: 4k tokens
- `mistral`: 8k tokens
- `qwen2.5`: 32k tokens

---

## `[rag]` section

Retrieval-Augmented Generation (RAG) settings.

```ini
[rag]
enable_chat_context = false
embedding_model = nomic-embed-text
embedding_dim = 768
embedding_batch_size = 16
embedding_timeout_seconds = 120
chunk_size = 800
chunk_overlap = 100
overlap_strategy = trailing
preserve_headings = true
sentence_aware = true
chat_top_k = 5
min_score = 0.0
max_chunks = 6
chat_context_max_chars = 2400
dedupe = true
enable_query_expansion = false
include_scores = false
include_headers = true
cassandra_hosts = 127.0.0.1
cassandra_port = 9042
cassandra_keyspace = mygpt
cassandra_table = rag_chunks
```

| Key | Description |
|---|---|
| `enable_chat_context` | Enable RAG context injection for chat (hot-reloadable) |
| `enabled` | **Legacy alias** for `enable_chat_context` (deprecated, use `enable_chat_context` instead) |
| `embedding_model` | Ollama embedding model |
| `embedding_dim` | Vector dimensionality (must match Cassandra VECTOR dimension) |
| `embedding_batch_size` | Batch size for embedding requests (smaller = lower memory, slower) |
| `embedding_timeout_seconds` | Timeout for each embedding batch request to Ollama |
| `chunk_size` | Maximum characters per text chunk |
| `chunk_overlap` | Character overlap between adjacent chunks |
| `overlap_strategy` | Overlap method: `trailing` (characters), `sentence` (complete sentences), or `semantic` (paragraphs) |
| `preserve_headings` | Keep Markdown headings with their content for better context (default: true) |
| `sentence_aware` | Split on sentence boundaries for better semantic chunking (default: true) |
| `chat_top_k` | Number of candidate chunks to retrieve from vector store |
| `top_k` | **Legacy alias** for `chat_top_k` (deprecated, use `chat_top_k` instead) |
| `min_score` | Minimum similarity score required for chunk inclusion |
| `max_chunks` | Hard cap on number of chunks injected into prompt |
| `chat_context_max_chars` | Maximum total characters of retrieved context |
| `dedupe` | Remove duplicate or near-duplicate chunks before injection |
| `enable_query_expansion` | Generate alternative phrasings to improve retrieval |
| `expansion_model` | Model for query expansion (optional, defaults to mygpt.default_model) |
| `include_scores` | Include similarity scores in context headers (debugging only) |
| `include_headers` | Include per-chunk headers like "[Context 1]" in injected context |
| `cassandra_hosts` | Cassandra host(s) |
| `cassandra_port` | Cassandra port |
| `cassandra_keyspace` | Cassandra keyspace for RAG |
| `cassandra_table` | Cassandra table name for RAG chunks |

**RAG Prompt Templates:**

Configurable templates control how retrieved context is presented to the LLM. Use `{context}` as a placeholder for formatted retrieved chunks.

```ini
[rag]
# Instruction template: tells the model how to use retrieved context
# instruction_template = Use the retrieved context below when it is relevant and helpful. Do not mention that you were given retrieved context unless the user explicitly asks about sources. If the context is insufficient, say so and answer from general knowledge.
#
# {context}

# Context format: wraps the retrieved chunks
# context_format = --- BEGIN RETRIEVED CONTEXT ---
# {context}
# --- END RETRIEVED CONTEXT ---
```

**Alternative template examples:**

```ini
# Minimal/concise template:
# instruction_template = Answer using the following context when relevant:
# {context}
# context_format = Context: {context}

# Verbose/detailed template with emphasis on citations:
# instruction_template = You have access to retrieved context below. Use it to inform your response when relevant. Always cite sources when using retrieved information. If the context doesn't contain relevant information, clearly state that and provide a general answer.
#
# {context}
# context_format = === RETRIEVED DOCUMENTS ===
# {context}
# === END DOCUMENTS ===
```

**Note:** `enable_chat_context` is **hot-reloadable** and takes effect on the next request. Changes to embedding schema require re-ingestion of documents.

---

## `[paths]` section

Absolute paths for operational components.

```ini
[paths]
repo_dir = /path/to/myGPT
venv_python = /path/to/myGPT/.venv/bin/python
node_bin = /opt/homebrew/bin/node
npm_bin = /opt/homebrew/bin/npm
```

| Key | Description |
|---|---|
| `repo_dir` | Absolute path to the myGPT repository |
| `venv_python` | Path to the Python executable in the project venv |
| `node_bin` | Path to Node.js executable |
| `npm_bin` | Path to npm executable |

---

## `[openai]` section

OpenAI API integration for accessing GPT models in addition to local Ollama models.

```ini
[openai]
api_key =
```

| Key | Description |
|---|---|
| `api_key` | OpenAI API key for accessing GPT models (required if using OpenAI models) |

**Getting your API key:**
- Visit: https://platform.openai.com/api-keys
- Create a new API key
- Store it securely in your config file

**Security best practices:**
- Keep your API key confidential - never commit it to version control
- Restrict file permissions: `chmod 600 ~/.myGPT/config.ini`
- Monitor usage at: https://platform.openai.com/usage
- Rotate keys regularly

---

## `[github]` section

GitHub integration for automated workflows and agent operations.

```ini
[github]
pat =
agents_enabled = false
repo_owner = your-username
repo_name = your-repo-name
project_owner = your-username
project_number = 1
dev_agent = myGPT-developer-agent
review_agent = myGPT-review-agent
scrum_agent = myGPT-scrummaster-agent
human_owner = your-username
status_field = Status
status_backlog = Backlog
status_in_progress = In progress
status_in_review = In Review
status_for_release = For Release
release_branch = v1.0.0
release_issue_number = 1
scrummaster_agent_token =
developer_agent_token =
review_agent_token =
myagent_review_agent_token =
claude_code_oauth_token =
```

| Key | Description |
|---|---|
| `pat` | GitHub Personal Access Token with `repo` and `project` scopes |
| `agents_enabled` | Enable GitHub agent integration (default: `false`) |
| `repo_owner` | GitHub repository owner username |
| `repo_name` | GitHub repository name |
| `project_owner` | GitHub Project owner username |
| `project_number` | GitHub Project number (from project URL) |
| `dev_agent` | Developer agent GitHub username |
| `review_agent` | Review agent GitHub username |
| `scrum_agent` | Scrummaster agent GitHub username |
| `human_owner` | Human owner GitHub username |
| `status_field` | GitHub Project status field name |
| `status_backlog` | Status value for backlog items |
| `status_in_progress` | Status value for in-progress items |
| `status_in_review` | Status value for items in review |
| `status_for_release` | Status value for items ready for release |
| `release_branch` | Active release branch name |
| `release_issue_number` | GitHub issue number tracking the current release |
| `scrummaster_agent_token` | Optional fine-grained token for scrummaster agent |
| `developer_agent_token` | Optional fine-grained token for developer agent |
| `review_agent_token` | Optional fine-grained token for review agent |
| `myagent_review_agent_token` | Optional fine-grained token for custom review agent |
| `claude_code_oauth_token` | OAuth token for Claude Code AI-assisted operations |

**Setting up GitHub integration:**

1. **Create a Personal Access Token (PAT):**
   - Visit: https://github.com/settings/tokens
   - Generate a new token with `repo` and `project` permissions
   - Store it in the `pat` field

2. **Create agent accounts (optional):**
   - Create separate GitHub accounts for each agent
   - Generate tokens for each agent with appropriate permissions
   - Configure agent-specific tokens in the config

3. **Configure GitHub Project:**
   - Create or identify your GitHub Project
   - Note the project number from the URL (e.g., `https://github.com/users/USERNAME/projects/2` → project_number = 2)
   - Ensure status field and values match your project configuration

4. **Get Claude Code OAuth token:**
   - Visit: https://claude.ai/settings/oauth
   - Create an OAuth token for API access
   - Store it in `claude_code_oauth_token`

**Security considerations:**
- All tokens are sensitive - never commit config files with real tokens
- Use fine-grained tokens with minimum required permissions
- Regularly rotate tokens
- Monitor agent activity in GitHub audit logs
- See [`docs/github-tokens.md`](github-tokens.md) for detailed token setup

**Note:** Agent tokens fall back to the main `pat` if not explicitly set.

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