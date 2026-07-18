# FastAPI Backend

nyxGPT provides a local FastAPI backend that exposes chat, RAG, session, and health endpoints. The API is intended for:

- the CLI (primary consumer today)
- future local TUI and web UI
- debugging and integration testing

The API is designed to run **locally only** by default.

---

## API Endpoint Reference

Quick reference of all 85 available endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/metrics` | GET | Prometheus-format metrics (request counts, latency histograms, error rates, chat/RAG business metrics) |
| `/api/v1/info` | GET | Runtime configuration |
| `/api/v1/batch/metrics` | GET | Request batching metrics |
| `/api/v1/metrics` | GET | Resource usage monitoring (memory, CPU, latency, queue depth) |
| `/api/v1/tracing` | GET | Distributed tracing status (enabled/active, service name, OTLP endpoint, Jaeger UI URL) |
| `/api/v1/error-tracking` | GET | Error tracking status (enabled/active, DSN, environment, GlitchTip UI URL) |
| `/api/v1/monitoring` | GET | Monitoring stack status (enabled/active, Grafana UI URL) |
| `/api/v1/log-aggregation` | GET | Log aggregation stack status (enabled/active, Grafana Loki UI URL) |
| `/api/v1/error-tracking/report` | POST | Forward a web UI client-side error to the local error tracker |
| `/api/v1/config` | GET | Get current configuration |
| `/api/v1/config` | POST | Update configuration (full replace) |
| `/api/v1/config` | PATCH | Partial configuration update |
| `/api/v1/admin/overview` | GET | Admin dashboard overview (system status summary) |
| `/api/v1/admin/health` | GET | Admin dashboard component health checks |
| `/api/v1/admin/activity` | GET | Recent admin/operational activity feed |
| `/api/v1/admin/access` | GET | Get API-key access configuration (masked) |
| `/api/v1/admin/access` | POST | Update API-key access configuration / rotate key |
| `/api/v1/admin/workflow-analytics` | GET | CI/CD workflow run history and analytics |
| `/api/v1/analytics/usage` | GET | Aggregated chat usage analytics (tokens, sessions, by model/day) |
| `/api/v1/analytics/export` | GET | Export recorded usage events (json/csv) |
| `/api/v1/deploy/status` | GET | Blue/green deployment status (active color, health, history) |
| `/api/v1/deploy/switch` | POST | Cut traffic over to a color (health-checked) |
| `/api/v1/deploy/rollback` | POST | Switch traffic back to the previously active color |
| `/api/v1/canary/status` | GET | Canary rollout status (weight, stable/canary health, live metrics, history) |
| `/api/v1/canary/start` | POST | Start a canary rollout at an initial traffic weight |
| `/api/v1/canary/evaluate` | POST | Check live error-rate/latency metrics against thresholds; auto-rollback on regression |
| `/api/v1/canary/promote` | POST | Increase the canary's traffic share by a step |
| `/api/v1/canary/rollback` | POST | Cut all traffic back to nyxgpt-api-stable |
| `/api/v1/self-heal/status` | GET | Self-heal watchdog status (per-component health, recent events) |
| `/api/v1/self-heal/toggle` | POST | Enable/disable automatic self-healing |
| `/api/v1/self-heal/heal` | POST | Manually restart one or every unhealthy component |
| `/api/v1/self-heal/logs` | GET | Recent Compose logs for one component (e.g. GlitchTip's registration link) |
| `/api/v1/models` | GET | List Ollama models |
| `/api/v1/models/pull` | POST | Pull model from Ollama |
| `/api/v1/models/{model_name}` | DELETE | Delete model |
| `/api/v1/models/{model_name}/info` | GET | Get model details |
| `/api/v1/sessions` | GET | List all sessions |
| `/api/v1/sessions/search` | GET | Search messages across sessions |
| `/api/v1/sessions/init` | POST | Initialize session (idempotent) |
| `/api/v1/sessions/{name}` | GET | Get session with messages |
| `/api/v1/sessions/{name}` | DELETE | Delete session |
| `/api/v1/sessions/{name}/summarize` | POST | Generate title/summary/tags |
| `/api/v1/sessions/{name}/pin` | POST | Pin session |
| `/api/v1/sessions/{name}/unpin` | POST | Unpin session |
| `/api/v1/sessions/{name}/title` | POST | Set session title |
| `/api/v1/sessions/{name}/tags/add` | POST | Add tags to session |
| `/api/v1/sessions/{name}/tags/remove` | POST | Remove tags from session |
| `/api/v1/sessions/{name}/rename` | POST | Rename session |
| `/api/v1/sessions/{name}/sync-filename` | POST | Sync filename with title |
| `/api/v1/sessions/{name}/metadata` | GET | Get session metadata |
| `/api/v1/sessions/{name}/rag/enable` | POST | Enable RAG for session |
| `/api/v1/sessions/{name}/rag/disable` | POST | Disable RAG for session |
| `/api/v1/sessions/{name}/documents` | GET | List documents force-included for session RAG context |
| `/api/v1/sessions/{name}/documents` | POST | Attach a document to a session for force-inclusion |
| `/api/v1/sessions/{name}/documents/{doc_id}` | DELETE | Detach a document from a session |
| `/api/v1/sessions/{name}/messages/{index}` | PATCH | Edit message (with fork option) |
| `/api/v1/sessions/{name}/messages/{index}/regenerate` | POST | Regenerate response |
| `/api/v1/sessions/{name}/messages/{index}/rag` | GET | Get RAG chunks for message |
| `/api/v1/sessions/{name}/citations/export` | GET | Export session RAG citations (json/markdown) |
| `/api/v1/sessions/{name}/export` | GET | Export session (markdown/json/html) |
| `/api/v1/chat` | POST | Send chat message |
| `/api/v1/chat/stream` | POST | Stream chat response |
| `/api/v1/tools/ls` | POST | List files |
| `/api/v1/tools/cat` | POST | Read file |
| `/api/v1/tools/grep` | POST | Search files |
| `/api/v1/rag/config` | GET | Get RAG configuration (score thresholds) |
| `/api/v1/rag/collections` | GET | List all RAG collections with statistics |
| `/api/v1/rag/collections` | POST | Create a new RAG collection |
| `/api/v1/rag/collections/{name}` | DELETE | Clear RAG collection (truncate all data) |
| `/api/v1/rag/collections/{name}/settings` | GET | Get collection settings (embedding model, chunk size/overlap) |
| `/api/v1/rag/collections/{name}/settings` | PUT | Update collection settings |
| `/api/v1/rag/collections/{name}/reindex` | POST | Re-index a collection with a different embedding model |
| `/api/v1/rag/ingest` | POST | Ingest text document (with update detection) |
| `/api/v1/rag/index-repo` | POST | Bulk-ingest a code repository into RAG |
| `/api/v1/rag/documents` | GET | List all documents in the RAG vector store |
| `/api/v1/rag/documents/{doc_id}` | GET | Get document version information |
| `/api/v1/rag/cache/stats` | GET | Query result cache statistics |
| `/api/v1/rag/cache/clear` | POST | Clear the query result cache |
| `/api/v1/rag/query` | POST | Query RAG vector store (supports metadata filters) |
| `/api/v1/rag/metrics/query` | POST | Query RAG with evaluation metrics |
| `/api/v1/rag/upload` | POST | Upload and ingest file |
| `/api/v1/logs/files` | GET | List log files |
| `/api/v1/logs/view/{filename}` | GET | View log file contents |
| `/api/v1/logs/stream/{filename}` | GET | Stream log file |

---

## Running the API

### Development (manual)

```bash
nyxgpt api
```

or, if running directly:

```bash
uvicorn nyxgpt.app:app --reload
```

### As a background service (Homebrew)

The recommended way to run the API persistently is via Homebrew services.

See: [`docs/homebrew.md`](homebrew.md)

---

## Base URL

By default, the API listens on:

```
http://127.0.0.1:8000
```

Configuration lives in `~/.nyxGPT/config.ini` under the `[api]` section.

---

## Health & diagnostics

### `GET /health`

Simple health check.

**Response:**

```json
{ "status": "ok" }
```

Used by:
- integration tests
- service monitors

### `GET /metrics`

Prometheus text exposition format metrics for scraping. Unauthenticated
(like `/health`), since Prometheus scrapers typically don't send an API key.

**Metrics exposed:**

| Metric | Type | Labels | Description |
|--------|------|--------|--------------|
| `nyxgpt_http_requests_total` | Counter | `method`, `path`, `status` | Total HTTP requests handled |
| `nyxgpt_http_request_duration_seconds` | Histogram | `method`, `path` | HTTP request latency |
| `nyxgpt_http_errors_total` | Counter | `method`, `path` | Requests that resulted in a 5xx error |
| `nyxgpt_chat_requests_total` | Counter | `model`, `streaming` | Chat requests processed |
| `nyxgpt_rag_queries_total` | Counter | `source` | RAG retrieval queries executed (`chat` or `rag_query`) |
| `nyxgpt_selfheal_unhealthy_components` | Gauge | — | Self-heal-monitored components currently unhealthy or stopped |
| `nyxgpt_selfheal_restarts_total` | Counter | `service`, `result` | Self-heal restart attempts, by service and outcome (`ok`/`failed`) |
| `nyxgpt_selfheal_restart_count` | Gauge | `service` | Current consecutive-restart count per service (resets to 0 once healthy) |
| `nyxgpt_selfheal_last_recovery_timestamp` | Gauge | `service` | Unix timestamp of the last successful self-heal restart, by service |
| `nyxgpt_deploy_active_color` | Gauge | `color` | Whether a blue/green color is currently receiving traffic (1) or not (0) |
| `nyxgpt_deploy_switches_total` | Counter | `from_color`, `to_color`, `result` | Blue/green switch attempts, by direction and outcome (`ok`/`failed`) |
| `nyxgpt_deploy_rollbacks_total` | Counter | `result` | Blue/green rollback attempts, by outcome (`ok`/`failed`) |
| `nyxgpt_canary_rollout_active` | Gauge | — | Whether a canary rollout is currently in progress (1) or idle (0) |
| `nyxgpt_canary_weight_percent` | Gauge | — | Current canary traffic weight percentage (0-100) |
| `nyxgpt_canary_evaluations_total` | Counter | `result` | Canary metric evaluations, by result (`pass`/`insufficient_data`/`regression`) |
| `nyxgpt_canary_events_total` | Counter | `action`, `result` | Canary lifecycle events (`start`/`promote`/`rollback`), by outcome |
| `nyxgpt_rag_ingests_total` | Counter | `source`, `result` | RAG document ingestion attempts, by source (`document`/`upload`/`repo`) and outcome (`success`/`failure`) |
| `nyxgpt_cache_requests_total` | Counter | `cache`, `result` | Cache lookups, by cache (`chat_response`/`embedding`/`rag_query_result`) and outcome (`hit`/`miss`) |
| `nyxgpt_rate_limit_rejections_total` | Counter | `path` | Requests rejected by the per-client rate limiter |
| `nyxgpt_resource_memory_rss_mb` | Gauge | — | API process resident set size, in MB (refreshed on each `/metrics` scrape) |
| `nyxgpt_resource_cpu_percent` | Gauge | — | API process CPU usage percentage (refreshed on each `/metrics` scrape) |
| `nyxgpt_resource_queue_depth` | Gauge | — | Current number of requests in the batch processing queue |

`path` labels use the route's path template (e.g. `/api/v1/sessions/{name}`),
not the raw request path, to keep cardinality bounded.

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: nyxgpt
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

The admin dashboard's Resource Usage panel (`/admin`) surfaces this
endpoint under a "Prometheus Endpoint" card, with a link to view current
metrics. Pre-built Grafana dashboards over these metrics (system overview,
RAG performance, API metrics, resource usage) are available via the
opt-in, local-only `monitoring` Compose profile — see
[Monitoring Dashboards](docker-compose.md#monitoring-dashboards). The
`/admin/observability` page ("SRE Overview") is the single entry point
into all of them, plus curated Loki log queries, Jaeger trace views, and
GlitchTip error tracking.

---

## Versioned API

All functional endpoints live under:

```
/api/v1
```

---

## Security Headers

All API responses include comprehensive security headers to protect against common web vulnerabilities. These headers are automatically added by middleware and apply to all endpoints.

### Headers Added

- **Content-Security-Policy**: Restricts resource loading to prevent XSS attacks
- **X-Content-Type-Options**: Prevents MIME sniffing attacks
- **X-Frame-Options**: Prevents clickjacking attacks
- **Strict-Transport-Security**: Enforces HTTPS connections (HTTPS only)

### Content Security Policy

The CSP header restricts what resources the browser can load:

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'
```

Key directives:
- `default-src 'self'` - Only load resources from same origin by default
- `script-src 'self' 'unsafe-inline'` - Allow same-origin scripts and inline scripts
- `style-src 'self' 'unsafe-inline'` - Allow same-origin styles and inline styles
- `img-src 'self' data:` - Allow same-origin images and data URIs
- `connect-src 'self'` - Only connect to same origin (API calls, WebSockets)
- `frame-ancestors 'none'` - Prevent embedding in iframes
- `form-action 'self'` - Forms can only submit to same origin
- `base-uri 'self'` - Restrict base element URLs

### X-Content-Type-Options

```
X-Content-Type-Options: nosniff
```

Prevents browsers from MIME-sniffing responses, reducing the risk of drive-by download attacks.

### X-Frame-Options

```
X-Frame-Options: DENY
```

Prevents the API responses from being embedded in iframes, protecting against clickjacking attacks.

### Strict-Transport-Security (HSTS)

```
Strict-Transport-Security: max-age=31536000; includeSubDomains
```

**Note**: This header is only added when the request uses HTTPS. For local HTTP development, this header is not present.

When present, it instructs browsers to:
- Only connect via HTTPS for the next year (`max-age=31536000`)
- Apply this policy to all subdomains (`includeSubDomains`)

### Example Response

```bash
curl -I http://127.0.0.1:8000/api/v1/info
```

```
HTTP/1.1 200 OK
content-security-policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'
x-content-type-options: nosniff
x-frame-options: DENY
x-request-id: 550e8400-e29b-41d4-a716-446655440000
content-type: application/json
```

### Benefits

- **XSS Protection**: CSP prevents execution of malicious scripts
- **Clickjacking Protection**: X-Frame-Options prevents UI redressing attacks
- **MIME Sniffing Protection**: X-Content-Type-Options prevents MIME confusion attacks
- **HTTPS Enforcement**: HSTS ensures secure connections (when using HTTPS)

### Compatibility

Security headers work alongside:
- CORS headers (for cross-origin requests)
- Request ID tracking
- Authentication headers
- Rate limiting

All security headers are present on:
- Successful responses (200, 201, etc.)
- Error responses (400, 404, 500, etc.)
- Streaming responses

---

## Request ID Tracking

All API requests are automatically assigned a unique request ID for traceability across logs and responses. This enables correlation of log entries with specific API requests for debugging and monitoring.

### Features

- **Automatic generation**: Each request receives a UUID v4 request ID if not provided
- **Client-provided IDs**: Clients can provide their own request ID via the `X-Request-Id` header
- **Response header**: The request ID is always returned in the `X-Request-Id` response header
- **Logging integration**: All log entries include the request ID for full request tracing
- **Error responses**: Error responses include the request ID in the response body

### Usage

**Auto-generated request ID:**

```bash
curl http://127.0.0.1:8000/api/v1/info
# Response includes: X-Request-Id: 550e8400-e29b-41d4-a716-446655440000
```

**Client-provided request ID:**

```bash
curl http://127.0.0.1:8000/api/v1/info \
  -H "X-Request-Id: my-custom-request-id"
# Response includes: X-Request-Id: my-custom-request-id
```

**Error response with request ID:**

```json
{
  "error": {
    "code": "not_found",
    "message": "Resource not found",
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

**Log entries with request ID:**

```
2026-01-03 12:34:56 INFO [550e8400-e29b-41d4-a716-446655440000] nyxgpt.api: Chat request received
2026-01-03 12:34:57 INFO [550e8400-e29b-41d4-a716-446655440000] nyxgpt.api: Chat request completed
```

### Benefits

- **Debugging**: Trace all log entries related to a specific request
- **Monitoring**: Track request flow through the system
- **Client correlation**: Clients can provide their own IDs to correlate with their logs
- **Error investigation**: Quickly find all logs related to failed requests

---

## Info endpoint

### `GET /api/v1/info`

Returns basic runtime configuration details.

**Response:**

```json
{
  "ollama_base_url": "http://127.0.0.1:11434",
  "default_model": "llama3.1:8b",
  "sessions_dir": "/Users/you/.nyxGPT/sessions"
}
```

---

## Models endpoints

Manage Ollama models via the API. These endpoints allow listing, pulling, deleting, and inspecting models.

### `GET /api/v1/models`

List all available Ollama models.

**Response:**

```json
{
  "models": ["llama3.1:8b", "mistral:7b", "codellama:13b"]
}
```

### `POST /api/v1/models/pull`

Pull (download) a model from the Ollama library.

**Request (non-streaming):**

```json
{
  "model": "llama3.1:8b"
}
```

**Response (non-streaming):**

```json
{
  "ok": true,
  "model": "llama3.1:8b",
  "result": { "status": "success" }
}
```

**Request (streaming progress via SSE):**

```json
{
  "model": "llama3.1:8b",
  "stream": true
}
```

When `stream` is `true`, the response is a `text/event-stream` (SSE) with one JSON
object per event line:

```
data: {"status": "pulling manifest", "completed": 0, "total": 0, "percent": 0.0}

data: {"status": "downloading", "completed": 524288000, "total": 4700000000, "percent": 11.2}

data: {"status": "success", "ok": true, "model": "llama3.1:8b"}
```

**Notes:**
- Downloads can take several minutes for large models
- Use `stream: true` for real-time progress updates
- Timeout: 600 seconds

### `DELETE /api/v1/models/{model_name}`

Delete a model from Ollama.

**Example:**

```bash
curl -X DELETE http://127.0.0.1:8000/api/v1/models/mistral:7b
```

**Response:**

```json
{
  "ok": true,
  "model": "mistral:7b"
}
```

**Error Responses:**
- `400` - Invalid model name (empty or whitespace)
- `502` - Ollama API error

### `GET /api/v1/models/{model_name}/info`

Get detailed information about a specific model.

**Example:**

```bash
curl http://127.0.0.1:8000/api/v1/models/llama3.1:8b/info
```

**Response:**

```json
{
  "ok": true,
  "model": "llama3.1:8b",
  "info": {
    "modelfile": "FROM llama3.1:8b\n...",
    "parameters": "temperature 0.7\n...",
    "template": "{{ .System }} {{ .Prompt }}",
    "size": 4700000000,
    "modified_at": "2025-01-07T12:34:56Z"
  }
}
```

---

## Config Management

Configuration can be read and updated via the API. Changes to hot-reloadable settings (default_model, rag_enabled, log_level) take effect immediately without restart.

### `GET /api/v1/config`

Get current configuration values.

**Response:**

```json
{
  "ollama_base_url": "http://127.0.0.1:11434",
  "default_model": "qwen2.5:0.5b",
  "rag_enabled": false,
  "log_level": "INFO"
}
```

### `POST /api/v1/config`

Update configuration (full replace). Only provided keys are updated.

**Request:**

```json
{
  "default_model": "llama3.1:8b",
  "rag_enabled": true,
  "log_level": "DEBUG"
}
```

**Response:**

```json
{
  "updated": {
    "default_model": "llama3.1:8b",
    "rag_enabled": true,
    "log_level": "DEBUG"
  },
  "effective": {
    "ollama_base_url": "http://127.0.0.1:11434",
    "default_model": "llama3.1:8b",
    "rag_enabled": true,
    "log_level": "DEBUG"
  }
}
```

### `PATCH /api/v1/config`

Partial configuration update (same as POST, provided for semantic clarity).

---

## Admin Dashboard

These endpoints back the unified admin dashboard at `/admin/dashboard`: a
system status overview, an activity log (audit trail of admin actions),
and access/API-key management. nyxGPT is a single-operator local system
(see [`docs/configuration.md`](configuration.md#auth-section)), so "access
management" here means the shared API key rather than per-user accounts.

### `GET /api/v1/admin/overview`

Aggregate system status: app info, resource metrics, deploy/canary/self-heal
status, opt-in observability stack flags, and whether API-key auth is
enabled. Individual sub-sections (e.g. `deploy`, `canary`, `self_heal`)
degrade to `{"error": "..."}` instead of failing the whole request when a
backing service (like a local K8s cluster or the Docker daemon) is
unavailable.

**Response:**

```json
{
  "info": {
    "ollama_base_url": "http://127.0.0.1:11434",
    "default_model": "llama3.1:8b",
    "rag_enabled": false
  },
  "resource_metrics": { "memory": {}, "cpu": {}, "latency": {}, "queue": {} },
  "deploy": { "namespace": "nyxgpt", "active": "blue", "inactive": "green" },
  "canary": { "namespace": "nyxgpt", "active": false },
  "self_heal": { "enabled": false, "components": [], "unhealthy_count": 0, "events": [] },
  "observability": {
    "monitoring": false,
    "tracing": false,
    "error_tracking": false,
    "log_aggregation": false
  },
  "auth_enabled": false
}
```

### `GET /api/v1/admin/health`

Aggregate system health for the admin health dashboard (`/admin/health`):
service uptime, dependency reachability checks (Ollama, and Cassandra when
RAG is enabled), resource utilization, and threshold-based alert
indicators (warning/critical on elevated memory, CPU, or error rate, and
critical on any unreachable dependency).

**Response:**

```json
{
  "service": { "status": "ok", "uptime_s": 3725.4 },
  "dependencies": [
    { "name": "ollama", "ok": true, "detail": "Reachable at http://127.0.0.1:11434", "applicable": true },
    { "name": "cassandra", "ok": true, "detail": "RAG disabled; Cassandra is not required", "applicable": false }
  ],
  "resource_metrics": { "memory": {}, "cpu": {}, "latency": {}, "queue": {}, "errors": {} },
  "alerts": []
}
```

### `GET /api/v1/admin/activity`

Return recent admin dashboard activity (config changes, deploy/canary
actions, model pulls/deletes, access changes). Accepts an optional
`limit` query parameter (default 50, max 500).

**Response:**

```json
{
  "events": [
    { "ts": 1768300800.0, "action": "config.updated", "detail": "log_level=DEBUG" },
    { "ts": 1768300900.0, "action": "deploy.switch", "detail": "Switched traffic from blue to green" }
  ]
}
```

### `GET /api/v1/admin/access`

Return the current API-key access configuration. The key itself is never
returned in full — only a masked value (e.g. `sk-a****-b12c`).

**Response:**

```json
{
  "enabled": false,
  "header": "X-API-Key",
  "api_key_set": true,
  "api_key_masked": "sk-a****-b12c"
}
```

### `POST /api/v1/admin/access`

Update access configuration: enable/disable API-key auth, change the
header name, or rotate the key. All fields are optional; at least one
must be provided.

**Request:**

```json
{ "enabled": true, "header": "X-API-Key", "rotate": true }
```

On rotation, the freshly generated key is returned once in the response
body as `api_key` so the operator can copy it — it is never returned
again by `GET /admin/access`, which only shows the masked value.

**Response:**

```json
{
  "enabled": true,
  "header": "X-API-Key",
  "api_key_set": true,
  "api_key_masked": "8f3a****91c2",
  "api_key": "8f3a1b2c...e5f691c2"
}
```

### `GET /api/v1/admin/workflow-analytics`

CI/CD workflow run history and analytics for the admin dashboard. Wraps
the SQLite store built by `scripts/collect_workflow_logs.py collect`
(success rate, average duration, per-workflow/day breakdowns, top failing
workflows, and recent runs) so it's visible from the dashboard, not just
the CLI.

**Query Parameters:**
- `days` (int, default: `30`) - How many days of history to summarize
- `limit` (int, default: `50`) - Maximum number of recent runs to return

**Response:** summary object produced by `workflow_analytics.summary()` —
success rate, average duration, per-workflow/day breakdowns, top failing
workflows, and a `recent_runs` list.

---

## Usage Analytics

These endpoints back the usage analytics dashboard at `/admin/analytics`.
Every completed `/api/v1/chat` and `/api/v1/chat/stream` request records a
usage event (session, model, prompt/completion token estimates, request
duration) that feeds these endpoints.

### `GET /api/v1/analytics/usage`

Return aggregated usage analytics: total requests, total prompt/completion
tokens, distinct session count, and per-model and per-day breakdowns.

**Response:**

```json
{
  "total_requests": 42,
  "total_prompt_tokens": 5210,
  "total_completion_tokens": 8390,
  "total_tokens": 13600,
  "session_count": 6,
  "by_model": [
    { "model": "llama3.1:8b", "requests": 42, "prompt_tokens": 5210, "completion_tokens": 8390 }
  ],
  "by_day": [
    { "date": "2026-07-14", "requests": 42, "prompt_tokens": 5210, "completion_tokens": 8390 }
  ]
}
```

### `GET /api/v1/analytics/export`

Export recorded usage events as a downloadable report. Accepts an
optional `format` query parameter: `json` (default) or `csv`. Returns a
`400` for any other format.

- `format=json`: `{"summary": {...}, "events": [...]}` (same shape as
  `GET /admin/analytics/usage` for `summary`, plus the raw event list).
- `format=csv`: header row `ts,session,model,prompt_tokens,completion_tokens,duration_s`
  followed by one row per recorded request.

Both formats are returned with a `Content-Disposition: attachment`
header so browsers download the file directly.

---

## Deployment (Blue/Green)

Local blue/green deployment for `nyxgpt-api` on a local Kubernetes cluster
(kind/minikube/k3s) — see [kubernetes.md](kubernetes.md#bluegreen-deployment)
for the full workflow and the `nyxgpt deploy` CLI. These endpoints back the
SRE/admin dashboard at `/admin/deploy`.

Every switch/rollback decision (attempt, refusal, outcome) is logged with
structured fields and exported as the `nyxgpt_deploy_*` metrics above -- see
[kubernetes.md#deploy-logging--metrics](kubernetes.md#deploy-logging--metrics)
for the pre-provisioned Grafana **Blue-Green Deployment** dashboard and the
Loki saved query, both linked directly from `/admin/deploy`.

### `GET /api/v1/deploy/status`

Return which color is active, each color's health, and recent switch history.

**Response:**

```json
{
  "namespace": "nyxgpt",
  "active": "blue",
  "inactive": "green",
  "colors": {
    "blue": { "healthy": true, "message": "nyxgpt-api-blue healthy (1/1 ready)" },
    "green": { "healthy": true, "message": "nyxgpt-api-green healthy (1/1 ready)" }
  },
  "history": [{ "from": "green", "to": "blue", "ts": 1730000000.0 }],
  "available": true,
  "unavailable_reason": null
}
```

`available` is `false` when blue/green deployment can't operate in the
current deployment mode (e.g. under docker-compose, which has no cluster for
`kubectl` to reach — see
[docker-compose.md](docker-compose.md#bluegreen-and-canary-deployment)); in
that case `unavailable_reason` explains why and every color's `message`
carries the same explanation instead of a raw `kubectl not found` error.

### `POST /api/v1/deploy/switch`

Cut traffic over to a color. Refuses (`409`) unless the target Deployment is
healthy, unless `force` is set.

**Request:**

```json
{ "to": "green", "force": false }
```

`to` is optional; if omitted, the target defaults to whichever color is
currently inactive. `force` is optional (default `false`).

**Response:**

```json
{ "ok": true, "message": "Switched traffic from blue to green" }
```

Returns `400` if `to` is not `"blue"` or `"green"`, and `409` if the switch
was refused (e.g. the target is unhealthy).

### `POST /api/v1/deploy/rollback`

Switch traffic back to the color that was active before the last switch.
Unlike `switch`, this bypasses the health gate — it's the emergency escape
hatch. Returns `409` if there is no switch history to roll back to.

**Response:**

```json
{ "ok": true, "message": "Switched traffic from green to blue" }
```

---

## Canary Deployment

Local canary deployment for `nyxgpt-api` on a local Kubernetes cluster
(kind/minikube/k3s) — see [kubernetes.md](kubernetes.md#canary-deployment)
for the full workflow and the `nyxgpt canary` CLI. These endpoints back the
SRE/admin dashboard at `/admin/canary`. Traffic is split by the
`nyxgpt-api-stable`/`nyxgpt-api-canary` Deployments' replica-count ratio
behind a shared Service (kube-proxy is the traffic layer — no in-cluster
proxy or cloud LB involved).

Every start/evaluate/promote/rollback decision is logged with structured
fields and exported as the `nyxgpt_canary_*` metrics above -- see
[kubernetes.md#canary-logging--metrics](kubernetes.md#canary-logging--metrics)
for the pre-provisioned Grafana **Canary Rollout** dashboard and the Loki
saved query, both linked directly from `/admin/canary`.

### `GET /api/v1/canary/status`

Return rollout progress, stable/canary health, live error-rate/latency
metrics, and recent action history.

**Response:**

```json
{
  "namespace": "nyxgpt",
  "active": true,
  "weight_percent": 25,
  "stable": { "healthy": true, "message": "nyxgpt-api-stable healthy (3/3 ready)" },
  "canary": { "healthy": true, "message": "nyxgpt-api-canary healthy (1/1 ready)" },
  "metrics": { "total_requests": 120, "error_rate_percent": 0.83, "p95_latency_ms": 340.5 },
  "history": [{ "action": "start", "weight_percent": 10, "ts": 1730000000.0 }],
  "available": true,
  "unavailable_reason": null
}
```

`available`/`unavailable_reason` behave the same as on `GET
/api/v1/deploy/status` above.

### `POST /api/v1/canary/start`

Start a canary rollout at an initial traffic weight. Refuses (`409`) if a
rollout is already in progress.

**Request:**

```json
{ "weight_percent": 10 }
```

`weight_percent` is optional (default `10`), clamped to `1`-`99`.

**Response:**

```json
{ "ok": true, "message": "Started canary rollout at 10% (1/4 replicas)" }
```

### `POST /api/v1/canary/evaluate`

Compare live error-rate/p95-latency metrics (from `/api/v1/metrics`) against
the configured `[canary]` thresholds. Automatically rolls back if either is
breached, or reports "insufficient data" (still `200`) if too few requests
have been observed. Returns `409` if there is no rollout in progress or a
regression triggered an automatic rollback.

**Response:**

```json
{ "ok": true, "message": "Metrics within thresholds (error_rate=0.83%, p95=340.50ms); safe to promote" }
```

### `POST /api/v1/canary/promote`

Increase the canary's traffic share by a step, finalizing at 100%. Returns
`409` if there is no rollout in progress.

**Request:**

```json
{ "step_percent": 25 }
```

`step_percent` is optional (default: `[canary] step_percent`, `25`).

**Response:**

```json
{ "ok": true, "message": "Promoted canary to 35% (1/4 replicas)" }
```

### `POST /api/v1/canary/rollback`

Cut all traffic back to `nyxgpt-api-stable`. Scales the canary Deployment to
0 first (removing it from the Service's endpoints) before restoring stable's
replica count — the emergency escape hatch, not blocked by a flaky
stable-scale-up. Returns `409` if there is no rollout in progress.

**Response:**

```json
{ "ok": true, "message": "Rolled back canary rollout from 25% to 0%" }
```

---

## Self-Heal Watchdog

Watches every component of the local Docker Compose stack (`docker
compose ps` across the core services plus any of the
`monitoring`/`logging`/`tracing`/`errors` profiles that are up) and
automatically restarts anything unhealthy or stopped. See
[self-healing.md](self-healing.md) for the full design, the
`nyxgpt self-heal` CLI, and the known limitation around the `api`
container. These endpoints back the SRE/admin dashboard at
`/admin/self-heal`.

Every decision the watchdog makes (health checks, restart attempts and
outcomes, backoff skips, restart-count resets, giving up after too many
consecutive failures) is logged with structured fields and exported as the
`nyxgpt_selfheal_*` metrics above -- see
[self-healing.md#observability](self-healing.md#observability-logs-metrics-and-the-self-healing-dashboard)
for the pre-provisioned Grafana **Self-Healing** dashboard and the Loki
saved query, both linked directly from `/admin/self-heal`.

### `GET /api/v1/self-heal/status`

Return whether the watchdog is enabled, live per-component health, and
recent heal events.

**Response:**

```json
{
  "enabled": true,
  "components": [
    { "service": "api", "container": "nyxgpt-api-1", "state": "running", "health": "healthy", "healthy": true },
    { "service": "web", "container": "nyxgpt-web-1", "state": "running", "health": "healthy", "healthy": true }
  ],
  "unhealthy_count": 0,
  "events": [
    { "ts": 1730000000.0, "service": "web", "reason": "state=exited health=n/a", "action": "restart", "ok": true, "restart_count": 1, "message": "Restarted web" }
  ]
}
```

### `POST /api/v1/self-heal/toggle`

Enable or disable the automatic heal loop.

**Request:**

```json
{ "enabled": true }
```

**Response:**

```json
{ "enabled": true }
```

### `POST /api/v1/self-heal/heal`

Manually trigger a heal pass. With no body (or `{}`), checks every
monitored component and restarts anything unhealthy/stopped, same as the
automatic loop but on demand. With `{"service": "<name>"}`, restarts that
one component immediately regardless of its current health — the
dashboard's per-component "Heal now" button. Returns `404` if `service`
isn't a currently-known container.

**Request:**

```json
{ "service": "web" }
```

**Response:**

```json
{
  "checked": [{ "service": "web", "state": "running", "health": "healthy", "healthy": true }],
  "healed": [{ "ts": 1730000000.0, "service": "web", "reason": "manual heal-now", "action": "restart", "ok": true, "restart_count": 2, "message": "Restarted web" }]
}
```

### `GET /api/v1/self-heal/logs`

Recent Docker Compose logs for one component. Backs the "View GlitchTip logs"
button in the Error Tracking panel (`/admin/observability`) as well as
`nyxgpt ops logs` -- both let an operator read a container's console output
(e.g. GlitchTip's first-account confirmation link, printed there by its
console email backend) without a raw `docker`/`docker compose` command.

**Query parameters:** `service` (required, Compose service name, e.g.
`glitchtip`, `api`), `tail` (optional, number of trailing lines, default 200).

**Response:**

```json
{ "service": "glitchtip", "tail": 100, "logs": "glitchtip-1  | confirm your account: http://localhost:8080/accounts/confirm/..." }
```

Returns `502` if the service is unknown or the logs can't be fetched.

---

## Sessions endpoints

Sessions store conversation history and metadata and are persisted automatically on disk.

### `GET /api/v1/sessions`

List all known sessions with comprehensive metadata.

**Response:**

```json
{
  "sessions": [
    {
      "name": "default",
      "messages": 14,
      "modified": "2025-12-23 23:35:32",
      "pinned": false,
      "tags": ["python", "debugging"],
      "title": "Debugging Python Script",
      "summary": "Troubleshooting import errors in main.py",
      "token_estimate": 2284,
      "model": "llama3.1:8b"
    }
  ]
}
```

**Metadata fields:**
- `pinned` (bool) - Session is pinned to top of list
- `tags` (list) - User-defined tags for organization
- `title` (string) - Human-readable session title
- `summary` (string) - Auto-generated or manual summary
- `token_estimate` (int) - Estimated total tokens in session
- `model` (string) - Model used for this session

---

### `GET /api/v1/sessions/search`

Search for messages across all sessions or within a specific session.

**Query Parameters:**
- `query` (required) - Text to search for in messages
- `case_sensitive` (optional, default: false) - Case-sensitive search
- `role_filter` (optional) - Filter by message role: `user`, `assistant`, `system`
- `session_filter` (optional) - Filter to specific session name
- `limit` (optional, default: 50, max: 500) - Maximum results to return

**Example:**

```bash
# Search all sessions
curl "http://127.0.0.1:8000/api/v1/sessions/search?query=python&limit=10"

# Search only user messages
curl "http://127.0.0.1:8000/api/v1/sessions/search?query=error&role_filter=user"

# Search within specific session
curl "http://127.0.0.1:8000/api/v1/sessions/search?query=function&session_filter=default"
```

**Response:**

```json
{
  "query": "python",
  "total_results": 3,
  "results": [
    {
      "session_name": "default",
      "session_title": "Python Tutorial",
      "message_index": 5,
      "role": "user",
      "content": "How do I use Python decorators?",
      "content_preview": "How do I use Python decorators?",
      "timestamp": "2025-01-15T10:30:00",
      "matches": 1
    },
    {
      "session_name": "research",
      "session_title": null,
      "message_index": 2,
      "role": "assistant",
      "content": "Python decorators are a powerful feature...",
      "content_preview": "Python decorators are a powerful...",
      "timestamp": "2025-01-14T14:20:00",
      "matches": 2
    }
  ]
}
```

---

### `POST /api/v1/sessions/init`

Initialize a session. This endpoint is **idempotent** and does not invoke the model.

**Request:**

```json
{
  "name": "my-session"
}
```

**Response:**

```json
{ "status": "ok", "session": "my-session" }
```

---

### `GET /api/v1/sessions/{name}`

Retrieve a single session, including messages and metadata.

**Response (abridged):**

```json
{
  "name": "my-session",
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi!"}
  ],
  "meta": {
    "pinned": false,
    "tags": [],
    "title": "",
    "summary": ""
  }
}
```

---

## Session Metadata Management

Endpoints for managing session metadata including titles, tags, pinning, and auto-summarization.

### `POST /api/v1/sessions/{name}/summarize`

Generate title, summary, and tags for a session using the LLM. This analyzes the conversation and automatically extracts meaningful metadata.

**Response:**

```json
{
  "ok": true
}
```

After success, the session metadata will contain auto-generated title, summary, and tags.

### `POST /api/v1/sessions/{name}/pin`

Pin a session to keep it at the top of the session list.

**Response:**

```json
{
  "ok": true
}
```

### `POST /api/v1/sessions/{name}/unpin`

Unpin a session.

**Response:**

```json
{
  "ok": true
}
```

### `POST /api/v1/sessions/{name}/title`

Set the session title manually.

**Request:**

```json
{
  "title": "Python Debugging Session"
}
```

**Response:**

```json
{
  "ok": true
}
```

### `POST /api/v1/sessions/{name}/tags/add`

Add one or more tags to a session.

**Request:**

```json
{
  "tags": ["python", "debugging", "tutorial"]
}
```

**Response:**

```json
{
  "ok": true
}
```

### `POST /api/v1/sessions/{name}/tags/remove`

Remove one or more tags from a session.

**Request:**

```json
{
  "tags": ["tutorial"]
}
```

**Response:**

```json
{
  "ok": true
}
```

### `POST /api/v1/sessions/{name}/rename`

Rename a session with optional title update and filename sync.

**Request (direct rename):**

```json
{
  "new_name": "my-new-session-name",
  "sync_filename": false
}
```

**Request (title-based rename with auto-sync):**

```json
{
  "new_name": "Python Debugging Session",
  "sync_filename": true
}
```

**Response:**

```json
{
  "ok": true,
  "old_name": "default",
  "new_name": "python-debugging-session",
  "message": "Session renamed and filename synced"
}
```

When `sync_filename` is true:
1. The title is set to `new_name`
2. The filename is automatically sanitized and synced
3. Only alphanumeric characters, hyphens, and underscores are kept

### `POST /api/v1/sessions/{name}/sync-filename`

Force filename sync for a session based on its current title. Useful for cleaning up session filenames to match their titles.

**Response:**

```json
{
  "ok": true,
  "old_name": "default",
  "new_name": "python-debugging-session",
  "message": "Filename synced with title"
}
```

**Possible statuses:**
- `renamed` - Filename was changed to match title
- `no_change` - Filename already matches title
- `no_title` - No title set, filename unchanged

### `GET /api/v1/sessions/{name}/metadata`

Get metadata for a specific session.

**Response:**

```json
{
  "pinned": false,
  "tags": ["python", "debugging"],
  "title": "Python Debugging Session",
  "summary": "Troubleshooting import errors and syntax issues",
  "token_estimate": 2284,
  "model": "llama3.1:8b",
  "rag_enabled": false,
  "created_at": "2025-01-15T10:00:00",
  "modified_at": "2025-01-15T14:30:00"
}
```

### `POST /api/v1/sessions/{name}/rag/enable`

Enable RAG (Retrieval-Augmented Generation) for a specific session.

**Response:**

```json
{
  "session": "default",
  "rag_enabled": true
}
```

### `POST /api/v1/sessions/{name}/rag/disable`

Disable RAG for a specific session.

**Response:**

```json
{
  "session": "default",
  "rag_enabled": false
}
```

---

## Message Editing

Endpoints for editing messages, forking conversations, and regenerating responses.

### `PATCH /api/v1/sessions/{name}/messages/{message_index}`

Edit a message in a session. By default, this forks the conversation (truncates messages after the edited one).

**Request:**

```json
{
  "content": "Updated message content",
  "fork": true
}
```

**Parameters:**
- `content` (required) - New message content
- `fork` (optional, default: true) - Whether to truncate messages after this one

**Response:**

```json
{
  "ok": true,
  "message": "Message edited and conversation forked"
}
```

**Use cases:**
- Fix typos in user messages
- Modify prompt and regenerate from that point
- Create conversation branches by editing + regenerating

### `POST /api/v1/sessions/{name}/messages/{message_index}/regenerate`

Regenerate the assistant response from a specific user message.

**Request:**

```json
{
  "prompt": "Optional new prompt to replace the message",
  "model": "llama3.1:8b",
  "rag_enabled": false
}
```

**Parameters:**
- `prompt` (optional) - New prompt to replace the message content
- `model` (optional) - Model override for regeneration
- `rag_enabled` (optional) - RAG override for regeneration

**Response:**

```json
{
  "ok": true,
  "session": "default",
  "model": "llama3.1:8b",
  "reply": "New assistant response...",
  "rag_used": false
}
```

**Workflow:**
1. Optionally replaces the user message with new prompt
2. Truncates conversation after that message
3. Generates a new response using the chat endpoint
4. Returns the new response

### `GET /api/v1/sessions/{name}/messages/{message_index}/rag`

Get RAG chunks associated with a specific message. Enables lazy loading of RAG citation data.

**Response:**

```json
{
  "message_index": 5,
  "has_rag": true,
  "chunks": [
    {
      "text": "Relevant document content here...",
      "score": 0.95,
      "doc_id": "doc123",
      "chunk_id": 5
    },
    {
      "text": "Another relevant chunk...",
      "score": 0.87,
      "doc_id": "doc123",
      "chunk_id": 6
    }
  ]
}
```

### `GET /api/v1/sessions/{name}/citations/export`

Export all RAG citations from a session's assistant messages, for
generating bibliographies or reference lists.

**Query Parameters:**
- `format` (str, default: `json`) - `json` or `markdown`
- `sessions_dir` (str, optional) - Override the sessions directory

**Response (`format=json`):**

```json
{
  "session": "default",
  "total_citations": 2,
  "citations": [
    {
      "message_index": 5,
      "citation_index": 0,
      "doc_id": "doc123",
      "chunk_id": 6,
      "text": "Relevant document content here...",
      "score": 0.95,
      "similarity_score": 0.95
    }
  ]
}
```

**Response (`format=markdown`):** returned as `text/markdown` with a
`Content-Disposition: attachment; filename="{name}-citations.md"` header.
Citations with a missing (`null`) or malformed (non-string) `doc_id`/`text`
field are rendered with that field coerced to a safe string instead of
failing the export.

**Error Responses:**
- `400 Bad Request` - Invalid session name, or `format` is not `json`/`markdown`
- `404 Not Found` - Session does not exist

---

## Chat endpoint

### `POST /api/v1/chat`

Send a chat prompt and receive a model response.

**Request:**

```json
{
  "prompt": "Hello",
  "session": "default",
  "model": "llama3.1:8b"
}
```

**Structured output** – pass an `output_format` JSON schema to constrain the model's
reply to valid JSON:

```json
{
  "prompt": "Extract the name and age from: 'Alice is 30 years old'",
  "output_format": {
    "type": "object",
    "properties": {
      "name": { "type": "string" },
      "age": { "type": "integer" }
    },
    "required": ["name", "age"]
  }
}
```

The model will produce JSON conforming to the schema (Ollama `format` parameter).

**Response:**

```json
{
  "reply": "Hello! How can I help you today?",
  "session": "default",
  "model": "llama3.1:8b",
  "rag_used": false,
  "rag_chunks": []
}
```

**Response with RAG enabled:**

```json
{
  "reply": "Based on the context, here's what I found...",
  "session": "default",
  "model": "llama3.1:8b",
  "rag_used": true,
  "rag_chunks": [
    {
      "text": "Relevant document content here...",
      "score": 0.95,
      "doc_id": "doc123",
      "chunk_id": 5
    },
    {
      "text": "Another relevant chunk...",
      "score": 0.87,
      "doc_id": "doc123",
      "chunk_id": 6
    }
  ]
}
```

- Sessions are persisted automatically.
- RAG context may be injected depending on configuration.
- When RAG is enabled, the response includes `rag_chunks` array with retrieved context metadata.

**Model runtime failures:** if the Ollama/llama-server model runtime itself
fails to load or run the selected model (crash, out-of-memory, timeout), the
endpoint returns `502 Bad Gateway` with an actionable `detail` message
instead of a bare `500`, e.g.:

```json
{"detail": "Model failed to run — the model runtime returned an error. This can happen if the host doesn't have enough free memory to load the model, but may also be a transient failure (Ollama HTTP 500: ...)"}
```

When request batching (`[batch] enabled`) is on, a batch-queue timeout
(distinct from a model failure) returns `504 Gateway Timeout` instead. See
[troubleshooting.md#502-bad-gateway-chat](troubleshooting.md#502-bad-gateway-chat)
and [performance.md#approximate-memory-by-model-tag](performance.md#approximate-memory-by-model-tag).

### RAG prompt & context optimization

When RAG-assisted chat is enabled, retrieved context injected into the prompt is governed by configuration settings in `~/.nyxGPT/config.ini` under the `[rag]` section.

Key controls include:

- `enable_chat_context`: turn RAG injection on or off for chat
- `chat_top_k`: number of candidate chunks retrieved from the vector store
- `min_score`: minimum similarity score required for a chunk to be included
- `max_chunks`: hard cap on the number of chunks injected
- `chat_context_max_chars`: maximum total character budget for injected context
- `dedupe`: remove duplicate or near-duplicate chunks

These controls allow you to balance answer quality, latency, and prompt size. Sensible defaults are provided; see `example.config.ini` for recommended values.

---

## Streaming chat endpoint

### `POST /api/v1/chat/stream`

Stream a chat response incrementally using Server-Sent Events (SSE) format.

This endpoint is functionally equivalent to `/api/v1/chat` but returns the assistant response incrementally as it is generated, providing a much better user experience for interactive clients such as a TUI or web UI.

**Request:**

```json
{
  "prompt": "Write a haiku about streaming",
  "session": "default",
  "model": "llama3.1:8b",
  "rag_enabled": true,
  "rag_filters": {
    "doc_ids": ["README.md"],
    "filename": "README",
    "date_from": "2025-01-01"
  }
}
```

**Request Parameters:**
- `prompt` (required) - User's message
- `session` (optional) - Session name (default: "default")
- `model` (optional) - Model override
- `system` (optional) - System prompt override
- `rag_enabled` (optional) - Enable/disable RAG for this request
- `rag_filters` (optional) - Metadata filters for RAG document selection:
  - `doc_ids` (list[str]) - Filter by specific document IDs
  - `filename` (str) - Filter by filename (partial match, case-insensitive)
  - `tags` (list[str]) - Filter by tags (must have ALL tags)
  - `date_from` (str) - Filter by ingestion date >= (ISO format)
  - `date_to` (str) - Filter by ingestion date <= (ISO format)
- `attachments` (optional) - List of inline file attachments (see `AttachmentBlock` schema below)

#### `AttachmentBlock` Schema

Each element in the `attachments` list must conform to the following schema:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"image"` \| `"document"` | Yes | Attachment type |
| `media_type` | string | Yes | MIME type of the file |
| `data` | string | Yes | Base64-encoded file content (max ~20 MB, i.e. 27,000,000 base64 chars) |
| `filename` | string | No | Original filename (for display purposes) |

**Supported `media_type` values:**

- `image/jpeg`
- `image/png`
- `image/gif`
- `image/webp`
- `application/pdf`
- `text/plain`

**Example request with attachments:**

```json
{
  "prompt": "Summarise this document",
  "session": "default",
  "attachments": [
    {
      "type": "document",
      "media_type": "application/pdf",
      "data": "<base64-encoded PDF content>",
      "filename": "report.pdf"
    },
    {
      "type": "image",
      "media_type": "image/png",
      "data": "<base64-encoded PNG content>",
      "filename": "screenshot.png"
    }
  ]
}
```

**Notes:**
- Image attachments are passed directly to the model's vision API (Ollama multimodal models).
- Document attachments (PDF, plain text) are base64-decoded and their text content is prepended to the prompt.
- Attachments with an unrecognised `type` are rejected at the API boundary with a 422 validation error.
- The `data` field is limited to 27,000,000 characters (~20 MB base64) to protect server memory.

**Response:**

- HTTP 200
- `Content-Type: text/event-stream; charset=utf-8`
- `Cache-Control: no-cache`
- `Connection: keep-alive`
- Body is streamed as Server-Sent Events (SSE)

**SSE Event Format:**

The response uses standard SSE framing with structured JSON events. Each event includes timing/performance data and incremental metrics:

**1. Heartbeat Event** (sent immediately on connection):
```
event: heartbeat
data: {"timestamp": 1234567890.123}
id: 1

```

**2. Metadata Event** (session and model information):
```
event: metadata
data: {"session": "default", "model": "llama3.1:8b", "timestamp": 1234567890.123}
id: 2

```

**3. Text Events** (content chunks with performance data):
```
event: text
data: {"content": "Hello ", "tokens": 5, "elapsed": 0.123}
id: 3

event: text
data: {"content": "world!", "tokens": 10, "elapsed": 0.456}
id: 4

```

**4. RAG Context Event** (when RAG is enabled):
```
event: rag_context
data: {"type":"rag_metadata","chunks":[{"text":"...","score":0.95,"doc_id":"doc123","chunk_id":5}]}
id: 5

```

**5. Error Event** (if an error occurs):
```
event: error
data: {"error": "Connection timeout", "elapsed": 30.0}
id: 6

```

If the model runtime itself fails (crash, out-of-memory, timeout loading a
large model), `error` contains the same actionable message as the
non-streaming endpoint's `502` response, e.g. `"Model failed to run — it may
require more memory than is available on this host (...)"`, instead of a raw
Ollama error string.

**6. Done Event** (end of stream with final stats):
```
event: done
data: {"event_id": 7, "total_tokens": 150, "elapsed": 2.345}
id: 7

```

Example using `curl`:

```bash
curl -N http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Write a haiku about streaming","session":"default"}'
```

**Client Implementation:**

Clients should parse SSE events and handle each event type:

```javascript
const response = await fetch('/api/v1/chat/stream', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ prompt: 'Hello', session: 'test' })
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { value, done } = await reader.read();
  if (done) break;

  buffer += decoder.decode(value, { stream: true });
  const events = buffer.split('\n\n');
  buffer = events.pop() || '';

  for (const eventText of events) {
    const lines = eventText.split('\n');
    let eventType = '';
    let eventData = '';

    for (const line of lines) {
      if (line.startsWith('event:')) eventType = line.substring(6).trim();
      else if (line.startsWith('data:')) eventData = line.substring(5).trim();
    }

    if (eventType === 'heartbeat') {
      // Connection established
    } else if (eventType === 'metadata') {
      const metadata = JSON.parse(eventData);
      // Store session/model information (metadata.session, metadata.model)
    } else if (eventType === 'text') {
      const data = JSON.parse(eventData);
      // Append data.content to displayed message
      // Optional: display token count (data.tokens) and elapsed time (data.elapsed)
    } else if (eventType === 'rag_context') {
      const ragData = JSON.parse(eventData);
      // Store ragData.chunks for citation display
    } else if (eventType === 'error') {
      const errorData = JSON.parse(eventData);
      // Display error message (errorData.error)
      break;
    } else if (eventType === 'done') {
      const doneData = JSON.parse(eventData);
      // Stream complete - optional: display final stats
      // (doneData.total_tokens, doneData.elapsed)
      break;
    }
  }
}
```

Notes:

- The connection remains open until generation completes
- Retrieved RAG context (if enabled) is injected *before* streaming begins
- Event IDs are incremental integers
- The full response is persisted to the session once streaming completes
- SSE format provides better structure and reliability than plain text streaming
- All events include structured JSON data with timing and performance metrics
- Token counting is incremental (cumulative tokens generated so far)
- Elapsed time is measured from stream start and included in text, error, and done events
- Backward compatibility: clients can still handle legacy "message" and "rag_metadata" event names

### Client Capability Negotiation

The streaming endpoint supports **client capability hints** for content negotiation, allowing the server to adapt response format based on client capabilities. This enables graceful degradation for legacy clients while providing enhanced features for modern clients.

**Supported Client Hint Headers:**

| Header | Values | Description |
|--------|--------|-------------|
| `Accept` | `text/event-stream` | Standard HTTP Accept header for SSE |
| `X-Client-Supports-SSE` | `true`, `false` | Explicit SSE capability flag |
| `X-Client-Supports-Structured-Events` | `true`, `false` | Support for typed events (heartbeat, metadata, text, done, error) |
| `X-Client-Supports-Streaming` | `true`, `false` | Support for streaming responses (default: true) |
| `X-Client-Version` | String | Client version identifier (e.g., "web-ui/1.0.0") |
| `X-Client-Max-Event-Size` | Integer | Maximum event payload size in bytes (0 = unlimited) |

**Server Response Headers:**

The server responds with its own capability headers for feature detection:

| Header | Value | Description |
|--------|-------|-------------|
| `X-Server-Supports-SSE` | `true` | Server supports Server-Sent Events |
| `X-Server-Supports-Structured-Events` | `true` | Server supports structured event types |
| `X-Server-Supports-Streaming` | `true` | Server supports streaming responses |
| `X-Server-Version` | `nyxgpt/1.0.0` | Server version identifier |

**Response Format Adaptation:**

The server adapts the streaming response based on client capabilities:

1. **Modern clients** (SSE + structured events):
   - Content-Type: `text/event-stream`
   - Full structured SSE events (heartbeat, metadata, text, done, error)
   - Event IDs and timing metadata

2. **SSE-only clients** (SSE without structured events):
   - Content-Type: `text/event-stream`
   - Simple SSE `data:` events without typed event names
   - No metadata or done events

3. **Legacy clients** (no SSE support):
   - Content-Type: `text/plain`
   - Plain text streaming without SSE framing
   - No event structure or metadata

**Example - Modern Client Request:**

```bash
curl -N http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-Client-Supports-SSE: true" \
  -H "X-Client-Supports-Structured-Events: true" \
  -H "X-Client-Version: cli/1.0.0" \
  -d '{"prompt":"Hello","session":"test"}'
```

**Example - Legacy Client Request:**

```bash
curl -N http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "Accept: text/plain" \
  -H "X-Client-Supports-SSE: false" \
  -d '{"prompt":"Hello","session":"test"}'
```

**JavaScript/TypeScript Client Example:**

```typescript
const response = await fetch('/api/v1/chat/stream', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'text/event-stream',
    'X-Client-Supports-SSE': 'true',
    'X-Client-Supports-Structured-Events': 'true',
    'X-Client-Version': 'web-ui/1.0.0',
  },
  body: JSON.stringify({ prompt: 'Hello', session: 'test' })
});

// Check server capabilities from response headers
const serverSupportsSSE = response.headers.get('X-Server-Supports-SSE');
const serverVersion = response.headers.get('X-Server-Version');
```

**Benefits:**

- **Backwards compatibility**: Legacy clients continue to work without modifications
- **Progressive enhancement**: Modern clients receive enhanced features (typed events, metadata, timing)
- **Version negotiation**: Clients and servers can coordinate on feature sets
- **Graceful degradation**: Server adapts to client capabilities automatically

---

## RAG endpoints

RAG-related endpoints are documented in detail in:

➡️ [`docs/rag.md`](rag.md)

At a high level, the API supports:

- document ingestion
- vector search / retrieval
- RAG-assisted chat
- **metadata filtering** - filter queries by doc_id, filename, tags, or date range
- **collection management** - manage multi-model embedding collections
- **query result caching** - cache repeated query results with TTL expiration and automatic invalidation; monitor hit rate via `GET /api/v1/rag/cache/stats` or clear via `POST /api/v1/rag/cache/clear`

### `GET /api/v1/rag/collections`

List all RAG collections with statistics including document count, chunk count, and embedding models used.

**Response:**

```json
{
  "collections": [
    {
      "name": "default",
      "doc_count": 15,
      "chunk_count": 342,
      "embedding_models": ["nomic-embed-text"]
    },
    {
      "name": "all-minilm",
      "doc_count": 8,
      "chunk_count": 156,
      "embedding_models": ["all-minilm:latest"]
    }
  ]
}
```

**Response Fields:**
- `name` - Collection name
- `doc_count` - Number of documents in the collection
- `chunk_count` - Total number of chunks across all documents
- `embedding_models` - List of unique embedding models used in this collection

**Use Cases:**
- Monitor collection growth and usage
- Verify which embedding models are active
- Understand document distribution across collections

### `DELETE /api/v1/rag/collections/{name}`

Clear all data from a RAG collection (truncates the collection table).

**WARNING:** This operation permanently deletes all documents and chunks in the collection and cannot be undone.

**Path Parameters:**
- `name` - Collection name to clear

**Restrictions:**
- Cannot clear the `default` collection (returns 400 error)

**Response:**

```json
{
  "collection": "all-minilm",
  "status": "Collection 'all-minilm' has been cleared (truncated)"
}
```

**Error Responses:**
- `400 Bad Request` - Attempted to clear default collection (message: "Cannot clear the 'default' collection. This collection is protected.")
- `503 Service Unavailable` - Cassandra driver not available
- `500 Internal Server Error` - Failed to clear collection

### `GET /api/v1/rag/documents`

List all documents available in the RAG vector store with metadata.

**Response:**

```json
{
  "documents": [
    {
      "doc_id": "README.md",
      "chunks": 42,
      "embedding_model": "nomic-embed-text",
      "filename": "README.md",
      "tags": ["documentation"],
      "ingested_at": "2025-01-23T10:30:00"
    }
  ]
}
```

**Response Fields:**
- `doc_id` - Document identifier
- `chunks` - Number of chunks stored for this document
- `embedding_model` - Model used for embeddings
- `filename` - Original filename (from metadata)
- `tags` - Document tags (from metadata)
- `ingested_at` - Timestamp when document was ingested

### Metadata filtering

The `/api/v1/rag/query` and `/api/v1/chat/stream` endpoints support optional metadata filters to narrow search scope:

```json
{
  "query": "What is RAG?",
  "top_k": 5,
  "doc_ids": ["doc1", "doc2"],
  "filename": "notes",
  "tags": ["python", "tutorial"],
  "date_from": "2024-01-01",
  "date_to": "2024-12-31"
}
```

**Filter parameters:**
- `doc_ids` (list[str]): Filter by document IDs (OR logic)
- `filename` (str): Partial filename match (case-insensitive)
- `tags` (list[str]): Filter by tags (document must have ALL tags)
- `date_from` (str): ISO date string, filter by ingestion date >=
- `date_to` (str): ISO date string, filter by ingestion date <=

All filters are optional and combined with AND logic when present.

---

## Log viewing endpoints

The API provides endpoints for viewing and streaming log files, useful for debugging and monitoring.

### `GET /api/v1/logs/files`

List available log files with metadata.

**Response:**

```json
{
  "files": [
    {
      "name": "nyxgpt.log",
      "path": "/home/user/.nyxGPT/logs/nyxgpt.log",
      "size": 1024000,
      "modified": 1704067200.0
    }
  ],
  "log_dir": "/home/user/.nyxGPT/logs"
}
```

### `GET /api/v1/logs/view/{filename}`

View log file contents with optional filtering.

**Query Parameters:**

- `tail` (optional): Number of lines to return from the end (default: all lines)
- `level` (optional): Filter by log level (DEBUG, INFO, WARNING, ERROR)
- `search` (optional): Search string to filter lines (case-insensitive)

**Example:**

```bash
# Get last 100 lines
curl "http://127.0.0.1:8000/api/v1/logs/view/nyxgpt.log?tail=100"

# Filter by log level
curl "http://127.0.0.1:8000/api/v1/logs/view/nyxgpt.log?level=ERROR"

# Search for specific text
curl "http://127.0.0.1:8000/api/v1/logs/view/nyxgpt.log?search=session"

# Combine filters
curl "http://127.0.0.1:8000/api/v1/logs/view/nyxgpt.log?tail=50&level=INFO&search=chat"
```

**Response:**

```json
{
  "filename": "nyxgpt.log",
  "lines": ["2024-01-01 12:00:00 INFO ...", "..."],
  "total_lines": 1000,
  "filtered_lines": 50
}
```

### `GET /api/v1/logs/stream/{filename}`

Stream log file contents with optional filtering. Useful for real-time log viewing.

**Query Parameters:**

- `level` (optional): Filter by log level (DEBUG, INFO, WARNING, ERROR)
- `search` (optional): Search string to filter lines (case-insensitive)

**Example:**

```bash
# Stream all logs
curl "http://127.0.0.1:8000/api/v1/logs/stream/nyxgpt.log"

# Stream only ERROR logs
curl "http://127.0.0.1:8000/api/v1/logs/stream/nyxgpt.log?level=ERROR"
```

**Response:** Text stream (Content-Type: text/plain)

**Security Notes:**

- File paths are sanitized to prevent path traversal attacks
- Access is restricted to files within the configured log directory
- Attempting to access files outside the log directory returns 403 Forbidden

---

## Authentication

nyxGPT API supports optional API key authentication. Authentication is **disabled by default** for local-only usage and can be enabled via configuration when additional security is needed.

### Overview

When authentication is enabled:

- All `/api/v1/*` endpoints require a valid API key
- Health check (`/health`) and documentation endpoints (`/docs`, `/openapi.json`, `/redoc`) remain publicly accessible
- Invalid or missing API keys return `401 Unauthorized` with a request ID for debugging
- API keys are compared using constant-time comparison to prevent timing attacks

### Configuration

Authentication is configured in `~/.nyxGPT/config.ini` under the `[auth]` section.

#### Configuration Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Enable/disable API key authentication |
| `api_key` | string | (empty) | Shared secret required for API access |
| `header` | string | `X-API-Key` | HTTP header name for the API key |

#### Example Configuration

```ini
[auth]
# Enable API key authentication
enabled = true

# Shared secret (required when enabled)
# IMPORTANT: Generate a strong, random key
api_key = your-secret-key-here

# HTTP header used to pass the API key
# Default: X-API-Key
header = X-API-Key
```

#### Generating a Secure API Key

For production or security-sensitive environments, generate a strong random key:

```bash
# macOS/Linux: Generate 32-byte random key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Example output: ZqJ9X_vK2nP8mR5tL3wH7yU4sN1aB6cE9fG0dI2jK8
```

### Header Format and Usage

#### Request Format

Include the API key in the HTTP header specified in your configuration:

```bash
curl http://127.0.0.1:8000/api/v1/info \
  -H "X-API-Key: your-secret-key-here"
```

#### Custom Header Name

If you configure a custom header name:

```ini
[auth]
enabled = true
api_key = my-secret-key
header = Authorization
```

Then use that header in requests:

```bash
curl http://127.0.0.1:8000/api/v1/info \
  -H "Authorization: my-secret-key"
```

### Example Authenticated Requests

#### Chat Request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-secret-key-here" \
  -d '{
    "prompt": "Hello, how are you?",
    "session": "my-session",
    "model": "llama3.1:8b"
  }'
```

#### Streaming Chat Request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-secret-key-here" \
  -N \
  -d '{
    "prompt": "Write a haiku about security",
    "session": "my-session"
  }'
```

#### Session List Request

```bash
curl http://127.0.0.1:8000/api/v1/sessions \
  -H "X-API-Key: your-secret-key-here"
```

#### RAG Ingest Request

Ingest documents with automatic update detection. The Cassandra keyspace and
table for the target collection are created automatically on first ingest,
so `ensure_schema` is optional -- pass it explicitly only if you want to be
sure a specific collection gets created:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/rag/ingest \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-secret-key-here" \
  -d '{
    "doc_id": "doc123",
    "text": "This is important documentation.",
    "ensure_schema": true
  }'
```

**Response:**
```json
{
  "doc_id": "doc123",
  "chunks_ingested": 5,
  "status": "ingested",
  "doc_hash": "8f4e2a1b3c9d8f2a...",
  "previous_hash": null
}
```

Status values:
- `"ingested"`: New document added
- `"updated"`: Existing document with changed content
- `"skipped"`: Existing document with unchanged content

#### Get Document Information

Retrieve version tracking information:

```bash
curl http://127.0.0.1:8000/api/v1/rag/documents/doc123?collection=default
```

**Response:**
```json
{
  "doc_id": "doc123",
  "doc_hash": "8f4e2a1b3c9d8f2a...",
  "ingested_at": "2026-01-20T10:30:45",
  "updated_at": "2026-01-20T14:15:22",
  "chunks": 5,
  "embedding_model": "nomic-embed-text:latest"
}
```

### Error Responses

#### Missing API Key

When authentication is enabled but no API key is provided:

```bash
curl http://127.0.0.1:8000/api/v1/info
```

**Response** (HTTP 401):

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid or missing API key",
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

#### Invalid API Key

When an incorrect API key is provided:

```bash
curl http://127.0.0.1:8000/api/v1/info \
  -H "X-API-Key: wrong-key"
```

**Response** (HTTP 401):

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid or missing API key",
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

**Note**: The error message is intentionally identical for missing and invalid keys to prevent information leakage about whether a key was provided.

### Hot-Reload Support

Authentication configuration is hot-reloaded on every request. Changes to `~/.nyxGPT/config.ini` take effect immediately without restarting the API:

```bash
# 1. Edit config to enable auth
vim ~/.nyxGPT/config.ini

# 2. Save changes
# [auth]
# enabled = true
# api_key = my-new-key

# 3. Next request will require authentication (no restart needed)
curl http://127.0.0.1:8000/api/v1/info \
  -H "X-API-Key: my-new-key"
```

### Security Features

#### Constant-Time Comparison

API keys are compared using `secrets.compare_digest()` to prevent timing attacks. This ensures that attackers cannot determine the correct API key by measuring response times.

**Implementation** (from `src/nyxgpt/app.py:349`):

```python
auth_valid = secrets.compare_digest(expected, provided)
```

#### Request ID Tracking

All authentication failures include a request ID in the error response and logs, enabling correlation for security auditing:

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid or missing API key",
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

Check logs for details:

```bash
grep "550e8400-e29b-41d4-a716-446655440000" ~/.nyxGPT/logs/nyxgpt.log
```

#### Exempt Endpoints

The following endpoints remain accessible without authentication even when `auth.enabled = true`:

- `/health` - Health check endpoint
- `/docs` - OpenAPI documentation UI
- `/openapi.json` - OpenAPI schema
- `/redoc` - ReDoc documentation UI

This ensures monitoring and documentation remain accessible while protecting functional API endpoints.

### Security Recommendations

#### For Local Development (Default)

Authentication is disabled by default and **not required** for local-only development:

```ini
[auth]
enabled = false
```

This configuration is appropriate when:
- The API binds to `127.0.0.1` (localhost only)
- No external network access to the API
- Single-user development environment

#### For Shared Environments

Enable authentication when:
- The API is accessible from a network (even local network)
- Multiple users share the same machine
- Additional security layer is desired

**Recommended configuration:**

```ini
[auth]
enabled = true
api_key = <generate-strong-random-key>
header = X-API-Key
```

**Generate a strong key:**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

#### Key Management Best Practices

1. **Generate Strong Keys**
   - Use cryptographically secure random generation
   - Minimum 32 bytes of entropy
   - Never reuse keys across systems

2. **Store Keys Securely**
   - Never commit `~/.nyxGPT/config.ini` to version control
   - Restrict file permissions: `chmod 600 ~/.nyxGPT/config.ini`
   - Never log or expose API keys

3. **Rotate Keys Regularly**
   - Change API keys periodically
   - Rotate immediately if compromise suspected
   - Use hot-reload feature for zero-downtime rotation

4. **Transport Security**
   - Use HTTPS if API is exposed beyond localhost
   - Never send API keys over unencrypted HTTP on public networks
   - Consider VPN or SSH tunnels for remote access

5. **Monitor Access**
   - Review logs regularly for unauthorized attempts
   - Use request IDs to correlate suspicious activity
   - Set up alerts for repeated authentication failures

#### What Authentication Does NOT Protect Against

API key authentication provides a basic access control layer but is **not a substitute for:**

- **Network security**: Use firewalls, VPNs, or SSH tunnels for network isolation
- **Transport encryption**: Use HTTPS/TLS for encrypted communication
- **Rate limiting**: Configure `[rate_limit]` section to prevent abuse
- **Input validation**: Application-level validation is always enforced
- **User authentication**: This is a shared-secret system, not per-user authentication

#### Production Deployment Considerations

nyxGPT is designed for **local, single-user use**. For production or multi-user deployments, consider:

1. **HTTPS/TLS termination** via reverse proxy (nginx, caddy)
2. **Per-user authentication** instead of shared API keys
3. **OAuth2 or JWT** for more sophisticated auth
4. **Database-backed session management**
5. **Comprehensive audit logging**
6. **DDoS protection and rate limiting**

These features are beyond the scope of nyxGPT's current design but can be layered on top using standard infrastructure tools.

### Troubleshooting

#### Authentication Not Working

**Symptom**: Requests still work without API key despite `enabled = true`

**Solutions**:

1. Verify config file location:
   ```bash
   cat ~/.nyxGPT/config.ini
   ```

2. Check for syntax errors in config:
   ```bash
   python3 -c "from configparser import ConfigParser; c = ConfigParser(); c.read('$HOME/.nyxGPT/config.ini'); print(c.getboolean('auth', 'enabled'))"
   ```

3. Check API logs for authentication status:
   ```bash
   tail -f ~/.nyxGPT/logs/nyxgpt.log | grep auth
   ```

#### Can't Access API After Enabling Auth

**Symptom**: All requests return 401 after enabling authentication

**Solutions**:

1. Verify you're including the header:
   ```bash
   curl -v http://127.0.0.1:8000/api/v1/info \
     -H "X-API-Key: your-key"
   ```

2. Check header name matches config:
   ```bash
   grep "^header" ~/.nyxGPT/config.ini
   ```

3. Verify API key matches config exactly (no extra spaces):
   ```bash
   grep "^api_key" ~/.nyxGPT/config.ini
   ```

4. Temporarily disable auth to verify API is working:
   ```bash
   # Edit config
   vim ~/.nyxGPT/config.ini
   # Set: enabled = false
   # Test
   curl http://127.0.0.1:8000/api/v1/info
   ```

#### Web UI Authentication

The Next.js web UI reads the same `~/.nyxGPT/config.ini` file and automatically includes the API key in requests to the FastAPI backend. No additional configuration is needed.

**Verification**:

```bash
# Check web UI proxy configuration
grep -A 3 "\[auth\]" ~/.nyxGPT/config.ini
```

The web UI will automatically detect when authentication is enabled and include the configured API key in all backend requests.

---

## Operational Tasks

These tasks cover keeping nyxGPT services and supporting infrastructure running reliably across reboots.

### Docker Desktop startup (required)

Cassandra runs inside Docker and requires Docker Desktop to be running.

1. Open **Docker Desktop**
2. Go to **Settings → General**
3. Enable **Start Docker Desktop when you log in**
4. Verify:
   ```bash
   docker info
   ```

Docker Desktop must be running before any Cassandra container can start.

---

### Cassandra container (persistent + auto-restart)

The Cassandra container should be created with an auto-restart policy so it survives reboots.

Verify restart policy:

```bash
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' nyxgpt-cassandra
```

Expected output:

```
unless-stopped
```
```bash
# verify Cassandra data is on a named volume (persistence)
docker inspect nyxgpt-cassandra \
  --format '{{ range .Mounts }}{{ .Name }} -> {{ .Destination }}{{ println }}{{ end }}'
```

Expected output should include something like:

```
nyxgpt_cassandra_data -> /var/lib/cassandra
```

If not set correctly, recreate the container:

```bash
docker rm -f nyxgpt-cassandra

# create the named volume (safe if it already exists)
docker volume create nyxgpt_cassandra_data

docker run -d \
  --name nyxgpt-cassandra \
  --restart unless-stopped \
  -p 9042:9042 \
  -e CASSANDRA_CLUSTER_NAME=nyxgpt \
  -v nyxgpt_cassandra_data:/var/lib/cassandra \
  cassandra:5.0
```


Cassandra can take a minute to become ready after a fresh start. If `cqlsh` is installed on your Mac, you can verify from the host (no `docker exec` needed):

```bash
cqlsh 127.0.0.1 9042 -e "DESCRIBE KEYSPACES;"
```

---

### Centralized logs

nyxGPT consolidates logs under:

```
~/.nyxGPT/logs
```

This includes:

- nyxGPT application logs
- test logs
- streamed Cassandra logs
- Ollama logs (symlinked in native mode, streamed in Compose mode -- see
  [Ollama logs](#ollama-logs))

---

### Cassandra logs via Docker (LaunchAgent)

Cassandra logs are streamed from Docker into `~/.nyxGPT/logs` using a macOS LaunchAgent.

Installed files (tracked in the repo):

- `follow-cassandra-logs.sh`
- `com.nyxgpt.cassandra-logs.plist`

Install and activate:

```bash
nyxgpt ops install
# optional:
# nyxgpt ops install --repo-dir /path/to/nyxGPT
# nyxgpt ops install --force
```

Verify the agent is loaded:

```bash
launchctl list | grep com.nyxgpt.cassandra-logs
```

Verify logs:

```bash
tail -f ~/.nyxGPT/logs/cassandra-logfollower.out.log
tail -f ~/.nyxGPT/logs/cassandra-logfollower.err.log
```

This survives reboots.

---

### nyxgpt ops commands

`nyxgpt` provides built-in operational checks so you don’t have to remember Docker, LaunchAgent, and API details by hand.

#### `nyxgpt ops install`
Installs the Cassandra log follower LaunchAgent and prepares local log directories.

```bash
nyxgpt ops install
# optional:
# nyxgpt ops install --repo-dir /path/to/nyxGPT
# nyxgpt ops install --force
```

This command is safe to re-run.

- Exit code `0` → all install steps succeeded
- Exit code `2` → one or more steps failed (details are printed)

#### `nyxgpt ops status`
Shows the current health of local dependencies and services:

- Docker daemon reachable
- Cassandra container running
- Restart policy (`unless-stopped`)
- Cassandra data mounted to `/var/lib/cassandra`
- Cassandra log LaunchAgent loaded
- Expected log files present under `~/.nyxGPT/logs`
- FastAPI `/health` endpoint reachable

```bash
nyxgpt ops status
```

This command always exits `0` and is informational.

#### `nyxgpt ops doctor`
Runs the same checks as `status` but **fails fast** if anything is broken.

```bash
nyxgpt ops doctor
echo "exit=$?"
```

- Exit code `0` → all required services are healthy
- Exit code `2` → one or more checks failed (details are printed)

---
#### `nyxgpt ops restart`

Restart one or more nyxGPT-managed services without calling `brew`, `docker`,
or `launchctl` directly.

This is the recommended way to apply configuration changes or recover from
transient failures.

**Usage:**

```bash
nyxgpt ops restart
```

### Ollama logs

`nyxgpt ops install` captures Ollama's logs into `~/.nyxGPT/logs/ollama.log`
automatically, in whichever mode Ollama is actually running:

- **Native mode** (Ollama as a Homebrew service): `_ensure_log_symlinks`
  symlinks Homebrew's `ollama.log` into `~/.nyxGPT/logs/ollama.log`.
- **Compose mode** (Ollama as the `ollama`/`nyxgpt-ollama` container): the
  `com.nyxgpt.ollama-logs` LaunchAgent runs `scripts/follow-ollama-logs.sh`,
  which tails `docker logs -f nyxgpt-ollama` into the same
  `~/.nyxGPT/logs/ollama.log` path -- mirroring how the
  [Cassandra log follower](#cassandra-logs-via-docker-launchagent) works.
  It replaces any leftover symlink from the native-mode path the first time
  it sees the container, so the two mechanisms never collide.

Either way, `~/.nyxGPT/logs/ollama.log` is picked up by promtail's existing
`*.log*` glob (see [Centralized logs](#centralized-logs)) with no extra
configuration, so it's searchable in Grafana Logs Explorer -- filter on
Loki's auto-attached `filename` label to isolate it from the rest of the
`job="nyxgpt"` stream. Ollama's own log lines don't match nyxGPT's
`level`/`logger` extraction regex (that's for the app's own structured
format), so -- like the existing Cassandra logs -- they won't populate the
Operational Logs dashboard's per-component `$logger` dropdown; they're
still fully text-searchable there and in Explore.

Verify:

```bash
tail -f ~/.nyxGPT/logs/ollama.log
```

If you don't see output, confirm Ollama is actually running:

```bash
brew services info ollama
curl -s http://127.0.0.1:11434/api/tags | head
```

**Manual fallback** (e.g. troubleshooting, or a non-Homebrew Ollama
install) -- symlink whichever Homebrew log path exists:

```bash
mkdir -p ~/.nyxGPT/logs
for p in /usr/local/var/log/ollama.log /opt/homebrew/var/log/ollama.log; do
  if [[ -f "$p" ]]; then
    ln -sf "$p" ~/.nyxGPT/logs/ollama.log
    echo "linked $p -> ~/.nyxGPT/logs/ollama.log"
    break
  fi
done
```

---

### Local Web UI service (Next.js)

The local web UI is a small Next.js application that connects to the FastAPI backend.

It is intended to run:
- locally only
- as a background service
- using the same configuration file as the rest of nyxGPT

Configuration is read from `~/.nyxGPT/config.ini` under the `[web]` and `[paths]` sections.

#### Runtime configuration keys

```ini
[web]
host = 127.0.0.1
port = 3000
api_base_url =
```

- `host` — interface the web UI binds to
- `port` — port the web UI listens on
- `api_base_url` — optional override of FastAPI base URL; if unset, `[api] host/port` is used

The web UI is launched using a small wrapper script that reads this configuration.

Note: Homebrew/launchd services run with a minimal `PATH`. The web wrapper (`nyxgpt-web`) and `~/.nyxGPT/scripts/run-web.sh` ensure `node` is discoverable (via `[paths] node_bin` / `npm_bin`) so `npm` can run reliably in the background.

---

### Web UI background service

The web UI is managed via Homebrew services, similar to the FastAPI backend.

Install the service:

```bash
nyxgpt ops install
```

Verify status:

```bash
brew services list | grep nyxgpt-web
```

Start manually if needed:

```bash
brew services start nyxgpt-web
```

Logs are written to:

```
~/.nyxGPT/logs/nyxgpt-web.log
~/.nyxGPT/logs/nyxgpt-web.err.log
```

---

### Web UI manual startup (development)

For development or debugging, you can run the web UI manually:

```bash
cd web
npm install
npm run dev
```

By default, the UI will be available at:

```
http://127.0.0.1:3000
```

Ollama logs land in the same directory automatically -- see [Ollama
logs](#ollama-logs) above.

Verify:

```bash
tail -f ~/.nyxGPT/logs/ollama.log
```

---

### API background service

The FastAPI backend is expected to run via Homebrew services.

Verify status:

```bash
brew services list | grep nyxgpt-api
```

Start if needed:

```bash
brew services start nyxgpt-api
```

---

### Health verification checklist

After reboot:

```bash
docker ps | grep nyxgpt-cassandra
brew services list | grep nyxgpt-api
curl http://127.0.0.1:8000/health
```

Expected:

```json
{ "status": "ok" }
```

---

## Request Batching

Request batching groups multiple independent chat/RAG requests together for improved throughput. When enabled, the API collects incoming requests in a queue and processes them in batches, reducing overhead and improving overall system performance.

### Features

- **Configurable batch size**: Control how many requests to group together
- **Configurable wait time**: Set maximum time to wait for batch to fill
- **Priority handling**: Interactive requests get higher priority than batch requests
- **Metrics collection**: Monitor batching efficiency with detailed statistics

### Configuration

Batching is configured in `~/.nyxGPT/config.ini` under the `[batch]` section:

```ini
[batch]
# Enable/disable request batching (default: false)
enabled = false

# Maximum number of requests to batch together (default: 4, range: 1-50)
batch_size = 4

# Maximum time to wait for batch to fill in milliseconds (default: 100ms, range: 10-5000ms)
wait_time_ms = 100
```

### Metrics Endpoint

Monitor batching efficiency via the metrics endpoint:

```bash
curl http://127.0.0.1:8000/api/v1/batch/metrics
```

**Response (when batching enabled):**

```json
{
  "enabled": true,
  "total_requests": 150,
  "total_batches": 42,
  "avg_batch_size": 3.57,
  "avg_wait_time_ms": 45.23,
  "avg_process_time_ms": 234.56,
  "requests_per_second": 12.5,
  "interactive_requests": 90,
  "batch_requests": 60
}
```

**Response (when batching disabled):**

```json
{
  "enabled": false,
  "message": "Request batching is not enabled"
}
```

### Performance Tuning

**Batch size:**
- Larger batches improve throughput but add latency
- Smaller batches reduce latency but may reduce throughput
- Recommended range: 2-10 for typical workloads

**Wait time:**
- Lower values reduce latency (more responsive)
- Higher values improve batching efficiency (better throughput)
- Recommended range: 50-200ms for interactive use, 200-1000ms for batch processing

**When to enable batching:**
- Multiple concurrent users or clients
- Batch processing of many requests
- High-volume API workloads
- When throughput is more important than individual request latency

---

## Resource Usage Monitoring

### `GET /api/v1/metrics`

Monitor system resource usage including memory, CPU, request latency, and queue depth.

**Request:**

```bash
curl http://127.0.0.1:8000/api/v1/metrics
```

**Response:**

```json
{
  "memory": {
    "rss_mb": 245.32,
    "vms_mb": 512.45,
    "percent": 3.21,
    "available_mb": 8192.00
  },
  "cpu": {
    "process_percent": 12.5,
    "system_percent": 45.8
  },
  "latency": {
    "avg_ms": 23.45,
    "p50_ms": 18.23,
    "p95_ms": 89.12,
    "p99_ms": 156.78
  },
  "queue": {
    "depth": 3,
    "total_requests": 1234
  },
  "errors": {
    "count": 2,
    "rate_percent": 0.16
  }
}
```

**Response Fields:**

**Memory Metrics:**
- `rss_mb` - Resident Set Size (physical memory used by process) in MB
- `vms_mb` - Virtual Memory Size in MB
- `percent` - Percentage of system memory used by process
- `available_mb` - Available system memory in MB

**CPU Metrics:**
- `process_percent` - CPU usage percentage for this process (0-100 per core)
- `system_percent` - Overall system CPU usage percentage

**Latency Metrics:**
- `avg_ms` - Average request latency in milliseconds
- `p50_ms` - 50th percentile (median) request latency
- `p95_ms` - 95th percentile request latency (95% of requests faster than this)
- `p99_ms` - 99th percentile request latency (99% of requests faster than this)

**Queue Metrics:**
- `depth` - Current number of requests in batch processing queue (0 if batching disabled)
- `total_requests` - Total number of requests tracked since server startup

**Error Metrics:**
- `count` - Number of HTTP 5xx responses in the sliding window (last 1000 requests)
- `rate_percent` - Percentage of sampled requests that were HTTP 5xx errors; used by
  `nyxgpt canary evaluate` (see [Canary Deployment](#canary-deployment)) for
  metrics-based promotion/rollback decisions

**Use Cases:**
- Performance monitoring and alerting
- Capacity planning and resource optimization
- Identifying performance bottlenecks
- Tracking request latency over time
- Monitoring system health during high load

### Web UI Dashboard

The web UI provides a visual dashboard for resource usage metrics accessible from the Settings menu:

**Accessing the Dashboard:**
1. Open the web UI at `http://localhost:3000`
2. Click the Settings menu (⚙️ icon)
3. Select "Resource Usage"

**Dashboard Features:**
- Real-time metric updates (auto-refresh every 5 seconds)
- Visual display of memory, CPU, latency, and queue metrics
- Color-coded warning indicators (normal/warning/critical thresholds)
- Historical trends with configurable time ranges (1 hour, 24 hours, 7 days)
- Export functionality (CSV and JSON formats)
- Toggle auto-refresh on/off

**Warning Thresholds:**
- Memory: >75% warning, >90% critical
- CPU (Process): >60% warning, >80% critical
- CPU (System): >75% warning, >90% critical
- Latency (P99): >500ms warning, >1000ms critical

**When to disable batching:**
- Single-user interactive usage
- When latency is critical
- Low-volume workloads

### Example Configuration

**Interactive usage (low latency):**
```ini
[batch]
enabled = true
batch_size = 2
wait_time_ms = 50
```

**Batch processing (high throughput):**
```ini
[batch]
enabled = true
batch_size = 10
wait_time_ms = 500
```

---

## Distributed Tracing

### `GET /api/v1/tracing`

Report distributed tracing status: whether it's enabled in config, whether
it actually initialized for this process, and where to find the local
Jaeger UI.

**Request:**

```bash
curl http://127.0.0.1:8000/api/v1/tracing
```

**Response:**

```json
{
  "enabled": true,
  "service_name": "nyxgpt-api",
  "otlp_endpoint": "http://localhost:4318/v1/traces",
  "jaeger_ui_url": "http://localhost:16686",
  "active": true,
  "curated_views": [
    {
      "label": "Chat requests",
      "hint": "Filter by operation: POST /api/v1/chat or POST /api/v1/chat/stream",
      "url": "http://localhost:16686/search?service=nyxgpt-api&lookback=1h"
    },
    {
      "label": "RAG query",
      "hint": "Filter by operation: rag.retrieve (the retrieval pipeline span)",
      "url": "http://localhost:16686/search?service=nyxgpt-api&lookback=1h"
    },
    {
      "label": "RAG ingest",
      "hint": "Filter by operation: POST /api/v1/rag/ingest, /rag/upload, or /rag/index-repo",
      "url": "http://localhost:16686/search?service=nyxgpt-api&lookback=1h"
    },
    {
      "label": "Ollama backend calls",
      "hint": "Filter by operation: ollama.request, ollama.request.stream, or ollama.embeddings",
      "url": "http://localhost:16686/search?service=nyxgpt-api&lookback=1h"
    }
  ]
}
```

**Response Fields:**
- `enabled` - Whether `[tracing] enabled = true` in config.ini
- `service_name` - Service name attached to every span
- `otlp_endpoint` - OTLP/HTTP endpoint spans are exported to
- `jaeger_ui_url` - URL of the local Jaeger UI for browsing traces
- `active` - Whether tracing actually initialized for this running process (mirrors `enabled` unless startup failed)
- `curated_views` - Curated deep links into Jaeger's trace search for the main request flows (chat, RAG query/ingest, Ollama backend calls), each with a `hint` naming the operation(s) to pick from the dropdown. Surfaced by the `/admin/observability` ("SRE Overview") page.

**How it works:**

Tracing is OpenTelemetry-based, opt-in, and strictly local — there is no
external/cloud exporter. When enabled:
- Every HTTP request (chat, RAG, etc.) gets its own span, auto-instrumented via `opentelemetry-instrumentation-fastapi`
- Cassandra queries get their own child spans, auto-instrumented via `opentelemetry-instrumentation-cassandra`
- Ollama HTTP calls (chat, streaming chat, embeddings) get manual spans
- RAG retrieval (`retrieve_context`) gets a `rag.retrieve` span
- Spans are batched and exported over OTLP/HTTP to a local collector

**Enabling tracing:**

```ini
[tracing]
enabled = true
service_name = nyxgpt-api
otlp_endpoint = http://localhost:4318/v1/traces
jaeger_ui_url = http://localhost:16686
```

The local collector + Jaeger all-in-one (`tracing` Compose profile) already
starts automatically with `nyxgpt ops install`. To start it on its own or
re-run it later (never a raw `docker compose` command):

```bash
nyxgpt ops observability
```

Browse traces at `http://localhost:16686` (also linked from the SRE/admin
dashboard's Resource Usage step — see [`docs/configuration.md`](configuration.md)).

---

## Error Tracking

### `GET /api/v1/error-tracking`

Report error tracking status: whether it's enabled in config, whether it
actually initialized for this process, and where to find the local
GlitchTip UI.

**Request:**

```bash
curl http://127.0.0.1:8000/api/v1/error-tracking
```

**Response:**

```json
{
  "enabled": true,
  "dsn": "http://key@localhost:8080/1",
  "environment": "production",
  "release": "",
  "traces_sample_rate": 0.0,
  "glitchtip_ui_url": "http://localhost:8080",
  "active": true
}
```

**Response Fields:**
- `enabled` - Whether `[error_tracking] enabled = true` in config.ini
- `dsn` - DSN of the configured self-hosted tracker project
- `environment` - Environment tag attached to every event
- `release` - Release tag attached to every event, if set
- `traces_sample_rate` - Fraction of requests also sampled for performance monitoring
- `glitchtip_ui_url` - URL of the local GlitchTip UI for browsing events
- `active` - Whether error tracking actually initialized for this running process (requires both `enabled = true` and a non-empty `dsn`)

### `POST /api/v1/error-tracking/report`

Forward a web UI client-side error to the local error tracker.

**Request:**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/error-tracking/report \
  -H "Content-Type: application/json" \
  -d '{"message": "TypeError: Cannot read properties of undefined", "stack": "at ChatPane (ChatPane.tsx:42:1)", "url": "/"}'
```

**Response, tracking active (`202`):**

```json
{ "status": "accepted" }
```

**Response, tracking inactive (`503`):**

```json
{ "status": "inactive", "detail": "Error tracking is disabled or has no valid DSN configured; this event was not sent." }
```

The endpoint distinguishes these two cases on purpose: a misconfigured DSN
or a tracker nobody enabled must not look like a successfully delivered
event. The web UI's fire-and-forget client error reporter
(`ClientErrorReporter.tsx`) ignores the response either way, so it's safe
to call whether or not tracking is active; the Error Tracking panel's "Send
test event" button surfaces the real result to an operator.

**How it works:**

Error tracking is **Python** Sentry-SDK-protocol-based (`sentry_sdk`, see
`src/nyxgpt/error_tracking.py`), opt-in, and strictly local — there is no
default DSN and nothing here talks to Sentry's own SaaS. If GlitchTip's own
onboarding screen shows Node.js/`@sentry/node` setup instructions when you
create a project, ignore them — that's not this integration. When enabled
with a DSN:
- Unhandled backend exceptions are captured automatically, enriched with the request ID, path, and method
- Web UI errors reported via `POST /api/v1/error-tracking/report` are forwarded as message-level events

**Enabling error tracking (zero-touch):**

1. `nyxgpt ops install` already does everything below by default:
   starts the local tracker (`errors` Compose profile), then runs
   `nyxgpt ops glitchtip-init` to auto-provision its admin user,
   organization, project, and DSN — no sign-in, no copy-pasting a DSN. The
   API still needs a restart to pick it up (this setting isn't
   hot-reloaded).

2. If you skipped observability (`--skip-observability`) or need to
   re-run either step on its own (never a raw `docker compose` command):

   ```bash
   nyxgpt ops observability     # starts the errors Compose profile
   nyxgpt ops glitchtip-init    # provisions admin user/org/project/DSN
   ```

   `glitchtip-init` is idempotent and safe to re-run any time — it reuses
   an existing admin user/org/project/key rather than duplicating them,
   and no-ops with a clear message if the `glitchtip` container isn't
   up/healthy yet (e.g. still inside its post-start health-check window).

3. Once it succeeds, `[error_tracking] enabled`/`dsn` in config.ini are
   filled in for you:

   ```ini
   [error_tracking]
   enabled = true
   dsn = http://<public_key>@localhost:8080/<project_id>
   environment = production
   traces_sample_rate = 0.0
   glitchtip_ui_url = http://localhost:8080
   ```

   The `localhost` host is correct for a native deployment. For a
   containerized API, GlitchTip still hands out a `localhost`-hosted DSN
   (browsers need to reach its UI), but `init_error_tracking` automatically
   rewrites that host to the `glitchtip` Compose service name at startup —
   there is no host to edit by hand.

Prefer to do it by hand instead? Register an account at
`http://localhost:8080` (its confirmation email is printed to the
`glitchtip` container's console — `EMAIL_URL=consolemail://` — read it
with the "View GlitchTip logs" button on the Error Tracking panel or
`nyxgpt ops logs glitchtip`), create a project, and paste its DSN into
config.ini yourself, same as above, then restart the API.

Browse events at `http://localhost:8080` (also linked from the SRE/admin
dashboard's Resource Usage step). See
[`docs/ops.md`](ops.md#nyxgpt-ops-glitchtip-init) for the full
`glitchtip-init` reference.

**Recommended alerting for the default project:** GlitchTip has no
file-based provisioning for projects or alert rules (unlike Grafana's
`docker/grafana/dashboards`) — every project it manages is created
through its own interactive UI/API after the operator's own account
registration, so there is no meaningful "default project" to ship as
code beyond what step 3 above already creates. Once that project
exists, open **Project Settings → Alerts** in the GlitchTip UI and add a
rule that notifies (via the project's configured notification channel)
on **"A new issue is created"** with **quantity ≥ 1** — this surfaces
newly-seen exception types as soon as nyxGPT first reports them, which
is the alerting behavior appropriate for a single-operator error tracker.

---

## Monitoring Dashboards

### `GET /api/v1/monitoring`

Report monitoring stack status: whether it's enabled in config and where to
find the local Grafana and Prometheus UIs.

**Request:**

```bash
curl http://127.0.0.1:8000/api/v1/monitoring
```

**Response:**

```json
{
  "enabled": true,
  "grafana_ui_url": "http://localhost:3001",
  "prometheus_ui_url": "http://localhost:9090",
  "active": true
}
```

**Response Fields:**
- `enabled` - Whether `[monitoring] enabled = true` in config.ini
- `grafana_ui_url` - URL of the local Grafana UI for browsing dashboards
- `prometheus_ui_url` - URL of the local Prometheus UI
- `active` - Mirrors `enabled` (the Grafana/Prometheus stack itself runs outside this process, as Docker Compose services)

**How it works:**

Monitoring is Prometheus/Grafana-based, opt-in, and strictly local — there
is no external/cloud monitoring service. System overview, RAG performance,
API metrics, and resource usage dashboards are pre-provisioned in Grafana,
backed by a Prometheus server that scrapes this API's
[`/metrics`](#get-metrics) endpoint. The `/admin/observability` **SRE
Overview** page (reachable from `/admin/dashboard`) is the single entry
point into all of it — every Grafana dashboard, curated Loki queries,
Jaeger trace views, and GlitchTip error tracking.

**Enabling monitoring:**

```ini
[monitoring]
enabled = true
grafana_ui_url = http://localhost:3001
prometheus_ui_url = http://localhost:9090
```

The local Prometheus + Grafana instances (`monitoring` Compose profile)
already start automatically with `nyxgpt ops install`. To start it on its
own or re-run it later (never a raw `docker compose` command):

```bash
nyxgpt ops observability
```

Browse dashboards at `http://localhost:3001` (also linked from the
SRE/admin dashboard's Resource Usage step — see
[`docs/docker-compose.md`](docker-compose.md#monitoring-dashboards)).

---

## Log Aggregation

### `GET /api/v1/log-aggregation`

Report log aggregation stack status: whether it's enabled in config and
where to search logs in Grafana.

**Request:**

```bash
curl http://127.0.0.1:8000/api/v1/log-aggregation
```

**Response:**

```json
{
  "enabled": true,
  "grafana_explore_url": "http://localhost:3001/explore",
  "active": true,
  "curated_queries": [
    {
      "label": "Self-heal events",
      "hint": "Restart / recovery / backoff activity",
      "query": "{job=\"nyxgpt\", logger=\"nyxgpt.self_heal\"}"
    }
  ]
}
```

**Response Fields:**
- `enabled` - Whether `[log_aggregation] enabled = true` in config.ini
- `grafana_explore_url` - URL of the Grafana Explore view for searching logs
- `active` - Mirrors `enabled` (the Loki/promtail stack itself runs outside this process, as Docker Compose services)
- `curated_queries` - Saved LogQL queries provisioned as code, mirroring the per-component panels in the Operational Logs dashboard (self-heal, deploy, canary, chat errors, RAG pipeline) -- each has a `label`, a `hint`, and the raw `query` text to paste into Grafana Explore

**How it works:**

Log aggregation is Loki/promtail-based, opt-in, and strictly local — there
is no external/cloud log service, and it's a reduced footprint compared to
a full ELK stack. Promtail tails this API's log files under
`~/.nyxGPT/logs` and ships them into Loki, which applies a retention
policy (14 days by default) and is searched via a Grafana Logs Explorer
dashboard, pre-provisioned alongside the Prometheus dashboards from
[Monitoring Dashboards](#monitoring-dashboards).

**Enabling log aggregation:**

```ini
[log_aggregation]
enabled = true
grafana_explore_url = http://localhost:3001/explore
```

The local Loki + promtail instances (`logging` Compose profile), alongside
the `monitoring` profile for Grafana, already start automatically with
`nyxgpt ops install`. To start them on their own or re-run them later
(never a raw `docker compose` command):

```bash
nyxgpt ops observability
```

Search logs at `http://localhost:3001/explore` (also linked from the
SRE/admin dashboard's Resource Usage step — see
[`docs/docker-compose.md`](docker-compose.md#log-aggregation)).

---

## Error handling

- All errors return JSON
- HTTP status codes are used consistently
- Internal errors are logged to `~/.nyxGPT/logs/nyxgpt.log`

---

## MCP Server

nyxGPT ships a minimal [Model Context Protocol](https://modelcontextprotocol.io) (MCP)
server that exposes nyxGPT as a tool provider over **stdio**. MCP-compatible clients
(e.g. Claude Desktop) can connect to it directly.

### Starting the server

```bash
nyxgpt mcp
```

The server reads JSON-RPC 2.0 requests from stdin and writes responses to stdout.
Protocol version: `2024-11-05`.

### Exposed tools

| Tool | Description |
|------|-------------|
| `chat` | Send a message to nyxGPT and receive a reply. Supports `prompt`, `session`, and `model` arguments. |
| `list_sessions` | List all available chat sessions. |

### Claude Desktop integration

Add to `~/Library/Application\ Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nyxgpt": {
      "command": "nyxgpt",
      "args": ["mcp"]
    }
  }
}
```

---

## Notes

- The API is **not intended to be exposed publicly**.
- HTTPS termination is expected to be handled externally if ever needed.
- Streaming responses are supported via `/api/v1/chat/stream`.
