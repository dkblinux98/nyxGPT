# Configuration

nyxGPT is configured via an INI file, typically located at:

```
~/.nyxGPT/config.ini
```

---

## Configuration file location

- Default: `~/.nyxGPT/config.ini`
- Override per invocation:

```bash
nyxgpt chat --config /path/to/config.ini
```

The same configuration file is used by:
- the CLI
- the FastAPI backend
- tests (via explicit overrides)

---

## Creating a config file

### Option 1: Interactive wizard (recommended)

```bash
nyxgpt wizard
```

The wizard tests your Ollama connection and detects available models, helps
you select a default model, optionally configures RAG, and generates a
production-ready `~/.nyxGPT/config.ini`.

### Option 2: Manual

```bash
mkdir -p ~/.nyxGPT
cp example.config.ini ~/.nyxGPT/config.ini
chmod 600 ~/.nyxGPT/config.ini
```

Edit `~/.nyxGPT/config.ini` to select models, logging options, RAG settings,
and service paths — see the section reference below.

### Option 3: Web Configuration Wizard (edit an existing install)

Once nyxGPT is running, `/admin` in the web UI is a six-step **Configuration
Wizard**: Core & Model (`[nyxgpt]`, `[logging]`, `[ollama]`), RAG
Configuration (`[rag]`'s chat toggle and Cassandra connection), API & Auth
(`[api]`, `[auth]`, `[rate_limit]`), Observability (`[tracing]`,
`[error_tracking]`, `[monitoring]`, `[log_aggregation]`), **Additional
Settings**, and a Summary/save step. (Live resource metrics moved to the
System Health screen and the admin dashboard, #3384, #3413.)

- **The wizard's field list is *derived* from `example.config.ini`, not
  hand-maintained (#3388).** `src/nyxgpt/config_wizard.py` parses
  `example.config.ini` at startup and builds its schema from every section
  and key it declares, so a new config option automatically appears in the
  wizard the moment it's added to the example file -- the two can never
  silently drift apart the way the original hand-maintained schema did.
  Fields with an established, purpose-built widget elsewhere in the wizard
  (e.g. the RAG step's Cassandra fields) keep that widget; everything else
  renders generically on the **Additional Settings** step, grouped by topic
  (core behavior; API & network; RAG, retrieval & caching -- `[rag]`,
  `[context]`, `[pdf]`, `[cache]` together; observability & self-heal;
  Kubernetes deployment), with its type (checkbox/number/text/secret)
  inferred from the value declared in `example.config.ini`.
- **Three sections are deliberately excluded:** `[paths]`, `[openai]`, and
  `[github]` are agent-level concerns, not nyxGPT user options -- edit those
  by hand in `config.ini` if you need them. (`[openai]` will return to the
  wizard once external commercial LLM support ships.) Every other section is
  in scope; when in doubt the wizard covers a section rather than excluding
  it.
- **Every field shows its *effective* value, never a blank box.** A key
  absent from `config.ini` renders the same fallback value its `config.py`
  getter would use at runtime (e.g. `[tracing] service_name` shows
  `nyxgpt-api` even with no `[tracing]` section at all) -- see the
  per-section tables below for what each fallback is. A **"default"** badge
  next to the field's label marks it as inherited rather than something you
  explicitly set; a field whose fallback genuinely is blank (e.g.
  `[error_tracking] dsn`) stays blank with no badge. Saving without touching
  a badged field leaves it unset in `config.ini` -- it keeps tracking future
  changes to that default instead of freezing today's value in as an
  explicit setting (#3385).
- **Save merges into `config.ini`; it never rewrites the whole file
  (#3388).** Saving updates or adds only the specific keys you changed at
  the line level, so comments, key order, and any section or key the wizard
  doesn't manage -- including the excluded `[paths]`/`[openai]`/`[github]`
  and anything you hand-added -- survive a save byte-for-byte. A key is
  never deleted by a regular save.
- **Drift reconciliation.** If `config.ini` has a key inside a wizard-managed
  section that's no longer declared in `example.config.ini` (a retired
  option, or something added outside the wizard), the Additional Settings
  step shows it in a "no longer recognized" banner with a **Remove** button
  per key -- nothing is ever deleted automatically, only on your
  confirmation.
- **Save is apply-on-save, not just a file write.** Saving validates every
  changed field (ports, URLs, hosts), writes `config.ini` (still the single
  source of truth, #3194), and immediately invalidates the API's config
  cache so hot-reloadable settings (model, RAG, logging, auth)
  take effect on the very next request — no restart needed for those.
- **Settings that can't be hot-reloaded** — `[api] host`/`port`, the RAG
  Cassandra connection/embedding model, embedding/response/query cache
  backends, and anything else read only at process startup — are reported
  back as `restart_required` in the save response and tracked server-side.
  The **Admin Dashboard** then shows a restart-required banner for the
  affected component(s), which restarts them mode-aware (native, Compose,
  Terraform, or Kubernetes, matching however the stack is actually running)
  when you click **Restart now** (see [`docs/api.md`](api.md#config-wizard))
  — you never need to run a restart command yourself.
- **Enabling an observability toggle actually starts it.** Flipping
  `tracing`/`error_tracking`/`monitoring`/`log_aggregation` to enabled
  reconciles the Compose observability stack the same way `nyxgpt ops
  observability` does (and tears it down the same way `nyxgpt ops stop
  --target observability` does when all four are disabled) — so enabling a
  stack in the wizard results in a working dashboard, not a dangling flag.
- **Secrets are never echoed back.** `[auth] api_key`, `[error_tracking]
  dsn`/`admin_password`, and `[monitoring] grafana_admin_password` are shown
  as "set" plus a masked preview (e.g. `abcd****wxyz`); the wizard's input
  for these is always blank, and leaving it blank on save keeps the existing
  value. Only typing a new value rotates it.
- **Single-user scope.** The wizard edits one global `config.ini` — there is
  no per-session configuration.

### Option 4: Guided secrets setup

`nyxgpt wizard` generates `[auth] api_key` for you, but three secrets still
require a human to go fetch them from an external service: `[openai]
api_key`, `[github] pat`, and (if you want to change the auto-generated
default) `[auth] api_key` itself. `nyxgpt secrets setup` walks through each
one with masked (`getpass`) entry, a plain-language description of what it's
for, exactly where to obtain it, and format validation before it's written:

```bash
nyxgpt secrets setup              # set anything not already set
nyxgpt secrets setup --reconfigure  # re-prompt for every secret, even set ones
```

Idempotent: re-running with no `--reconfigure` skips anything already set
(showing its masked value), so it's safe to run repeatedly, e.g. after a
fresh install before any of the three are set. The same guided flow is
available from the web UI at **`/admin/secrets`** (masked entry, per-key
help, and a "Generate for me" option for `[auth] api_key`), backed by
`GET|POST /api/v1/config/secrets`.

See [Canonical secret store & sync to GitHub Actions](#canonical-secret-store--sync-to-github-actions)
below for what to do with these once they're set.

### Option 5: Guided AWS credentials setup

Any `nyxgpt cloud` command, and `[secrets] provider` above, needs AWS
credentials available to boto3. `nyxgpt cloud credentials-setup` (and the
`/admin` **AWS Credentials** wizard) walks through the same masked-entry,
what-it-is/where-to-get-it treatment as Option 4 -- but the AWS access key
ID/secret access key are **never written to `config.ini`**, routed instead
to `~/.aws/credentials`, the OS keychain, or left alone if already available
some other way (instance role, SSO, environment variables). See
[`docs/cloud.md`](cloud.md#guided-aws-credentials-setup-p6-13-3512) for full
details, including the `[cloud]` section this writes.

---

## Canonical secret store & sync to GitHub Actions

Several external tokens are **write-once**: the issuing service (Slack,
GitHub, OpenAI) shows the value only at creation time and never lets you
read it back afterward. If you then also paste that same value into GitHub's
**Settings → Secrets and variables → Actions** UI by hand, you now have two
copies with no way to verify they still match -- and when one needs to
rotate, it's easy to update only one of them and not notice until CI starts
failing with stale credentials.

`~/.nyxGPT/config.ini` is the **single canonical store** for these tokens.
Set them once via `nyxgpt secrets setup` (or `/admin/secrets`), then push the
declared subset that CI needs to this repo's GitHub Actions secrets with:

```bash
nyxgpt ops secrets-sync            # push config.ini's mapped secrets to Actions
nyxgpt ops secrets-sync --dry-run  # show which secrets *would* be pushed, by name only
```

This is **one direction only**: config.ini → GitHub Actions. Nothing is ever
read back from GitHub (the Actions secrets API can't return a value anyway).
Also available from the web UI at `/admin/secrets` ("Sync to GitHub Actions
secrets"), backed by `POST /api/v1/config/secrets/sync`.

- **What gets synced is a declared mapping, not "everything."**
  `nyxgpt.config.SECRETS_SYNC_MANIFEST` is the single place that maps a
  `config.ini` key to its GitHub Actions secret name. A key not listed there
  is never pushed, even if it's a secret-looking field. Today's manifest:

  | `config.ini` key | Actions secret |
  |---|---|
  | `[github] claude_code_oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN` |
  | `[github] developer_agent_token` | `DEVELOPER_AGENT_TOKEN` |
  | `[github] scrummaster_agent_token` | `SCRUMMASTER_AGENT_TOKEN` |
  | `[github] review_agent_token` | `REVIEW_AGENT_TOKEN` |
  | `[monitoring] slack_bot_token` | `SLACK_BOT_TOKEN` |
  | `[pypi] pypi_token` | `PYPI_API_TOKEN` |

  Adding a new synced secret is a one-line addition to that dict -- see
  `src/nyxgpt/config.py`.

  `[pypi] pypi_token` is the release ceremony's upload token
  (`scripts/release_ceremony.sh` Phase 2). Syncing it lets the
  [release-candidate publish workflow](cloud.md#release-candidates-acceptance-testing-unreleased-code)
  upload pre-releases with the same credential instead of a second copy
  pasted into GitHub's settings. Leave it unset if that workflow uses PyPI
  Trusted Publishing (OIDC) instead -- it falls back automatically.
- **`[github] pat` authenticates the sync call itself** (via the GitHub REST
  API's Actions secrets endpoints) and is not, itself, a sync target.
- **Values never appear in logs, tracebacks, or command output.** Sync
  results report which secrets were set/updated *by name only*; a failure
  names the key and how to fix it (e.g. missing `[github] pat`, wrong
  `repo_owner`/`repo_name`), never the value. This matches the existing
  `***redacted***` convention `get_effective_config_summary` uses for
  `[error_tracking] dsn`/`[monitoring] grafana_admin_password` -- see
  `[openai]`/`[github]`/`[monitoring]` below for which fields this covers.
- **Encryption.** Values are sealed with the repo's Actions public key
  (libsodium sealed-box, via PyNaCl) before they're ever sent, per
  [GitHub's Actions secrets API](https://docs.github.com/en/rest/actions/secrets).

---

## Container data layout (`~/.nyxGPT/volumes/`)

`~/.nyxGPT` holds two distinct kinds of state, both on the host filesystem
(no opaque Docker-managed storage, see issue #3346):

- **Native process state** — `config.ini`, `sessions_dir`/`vectorstore_dir`
  (see the `[nyxgpt]` section below), `logs/`, `cache/`: written directly by
  the native `api`/`web`/`nyxgpt` processes running as your user.
- **Container data** — `~/.nyxGPT/volumes/<component>/`: bind-mounted into
  the Docker/Terraform-managed containers (Ollama, Cassandra, the
  containerized api, and the opt-in observability stack). These are separate
  from the native state above; a containerized `api` never reads or writes
  your native `sessions_dir`/`vectorstore_dir` directly, since it sees its
  own `/root/.nyxGPT` bind-mounted from `~/.nyxGPT/volumes/nyxgpt-data`.

| Host directory | Component | Shared across |
|---|---|---|
| `~/.nyxGPT/volumes/ollama` | Pulled Ollama models | Compose + Terraform |
| `~/.nyxGPT/volumes/cassandra` | Cassandra data (chats/RAG vectors) | Compose + Terraform + native `nyxgpt ops install` |
| `~/.nyxGPT/volumes/nyxgpt-data` | Containerized api's home | Compose + Terraform |
| `~/.nyxGPT/volumes/prometheus`, `grafana`, `loki`, `glitchtip-postgres`, `glitchtip-uploads` | Opt-in observability stack | Compose only today |

See [docker-compose.md#volumes](docker-compose.md#volumes) and
[terraform.md](terraform.md) for exactly which service/resource mounts each
directory, and [`nyxgpt ops migrate-volumes`](ops.md) for migrating
pre-#3346 named-volume data into this layout on upgrade.

**Backup guidance:** because both native state and container data now live
under one host directory, backing up `~/.nyxGPT` (as a whole) captures
everything — chats, RAG vectors, pulled models, and config — regardless of
which deployment mode (native, Compose, Terraform) produced it.

---

## `[nyxgpt]` section

General application behavior.

```ini
[nyxgpt]
default_model = qwen2.5:0.5b
session_backend = file
sessions_dir = ~/.nyxGPT/sessions
vectorstore_dir = ~/.nyxGPT/vectorstore
chat_timeout_seconds = 60
auto_summarize_enabled = true
auto_summarize_after_messages = 5
auto_sync_filename = true
system_prompt_minimize = false
```

| Key | Description |
|---|---|
| `default_model` | Ollama model name used when none is specified (default: `llama3.1:8b`) |
| `session_backend` | Chat session storage backend: `file` (default) or `cassandra`. `cassandra` stores sessions in the stack's Cassandra (`[rag] cassandra_*`), so every deployment mode pointed at the same Cassandra shares one session list and multi-instance access is safe; existing `sessions_dir` files are imported once (idempotently) on API startup. Can be overridden per-process with the `NYXGPT_SESSION_BACKEND` environment variable. See [session-storage.md](session-storage.md) (#3590). |
| `sessions_dir` | Directory for chat session storage with the `file` backend (default: `~/.nyxGPT/sessions`). **Deprecated** for multi-instance / containerized deployments: under `session_backend = cassandra` it is only read once as the migration source. Must resolve *strictly inside* your home directory or the system temp directory -- values outside either, or equal to one of those roots exactly (e.g. `sessions_dir = ~`), are rejected at load time (CodeQL py/path-injection hardening, #3639/#3657). |
| `vectorstore_dir` | Directory for vector embeddings and RAG data (default: `~/.nyxGPT/vectorstore`). Same home-directory/temp-directory restriction as `sessions_dir`. |
| `chat_timeout_seconds` | Timeout for a single chat request (default: `180`) |
| `auto_summarize_enabled` | Automatically generate session title/summary/tags |
| `auto_summarize_after_messages` | Trigger auto-summarization after N messages (0 to disable) |
| `auto_sync_filename` | Whether the on-demand filename-sync operations (the "sync filename" session action and `POST /sessions/{name}/sync-filename`/`rename`) are allowed to rename a session's file to match its title. **Not** applied automatically after a chat turn -- auto-summarization only ever writes the title/summary/tags onto the *existing* session file. Renaming a session mid-conversation would silently orphan whatever name a client (web UI, CLI) is still addressing it by, so it never happens as a side effect of chat (#3459). |
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
| `base_url` | Base URL of the Ollama HTTP API (default: `http://127.0.0.1:11434`) |
| `default_model` | Default model for Ollama (optional override of `nyxgpt.default_model`) |

**Note:** If `default_model` is not set, nyxGPT uses `nyxgpt.default_model` instead.

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
| `host` | Bind address for the API server (default: `127.0.0.1`) |
| `port` | Port for the API server (default: `8000`) |

**Non-loopback binds require auth:** setting `host` to anything other than a
loopback address (`127.0.0.1`, `localhost`, `::1`) requires `[auth] enabled
= true`, or the API refuses to start — see [`[auth]`](#auth-section) above
and [`docs/security.md`](security.md#network-security).

---

## `[logging]` section

Centralized logging configuration. This is the **single source of truth** for all logging settings, managed by `src/nyxgpt/logging.py`.

```ini
[logging]
level = INFO
dir = ~/.nyxGPT/logs
```

| Key | Description |
|---|---|
| `level` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, etc.) |
| `dir` | Directory where logs are written (default: `~/.nyxGPT/logs`) |

All components use this centralized configuration, each writing to its own rotated file under
`{dir}`: the API process writes `api.log`, and every `nyxgpt` CLI invocation (including
`nyxgpt ops ...`) writes `cli.log` -- kept separate so Loki's `service_name` label (see
[docker-compose.md#log-aggregation](docker-compose.md#log-aggregation)) is accurate per line
instead of guessed.

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
- Restrict file permissions: `chmod 600 ~/.nyxGPT/config.ini`
- Never commit `~/.nyxGPT/config.ini` to version control
- Rotate keys regularly and immediately if compromise is suspected

**Note:** Authentication configuration is **hot-reloadable** and takes effect immediately without restart.

**Enforced at startup:** if `[api] host` (below) is bound non-loopback while
`enabled` isn't `true`, the native API refuses to start rather than serving
unauthenticated — see
[`docs/security.md#network-security`](security.md#network-security).

**Docker Compose:** the `web` and `api` containers need this same key as the
`NYXGPT_AUTH_API_KEY` environment variable (see `docker-compose.yml`).
config.ini stays the single source of truth — run `nyxgpt ops env-sync` to
derive `.env`'s value from it rather than setting `.env` independently.

Instead of editing `config.ini` by hand, you can view the enabled state and a
masked key, toggle authentication, and rotate the key from the admin
dashboard's Access Management panel (`/admin/dashboard`), backed by
`GET`/`POST /api/v1/admin/access` — see
[`docs/api.md`](api.md#admin-dashboard). The full Configuration Wizard
(`/admin`, see above) also covers `enabled`/`header`/`api_key` on its API &
Auth step, backed by `GET`/`POST /api/v1/config/sections`.

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
embedding_auto_pull = true
embedding_pull_timeout_seconds = 600
chunk_size = 800
chunk_overlap = 100
overlap_strategy = trailing
preserve_headings = true
sentence_aware = true
chat_top_k = 5
min_score = 0.0
good_score_threshold = 0.7
medium_score_threshold = 0.4
max_chunks = 6
chat_context_max_chars = 2400
dedupe = true
enable_query_expansion = false
include_scores = false
include_headers = true
cassandra_hosts = 127.0.0.1
cassandra_port = 9042
cassandra_keyspace = nyxgpt
cassandra_table = rag_chunks
```

| Key | Description |
|---|---|
| `enable_chat_context` | Enable RAG context injection for chat (hot-reloadable) |
| `enabled` | **Legacy alias** for `enable_chat_context` (deprecated, use `enable_chat_context` instead) |
| `embedding_model` | Ollama embedding model. Blank has no fixed fallback of its own -- it falls back to whatever `nyxgpt.default_model` is at call time, so the Configuration Wizard shows it genuinely empty rather than badging a default here |
| `embedding_dim` | Vector dimensionality (must match Cassandra VECTOR dimension) |
| `embedding_batch_size` | Batch size for embedding requests (smaller = lower memory, slower) |
| `embedding_timeout_seconds` | Timeout for each embedding batch request to Ollama |
| `embedding_auto_pull` | Pull `embedding_model` into Ollama the first time it is needed (default: true). Without it, the first upload on a fresh install fails with `model "nomic-embed-text" not found, try pulling it first`. Set to false on air-gapped hosts and pull the model yourself with `nyxgpt models pull <model>` |
| `embedding_pull_timeout_seconds` | Timeout for that automatic pull (default: 600); the first ingest blocks while the model downloads |
| `embedding_async_enabled` | Enable async/parallel processing for embedding generation (default: false) |
| `embedding_max_workers` | Maximum number of parallel workers for async embedding (default: 4, recommended: 2-8) |
| `embedding_gpu_enabled` | Enable GPU optimization and detection (default: false, requires nvidia-smi for NVIDIA GPUs) |
| `embedding_adaptive_batching` | Enable adaptive batch sizing based on available memory and GPU (default: false) |
| `chunk_size` | Maximum characters per text chunk |
| `chunk_overlap` | Character overlap between adjacent chunks |
| `overlap_strategy` | Overlap method: `trailing` (characters), `sentence` (complete sentences), or `semantic` (paragraphs) |
| `preserve_headings` | Keep Markdown headings with their content for better context (default: true) |
| `sentence_aware` | Split on sentence boundaries for better semantic chunking (default: true) |
| `chat_top_k` | Number of candidate chunks to retrieve from vector store |
| `top_k` | **Legacy alias** for `chat_top_k` (deprecated, use `chat_top_k` instead) |
| `min_score` | Minimum similarity score required for chunk inclusion |
| `good_score_threshold` | Threshold for high-confidence scores (green visual indicator in UI, default: 0.7) |
| `medium_score_threshold` | Threshold for medium-confidence scores (yellow visual indicator in UI, default: 0.4) |
| `max_chunks` | Hard cap on number of chunks injected into prompt |
| `chat_context_max_chars` | Maximum total characters of retrieved context |
| `dedupe` | Remove duplicate or near-duplicate chunks before injection |
| `enable_query_expansion` | Generate alternative phrasings to improve retrieval |
| `expansion_model` | Model for query expansion (optional, defaults to nyxgpt.default_model) |
| `include_scores` | Include similarity scores in context headers (debugging only) |
| `include_headers` | Include per-chunk headers like "[Context 1]" in injected context |
| `cassandra_hosts` | Cassandra host(s) (default: `127.0.0.1`) |
| `cassandra_port` | Cassandra port (default: `9042`) |
| `cassandra_keyspace` | Cassandra keyspace for RAG (default: `nyxgpt`) |
| `cassandra_table` | Cassandra table name for RAG chunks (default: `rag_chunks`) |
| `cassandra_pool_size` | Number of core connections per host in the driver-level pool (integer ≥ 1, default: `2`) |
| `cassandra_health_check_interval` | Seconds between automatic health check queries; a check is run on the next `get_session()` call once this interval has elapsed (float > 0, default: `30.0`) |
| `cassandra_reconnect_max_attempts` | Maximum number of reconnection attempts before giving up (integer ≥ 1, default: `3`) |
| `cassandra_batch_size` | Maximum number of chunk upserts grouped into a single Cassandra batch during ingestion (integer 1-100, default: `20`); batches are also flushed early once their estimated payload nears Cassandra's default 50KB batch size limit, so this is a ceiling and does not need to be tuned down for larger `embedding_dim`/`chunk_size` values |
| `vector_similarity_function` | Distance metric for the Cassandra SAI vector index and ANN scoring: `cosine`, `dot_product`, or `euclidean` (default: `cosine`). Changing this after ingestion requires recreating the vector index. |
| `ann_oversample_factor` | Multiplier applied to ANN candidate fetch size to improve recall (range: 1.0-5.0, default: `1.0`) |
| `cassandra_batch_query_concurrency` | Maximum ANN queries executed concurrently within a single batch search call (range: 1-32, default: `4`) |
| `debug_mode` | Collect and return detailed RAG troubleshooting metrics (timing, query analysis, embedding details, filtering stats) (default: `false`) |
| `enable_hybrid_search` | Combine BM25 keyword search with vector similarity search; when `false`, uses vector-only (legacy) search (default: `true`) |
| `bm25_k1` | BM25 term-frequency saturation parameter (typical range: 1.2-2.0, default: `1.5`) |
| `bm25_b` | BM25 document-length normalization parameter (0.0 = none, 1.0 = full, default: `0.75`) |
| `rrf_k` | Reciprocal Rank Fusion constant for rank weighting; higher = less emphasis on rank differences (default: `60`) |
| `hybrid_alpha` | Optional weighted-fusion alpha (vector vs. keyword weight) that overrides RRF fusion when set |
| `enable_reranking` | Re-score top retrieval results with a cross-encoder model for improved precision (default: `false`) |
| `reranker_model` | Model used for reranking (optional, defaults to `nyxgpt.default_model`) |
| `rerank_top_n` | Number of results to keep after reranking (default: `3`) |
| `reranker_timeout_seconds` | Timeout per reranking score request; total time scales with number of results scored (default: `30`) |

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

Absolute paths for operational components. The setup wizard (`nyxgpt wizard`,
also run automatically by `nyxgpt ops install` on a fresh machine) fills these
in automatically from the running environment — the repository checkout it
runs from, the active Python interpreter, and `node`/`npm` found on `PATH`
(falling back to the standard Homebrew locations). The values below are
illustrative.

```ini
[paths]
repo_dir = /path/to/nyxGPT
venv_python = /path/to/nyxGPT/.venv/bin/python
node_bin = /opt/homebrew/bin/node
npm_bin = /opt/homebrew/bin/npm
```

| Key | Description |
|---|---|
| `repo_dir` | Absolute path to the nyxGPT repository |
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
- Or run `nyxgpt secrets setup` (or `/admin/secrets`) for guided, masked
  entry instead of hand-editing this field -- see [Guided secrets
  setup](#option-4-guided-secrets-setup). This key is write-once (OpenAI
  won't show it again), so config.ini is its canonical copy -- see
  [Canonical secret store & sync to GitHub
  Actions](#canonical-secret-store--sync-to-github-actions).

**Security best practices:**
- Keep your API key confidential - never commit it to version control
- Restrict file permissions: `chmod 600 ~/.nyxGPT/config.ini`
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
dev_agent = nyxGPT-developer-agent
review_agent = nyxGPT-review-agent
scrum_agent = nyxGPT-scrummaster-agent
human_owner = your-username
status_field = Status
status_backlog = Backlog
status_in_progress = In Progress
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
   - Store it in the `pat` field -- or run `nyxgpt secrets setup` (or
     `/admin/secrets`) for guided, masked entry, and format validation. This
     token is write-once (GitHub won't show it again), so config.ini is its
     canonical copy: see [Guided secrets
     setup](#option-4-guided-secrets-setup) and [Canonical secret store &
     sync to GitHub
     Actions](#canonical-secret-store--sync-to-github-actions). `pat`
     authenticates `nyxgpt ops secrets-sync`'s calls to the Actions secrets
     API but is not itself pushed there.

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

**Note:** `scrummaster_agent_token`, `developer_agent_token`,
`review_agent_token`, and `claude_code_oauth_token` are synced to this
repo's GitHub Actions secrets by `nyxgpt ops secrets-sync` -- see
[Canonical secret store & sync to GitHub
Actions](#canonical-secret-store--sync-to-github-actions).

---

## `[secrets]` section

Cloud secrets provider for `[auth] api_key`, `[openai] api_key`, and
`[github] pat` (P6-10, #3507) -- AWS-only, and only relevant on a cloud
deploy. Local deploys leave this section unset and are unaffected.

```ini
[secrets]
provider =
region =
ssm_prefix = /nyxgpt
secretsmanager_id = nyxgpt
```

| Key | Description |
|---|---|
| `provider` | `""` (default, local deploy) reads the three credentials from `config.ini` as usual. `ssm` or `secretsmanager` resolves them from AWS instead -- see [`docs/cloud.md`](cloud.md#cloud-secrets-ssm--secrets-manager) for full setup, layout, and rotation. |
| `region` | AWS region to resolve secrets from. Blank falls back to boto3's normal region resolution. |
| `ssm_prefix` | SSM Parameter Store path prefix (`provider = ssm`). Each credential is one parameter at `f"{ssm_prefix}/{key}"`. |
| `secretsmanager_id` | Secrets Manager secret id/ARN (`provider = secretsmanager`). Holds one JSON object with all three credentials. |

See [`docs/cloud.md`](cloud.md#cloud-secrets-ssm--secrets-manager) for setup, the AWS-side layout, IAM permissions, and rotation.

---

## `[cloud]` section

The AWS identity *reference* nyxGPT uses for its own AWS API calls (`nyxgpt
cloud allow-ip`, cloud deploy, `[secrets]` resolution above) -- P6-13,
#3512. Excluded from the general Configuration Wizard: it has its own
guided flow instead (`nyxgpt cloud credentials-setup` / the `/admin` AWS
Credentials wizard), since the actual AWS access key pair must never be
hand-edited into this file.

```ini
[cloud]
profile = nyxgpt
region =
credentials_source =
```

| Key | Description |
|---|---|
| `profile` | AWS CLI profile name nyxGPT uses when calling boto3. |
| `region` | Default AWS region for nyxGPT's own AWS API calls. |
| `credentials_source` | Where the access key pair for `profile` was routed by the guided setup: `profile` (`~/.aws/credentials`), `keychain` (OS keychain), or `ambient` (already available some other way). Set by the guided flow -- not meant to be hand-edited. |

This section **never holds an AWS access key or secret access key** -- see
[`docs/cloud.md`](cloud.md#guided-aws-credentials-setup-p6-13-3512) for
where those actually go.

---

## `[tracing]` section

Distributed tracing via OpenTelemetry. Enabled by default (2026-07-28 owner
decision) and local-only: no data is ever sent to an external/cloud
endpoint, only to a local collector.

```ini
[tracing]
enabled = true
service_name = nyxgpt-api
otlp_endpoint = http://localhost:4318/v1/traces
jaeger_ui_url = http://localhost:16686
```

| Key | Description |
|---|---|
| `enabled` | Enable distributed tracing (default: `true`) |
| `service_name` | Service name attached to every span (default: `nyxgpt-api`) |
| `otlp_endpoint` | OTLP/HTTP endpoint of the local collector spans are exported to (default: `http://localhost:4318/v1/traces`) |
| `jaeger_ui_url` | URL of the local Jaeger UI -- a debug tool now (#3411); traces are browsed inside Grafana instead, via the SRE Overview tile |

When enabled, HTTP requests (chat/RAG paths), Ollama calls, and Cassandra
queries all get their own spans. Requires the `tracing` Compose profile
(local OTel collector + Jaeger all-in-one), started automatically by
`nyxgpt ops install` (or standalone via `nyxgpt ops observability` -- never
a raw `docker compose` command, see
[`docs/ops.md`](ops.md#nyxgpt-ops-observability)):

```bash
nyxgpt ops observability
```

`docker/config.docker.ini` (the Compose deployment's config) also ships
with `[tracing] enabled = true` out of the box, matching
`~/.nyxGPT/config.ini`'s (native deployment) default -- both start with
tracing on. If the local collector isn't reachable yet (fresh install,
`--skip-observability`, collector restart), span export degrades
gracefully: chat/API behavior is unaffected and `nyxgpt ops doctor`
reports OTLP reachability (#3350) as the diagnostic surface.

See [`docs/api.md`](api.md#distributed-tracing) for the `GET /api/v1/tracing`
status endpoint.

---

## `[error_tracking]` section

Self-hosted error tracking via the Sentry SDK protocol. Opt-in and
local-only: there is no default DSN, so error tracking stays fully inert
until `enabled = true` and a `dsn` are set -- both of which
`nyxgpt ops glitchtip-init` fills in automatically (see below). Nothing
here ever talks to Sentry's own SaaS.

```ini
[error_tracking]
enabled = false
dsn =
environment = development
release =
traces_sample_rate = 0.0
glitchtip_ui_url = http://localhost:8080
admin_email =
admin_password =
```

| Key | Description |
|---|---|
| `enabled` | Enable error tracking (default: `false`). Flipped to `true` automatically once `nyxgpt ops glitchtip-init` provisions a DSN |
| `dsn` | DSN of your self-hosted GlitchTip project. Empty by default; filled in automatically by `nyxgpt ops glitchtip-init`, or paste one yourself exactly as GlitchTip's UI shows it (`localhost` host) -- a containerized API automatically rewrites that host to the `glitchtip` Compose service name at startup, so there's nothing to edit by hand for either deployment mode |
| `environment` | Environment tag attached to every event (e.g. `development`, `production`; default: `development`) |
| `release` | Release tag attached to every event, for release tracking. Blank omits it |
| `traces_sample_rate` | Fraction of requests also sampled for performance monitoring, `0.0`-`1.0` (`0.0` disables performance monitoring; only exceptions are captured) |
| `glitchtip_ui_url` | URL of the local GlitchTip UI (click-through target from Grafana's GlitchTip panels, #3411), and the API base URL `nyxgpt ops glitchtip-init` provisions against |
| `admin_email` | GlitchTip admin login `nyxgpt ops glitchtip-init` provisions (or reuses if it already exists). Blank picks a default (`admin@nyxgpt.local`) |
| `admin_password` | GlitchTip admin password. Blank generates a strong one and saves it back here (chmod 600, same trust model as `[auth] api_key`) -- never returned by any API endpoint. Print it with `nyxgpt ops credentials` (or `nyxgpt cloud credentials` for a deployment) rather than reading this file by hand -- see [`docs/ops.md`](ops.md#nyxgpt-ops-credentials) |

When enabled with a DSN, unhandled backend exceptions are reported
automatically, and web UI client errors reported via
`POST /api/v1/error-tracking/report` are forwarded too.

The `errors` Compose profile itself already starts automatically with
`nyxgpt ops install` (or standalone via `nyxgpt ops observability` --
never a raw `docker compose` command, see
[`docs/ops.md`](ops.md#nyxgpt-ops-observability)), and `nyxgpt ops
glitchtip-init` -- also run automatically as part of `nyxgpt ops install`
-- provisions the admin user, organization, project, and DSN with no
manual sign-in step; see [`docs/ops.md`](ops.md#nyxgpt-ops-glitchtip-init).

nyxGPT reports via the **Python** `sentry_sdk` (`src/nyxgpt/error_tracking.py`)
-- if GlitchTip's own onboarding screen shows Node.js/`@sentry/node` setup
instructions when you create a project, ignore them.

See [`docs/api.md`](api.md#error-tracking) for the full guided setup steps
and the `GET /api/v1/error-tracking` status endpoint.

---

## `[monitoring]` section

Grafana dashboards backed by Prometheus. Opt-in and local-only: no metrics
are ever sent to an external/cloud service, only to a local Prometheus
server that scrapes this API's `/metrics` endpoint.

```ini
[monitoring]
enabled = false
grafana_ui_url = http://localhost:3001
prometheus_ui_url = http://localhost:9090
grafana_admin_password =
slack_webhook_url =
slack_bot_token =
```

| Key | Description |
|---|---|
| `enabled` | Enable monitoring (default: `false`) |
| `grafana_ui_url` | URL of the local Grafana UI -- Grafana is the single pane of glass (#3411); the Admin Dashboard's SRE Overview tile opens it in a new tab, built from this URL |
| `prometheus_ui_url` | URL of the local Prometheus UI -- a debug tool now (#3411); metrics are browsed inside Grafana instead |
| `grafana_admin_password` | Grafana's admin password, auto-generated by `nyxgpt wizard`, never returned by `GET /api/v1/monitoring`. Optional: if left unset, `nyxgpt ops install` manages its own generated secret at `~/.nyxGPT/secrets/grafana-admin-password` instead. Either way, `nyxgpt ops install` deterministically resets the running Grafana container's actual admin password to match via `grafana cli admin reset-admin-password` -- this works on both a fresh and a long-lived Grafana volume, unlike `GF_SECURITY_ADMIN_PASSWORD`, which only applies on first boot. To log in, print whichever of the two applies with `nyxgpt ops credentials` (or `nyxgpt cloud credentials` for a deployment) -- see [`docs/ops.md`](ops.md#nyxgpt-ops-credentials). See [security.md](security.md#api-key-management). |
| `slack_webhook_url` | Slack incoming webhook URL for Grafana's `nyxgpt-slack` alerting contact point. Optional: alert rules fire and stay visible in Grafana's Alerting UI either way -- this only controls whether firing alerts also post to Slack. Set from the config wizard's Additional Settings (masked as a secret) or directly in config.ini, then run `nyxgpt ops env-sync` (or `nyxgpt ops install`) to provision it. See [alerting.md](alerting.md#slack-contact-point). |
| `slack_bot_token` | Slack bot token the `notify-merge-conflicts` CI workflow uses to post notifications. Write-once (Slack shows it only at creation) -- config.ini is its canonical copy; run `nyxgpt ops secrets-sync` to push it to this repo's `SLACK_BOT_TOKEN` Actions secret instead of pasting it into GitHub's Secrets UI by hand. See [Canonical secret store & sync to GitHub Actions](#canonical-secret-store--sync-to-github-actions). |

Requires the `monitoring` Compose profile (local Prometheus + Grafana),
started automatically by `nyxgpt ops install` (or standalone via `nyxgpt
ops observability` -- never a raw `docker compose` command, see
[`docs/ops.md`](ops.md#nyxgpt-ops-observability)):

```bash
nyxgpt ops env-sync   # derive .env's GRAFANA_ADMIN_PASSWORD and the Slack webhook from config.ini
nyxgpt ops observability
```

`docker/config.docker.ini` ships with `[monitoring] enabled = true` out of
the box, since the profile above starts automatically -- the `false`
default shown here is `~/.nyxGPT/config.ini`'s (native deployment)
baseline.

See [`docs/api.md`](api.md#monitoring-dashboards) for the
`GET /api/v1/monitoring` status endpoint.

---

## `[log_aggregation]` section

Centralized log search backed by Loki + promtail, shipping this API's log
files under `~/.nyxGPT/logs`. Opt-in, local-only, and reduced-footprint --
not a full ELK stack -- since no logs are ever sent to an external/cloud
service.

```ini
[log_aggregation]
enabled = false
grafana_explore_url = http://localhost:3001/explore
```

| Key | Description |
|---|---|
| `enabled` | Enable log aggregation (default: `false`) |
| `grafana_explore_url` | URL of the Grafana Explore view (pointed at the Loki datasource), used only by the self-heal/canary pages' own curated per-component Explore links. General log search happens in Grafana's Logs Drilldown app instead (#3411), reached via the SRE Overview tile |

Requires the `logging` Compose profile (local Loki + promtail), plus the
`monitoring` profile (Grafana, pre-provisioned with a Loki datasource and a
Logs Explorer dashboard) -- both start automatically with `nyxgpt ops
install` (or standalone via `nyxgpt ops observability` -- never a raw
`docker compose` command, see
[`docs/ops.md`](ops.md#nyxgpt-ops-observability)):

```bash
nyxgpt ops observability
```

`docker/config.docker.ini` ships with `[log_aggregation] enabled = true`
out of the box, since the profiles above start automatically -- the
`false` default shown here is `~/.nyxGPT/config.ini`'s (native deployment)
baseline. The curated LogQL queries below are also returned by `GET
/api/v1/log-aggregation`, provisioned as code alongside the Operational
Logs dashboard.

See [`docs/api.md`](api.md#log-aggregation) for the
`GET /api/v1/log-aggregation` status endpoint.

---

## `[pdf]` section

Controls OCR extraction for image-based PDFs during RAG ingestion.

```ini
[pdf]
ocr_enabled = true              # OCR PDFs with little/no extractable text
ocr_min_text_threshold = 50     # chars below which OCR is attempted
ocr_dpi = 300                   # render DPI (higher = better, slower)
ocr_lang = eng                  # ISO 639-2 code(s), e.g. eng+spa
ocr_psm = 3                     # Tesseract page segmentation mode
# tesseract_cmd = /usr/bin/tesseract   # path if not on PATH
```

Requires Tesseract installed on the host. See `docs/rag.md` for ingestion details.

---

## `[cache]` section

Optional caching layers to avoid recomputing embeddings, RAG queries, and responses.
Each cache has an independent enable flag, backend (`memory` or `disk`), size cap, and TTL.

```ini
[cache]
embedding_cache_enabled = false
embedding_cache_backend = memory      # memory | disk
embedding_cache_max_size = 1000
embedding_cache_ttl_seconds = 86400
# ...matching query_cache_* and response_cache_* keys
```

Memory caches use LRU eviction; disk caches persist across restarts.

| Key | Description |
|---|---|
| `embedding_cache_enabled` | Cache embeddings to avoid recomputing vectors for identical texts (default: `false`) |
| `embedding_cache_backend` | `memory` (fast, volatile, LRU eviction) or `disk` (persistent pickle files) |
| `embedding_cache_max_size` | Maximum cached embedding entries (memory backend only, default: `1000`) |
| `embedding_cache_ttl_seconds` | TTL in seconds; `0` = no expiration (default: `3600` memory, `86400` disk) |
| `embedding_cache_dir` | Cache directory for disk backend (default: `~/.nyxGPT/cache/embeddings`) |
| `response_cache_enabled` | Cache LLM responses for identical prompts (default: `false`). Only recommended for deterministic use cases or short TTLs — may serve stale responses if context changes. |
| `response_cache_backend` | `memory` or `disk` |
| `response_cache_max_size` | Maximum cached response entries (memory backend only, default: `100`) |
| `response_cache_ttl_seconds` | TTL in seconds (default: `1800` memory, `3600` disk) |
| `response_cache_dir` | Cache directory for disk backend (default: `~/.nyxGPT/cache/responses`) |
| `query_cache_enabled` | Cache fully retrieved/fused/reranked RAG results for repeated queries, skipping vector search, BM25, fusion, and reranking on a hit (default: `false`). Auto-invalidated on document ingestion/update/deletion. Monitor via `GET /api/v1/rag/cache/stats`. |
| `query_cache_backend` | `memory` or `disk` |
| `query_cache_max_size` | Maximum cached query results (memory backend only, default: `500`) |
| `query_cache_ttl_seconds` | TTL in seconds (default: `300` memory, `600` disk) |
| `query_cache_dir` | Cache directory for disk backend (default: `~/.nyxGPT/cache/queries`) |

---

## `[rate_limit]` section

Token-bucket rate limiting on the API (disabled by default; localhost-only app).

```ini
[rate_limit]
enabled = false
requests_per_second = 10
burst_size = 20                 # max tokens in the bucket
```

Enable this when exposing the API beyond localhost.

Rate limiting uses a token bucket algorithm to track requests per IP address.
When enabled, all API responses include rate limit headers:

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

## `[batch]` section

Request batching for embedding throughput.

```ini
[batch]
enabled = false
batch_size = 32
wait_time_ms = 50               # max wait to fill a batch
```

---

## `[canary]` section

Local canary rollout controls (see `/admin/canary`).

```ini
[canary]
namespace = nyxgpt
total_replicas = 4
step_percent = 25                       # traffic increment per step
error_rate_threshold_percent = 5        # auto-rollback threshold
latency_p95_threshold_ms = 2000.0       # auto-rollback threshold
min_requests_for_evaluation = 20
```

---

## `[self_heal]` section

Watchdog that restarts unhealthy Docker Compose components (see `docs/self-healing.md`,
`/admin/self-heal`). Its own restart metric (`nyxgpt_selfheal_restarts_total`) counts only
these autonomous restarts -- operator-initiated `nyxgpt ops` actions (install/down/restart/
stop/observability, from the CLI or the admin dashboard) are recorded separately as
`nyxgpt_ops_actions_total`; see [self-healing.md's "Self-heal restarts vs. operator (`nyxgpt
ops`) actions"](self-healing.md#self-heal-restarts-vs-operator-nyxgpt-ops-actions) for why
the two must never be conflated when reading a dashboard.

```ini
[self_heal]
enabled = false
check_interval_seconds = 30
max_consecutive_restarts = 5            # stop after N failed restarts
backoff_seconds = 60                    # min wait between restarts
```

---

## `[web]` section

Settings for the Next.js web client / proxy.

```ini
[web]
host = 127.0.0.1
port = 3000
api_base_url = http://127.0.0.1:8000    # API the web app proxies to
```

---

## Web tier environment variables

The Next.js process doesn't read `config.ini` directly (only the Python API
does) -- it's configured through process environment variables and, for
anything inlined into the browser bundle, Next.js build args (`ARG`/`ENV` in
`web/Dockerfile`, `build.args` in `docker-compose.yml`). These cover the
correlation backbone (#3430) added alongside the `[tracing]`/
`[error_tracking]` sections above:

| Variable | Where it applies | Default | Purpose |
| --- | --- | --- | --- |
| `NYXGPT_API_BASE_URL` | server (runtime env) | `http://127.0.0.1:8000` | Base URL `apiProxy.ts`'s `apiFetch` proxies to (existing, pre-#3430). |
| `NYXGPT_AUTH_API_KEY` | server (runtime env) | unset | `X-API-Key` attached to proxied requests (existing, pre-#3430). |
| `NYXGPT_OTLP_ENDPOINT` | server (runtime env) | `http://localhost:4318/v1/traces` | Where `instrumentation.ts`'s `@vercel/otel` exports spans -- same collector as `[tracing] otlp_endpoint`. In Compose mode this must be the `otel-collector` service hostname (already set in `docker-compose.yml`), since "localhost" would mean the web container itself. |
| `NYXGPT_TRACING_ENABLED` | server (runtime env) | `true` | Set to `false` to disable the web tier's OTel setup entirely (matches the API's tracing-on-by-default, #3415/#3427). |
| `NYXGPT_LOG_DIR` | server (runtime env) | unset | Directory `web/src/lib/logger.ts` also appends structured log lines to (in addition to stdout) -- set in Compose mode so promtail can tail a file (native mode needs no equivalent; brew already redirects stdout to a file). |
| `NEXT_PUBLIC_API_BASE_URL` | build arg | `http://localhost:8000` | Base URL baked into the *browser* bundle (existing, pre-#3430; distinct from the server-side `NYXGPT_API_BASE_URL` above). |
| `NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT` | build arg | `http://localhost:4318/v1/traces` | Where `instrumentation-client.ts`'s browser `WebTracerProvider` exports spans. The default already matches the collector's host-published port, so no override is usually needed. |
| `NEXT_PUBLIC_NYXGPT_TRACING_ENABLED` | build arg | `true` | Disables the browser-side OTel setup when `false`. |
| `NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN` | build arg | unset (disabled) | GlitchTip DSN for browser error capture (`@sentry/nextjs`, see [docker-compose.md#error-tracking](docker-compose.md#error-tracking)) -- set to match `[error_tracking] dsn`. Sentry DSNs aren't secrets (designed for client-side embedding), unlike `NYXGPT_AUTH_API_KEY`. |
| `NYXGPT_ERROR_TRACKING_DSN` | server (runtime env) | unset (disabled) | Same DSN for the Next.js server-side Sentry init (`instrumentation.ts`'s `onRequestError` hook) -- can differ in principle from the build-time browser one, but should normally match it. |
| `NYXGPT_ERROR_TRACKING_ENVIRONMENT` / `NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_ENVIRONMENT` | server / build arg | `development` | Sentry `environment` tag, mirroring `[error_tracking] environment`. |

Because `NEXT_PUBLIC_*` values are inlined at build time, changing them only
takes effect after the web build is redone -- there's no hot-reload
equivalent to `config.ini`'s for these. `nyxgpt ops install` reconciles this
for the native-mode Homebrew formula. The Compose deployment path doesn't yet
have an equivalent `nyxgpt`-wrapped rebuild command (tracked as a follow-up,
alongside the pre-existing raw-command references in
[docker-compose.md](docker-compose.md)) -- until then, rebuild the web
service through whatever Compose tooling you used to deploy it. There's also
no automatic sync from `config.ini` into these build args yet (a known
follow-up) -- set them by hand to match your `[tracing]`/`[error_tracking]`
config.ini values.

---

## Hot-reloadable settings

- `nyxgpt.default_model`
- `logging.level`
- `rag.enable_chat_context`
- `auth.enabled`, `auth.api_key`, `auth.header`

All other settings require a service restart unless otherwise noted.

---

## Notes

- All paths support `~` expansion.
- Missing configuration values fall back to sensible defaults.
- Changes to RAG embedding settings require re-ingesting documents.
