# FastAPI Backend

myGPT provides a local FastAPI backend that exposes chat, RAG, session, and health endpoints. The API is intended for:

- the CLI (primary consumer today)
- future local TUI and web UI
- debugging and integration testing

The API is designed to run **locally only** by default.

---

## Running the API

### Development (manual)

```bash
mygpt api
```

or, if running directly:

```bash
uvicorn mygpt.app:app --reload
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

Configuration lives in `~/.myGPT/config.ini` under the `[api]` section.

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
2026-01-03 12:34:56 INFO [550e8400-e29b-41d4-a716-446655440000] mygpt.api: Chat request received
2026-01-03 12:34:57 INFO [550e8400-e29b-41d4-a716-446655440000] mygpt.api: Chat request completed
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
  "sessions_dir": "/Users/you/.myGPT/sessions"
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

**Request:**

```json
{
  "model": "llama3.1:8b"
}
```

**Response:**

```json
{
  "ok": true,
  "model": "llama3.1:8b",
  "result": { "status": "success" }
}
```

**Notes:**
- Downloads can take several minutes for large models
- Non-streaming pull (no progress updates)
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

## Sessions endpoints

Sessions store conversation history and metadata and are persisted automatically on disk.

### `GET /api/v1/sessions`

List all known sessions.

**Response:**

```json
{
  "sessions": [
    {
      "name": "default",
      "messages": 14,
      "modified": "2025-12-23 23:35:32",
      "pinned": false,
      "tags": [],
      "title": "",
      "summary": "",
      "token_estimate": 228,
      "model": "llama3.1:8b"
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

### RAG prompt & context optimization

When RAG-assisted chat is enabled, retrieved context injected into the prompt is governed by configuration settings in `~/.myGPT/config.ini` under the `[rag]` section.

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

Stream a chat response token-by-token as plain text.

This endpoint is functionally equivalent to `/api/v1/chat` but returns the assistant response incrementally as it is generated, providing a much better user experience for interactive clients such as a TUI or web UI.

**Request:**

```json
{
  "prompt": "Write a haiku about streaming",
  "session": "default",
  "model": "llama3.1:8b"
}
```

**Response:**

- HTTP 200
- `Content-Type: text/plain; charset=utf-8`
- Body is streamed incrementally as text chunks

Example using `curl`:

```bash
curl -N http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Write a haiku about streaming","session":"default"}'
```

**RAG metadata in streaming responses:**

When RAG is enabled, the first chunk contains RAG metadata in the format:

```
__RAG_START__{"type":"rag_metadata","chunks":[{"text":"...","score":0.95,"doc_id":"doc123","chunk_id":5}]}__RAG_END__
```

Clients should:
1. Parse and extract RAG metadata from the first chunk
2. Remove the `__RAG_START__`...`__RAG_END__` section from displayed content
3. Display RAG citations separately from the streamed response text

Notes:

- The connection remains open until generation completes
- Retrieved RAG context (if enabled) is injected *before* streaming begins
- RAG metadata is emitted as the first chunk when RAG is enabled
- The full response is persisted to the session once streaming completes

---

## RAG endpoints

RAG-related endpoints are documented in detail in:

➡️ [`docs/rag.md`](rag.md)

At a high level, the API supports:

- document ingestion
- vector search / retrieval
- RAG-assisted chat

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
      "name": "mygpt.log",
      "path": "/home/user/.myGPT/logs/mygpt.log",
      "size": 1024000,
      "modified": 1704067200.0
    }
  ],
  "log_dir": "/home/user/.myGPT/logs"
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
curl "http://127.0.0.1:8000/api/v1/logs/view/mygpt.log?tail=100"

# Filter by log level
curl "http://127.0.0.1:8000/api/v1/logs/view/mygpt.log?level=ERROR"

# Search for specific text
curl "http://127.0.0.1:8000/api/v1/logs/view/mygpt.log?search=session"

# Combine filters
curl "http://127.0.0.1:8000/api/v1/logs/view/mygpt.log?tail=50&level=INFO&search=chat"
```

**Response:**

```json
{
  "filename": "mygpt.log",
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
curl "http://127.0.0.1:8000/api/v1/logs/stream/mygpt.log"

# Stream only ERROR logs
curl "http://127.0.0.1:8000/api/v1/logs/stream/mygpt.log?level=ERROR"
```

**Response:** Text stream (Content-Type: text/plain)

**Security Notes:**

- File paths are sanitized to prevent path traversal attacks
- Access is restricted to files within the configured log directory
- Attempting to access files outside the log directory returns 403 Forbidden

---

## Authentication

myGPT API supports optional API key authentication. Authentication is **disabled by default** for local-only usage and can be enabled via configuration when additional security is needed.

### Overview

When authentication is enabled:

- All `/api/v1/*` endpoints require a valid API key
- Health check (`/health`) and documentation endpoints (`/docs`, `/openapi.json`, `/redoc`) remain publicly accessible
- Invalid or missing API keys return `401 Unauthorized` with a request ID for debugging
- API keys are compared using constant-time comparison to prevent timing attacks

### Configuration

Authentication is configured in `~/.myGPT/config.ini` under the `[auth]` section.

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

Authentication configuration is hot-reloaded on every request. Changes to `~/.myGPT/config.ini` take effect immediately without restarting the API:

```bash
# 1. Edit config to enable auth
vim ~/.myGPT/config.ini

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

**Implementation** (from `src/mygpt/app.py:349`):

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
grep "550e8400-e29b-41d4-a716-446655440000" ~/.myGPT/logs/mygpt.log
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
   - Never commit `~/.myGPT/config.ini` to version control
   - Restrict file permissions: `chmod 600 ~/.myGPT/config.ini`
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

myGPT is designed for **local, single-user use**. For production or multi-user deployments, consider:

1. **HTTPS/TLS termination** via reverse proxy (nginx, caddy)
2. **Per-user authentication** instead of shared API keys
3. **OAuth2 or JWT** for more sophisticated auth
4. **Database-backed session management**
5. **Comprehensive audit logging**
6. **DDoS protection and rate limiting**

These features are beyond the scope of myGPT's current design but can be layered on top using standard infrastructure tools.

### Troubleshooting

#### Authentication Not Working

**Symptom**: Requests still work without API key despite `enabled = true`

**Solutions**:

1. Verify config file location:
   ```bash
   cat ~/.myGPT/config.ini
   ```

2. Check for syntax errors in config:
   ```bash
   python3 -c "from configparser import ConfigParser; c = ConfigParser(); c.read('$HOME/.myGPT/config.ini'); print(c.getboolean('auth', 'enabled'))"
   ```

3. Check API logs for authentication status:
   ```bash
   tail -f ~/.myGPT/logs/mygpt.log | grep auth
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
   grep "^header" ~/.myGPT/config.ini
   ```

3. Verify API key matches config exactly (no extra spaces):
   ```bash
   grep "^api_key" ~/.myGPT/config.ini
   ```

4. Temporarily disable auth to verify API is working:
   ```bash
   # Edit config
   vim ~/.myGPT/config.ini
   # Set: enabled = false
   # Test
   curl http://127.0.0.1:8000/api/v1/info
   ```

#### Web UI Authentication

The Next.js web UI reads the same `~/.myGPT/config.ini` file and automatically includes the API key in requests to the FastAPI backend. No additional configuration is needed.

**Verification**:

```bash
# Check web UI proxy configuration
grep -A 3 "\[auth\]" ~/.myGPT/config.ini
```

The web UI will automatically detect when authentication is enabled and include the configured API key in all backend requests.

---

## Operational Tasks

These tasks cover keeping myGPT services and supporting infrastructure running reliably across reboots.

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
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' mygpt-cassandra
```

Expected output:

```
unless-stopped
```
```bash
# verify Cassandra data is on a named volume (persistence)
docker inspect mygpt-cassandra \
  --format '{{ range .Mounts }}{{ .Name }} -> {{ .Destination }}{{ println }}{{ end }}'
```

Expected output should include something like:

```
mygpt_cassandra_data -> /var/lib/cassandra
```

If not set correctly, recreate the container:

```bash
docker rm -f mygpt-cassandra

# create the named volume (safe if it already exists)
docker volume create mygpt_cassandra_data

docker run -d \
  --name mygpt-cassandra \
  --restart unless-stopped \
  -p 9042:9042 \
  -e CASSANDRA_CLUSTER_NAME=mygpt \
  -v mygpt_cassandra_data:/var/lib/cassandra \
  cassandra:5.0
```


Cassandra can take a minute to become ready after a fresh start. If `cqlsh` is installed on your Mac, you can verify from the host (no `docker exec` needed):

```bash
cqlsh 127.0.0.1 9042 -e "DESCRIBE KEYSPACES;"
```

---

### Centralized logs

myGPT consolidates logs under:

```
~/.myGPT/logs
```

This includes:

- myGPT application logs
- test logs
- streamed Cassandra logs
- Ollama logs (symlinked)

---

### Cassandra logs via Docker (LaunchAgent)

Cassandra logs are streamed from Docker into `~/.myGPT/logs` using a macOS LaunchAgent.

Installed files (tracked in the repo):

- `follow-cassandra-logs.sh`
- `com.mygpt.cassandra-logs.plist`

Install and activate:

```bash
mygpt ops install
# optional:
# mygpt ops install --repo-dir /path/to/myGPT
# mygpt ops install --force
```

Verify the agent is loaded:

```bash
launchctl list | grep com.mygpt.cassandra-logs
```

Verify logs:

```bash
tail -f ~/.myGPT/logs/cassandra-logfollower.out.log
tail -f ~/.myGPT/logs/cassandra-logfollower.err.log
```

This survives reboots.

---

### mygpt ops commands

`mygpt` provides built-in operational checks so you don’t have to remember Docker, LaunchAgent, and API details by hand.

#### `mygpt ops install`
Installs the Cassandra log follower LaunchAgent and prepares local log directories.

```bash
mygpt ops install
# optional:
# mygpt ops install --repo-dir /path/to/myGPT
# mygpt ops install --force
```

This command is safe to re-run.

- Exit code `0` → all install steps succeeded
- Exit code `2` → one or more steps failed (details are printed)

#### `mygpt ops status`
Shows the current health of local dependencies and services:

- Docker daemon reachable
- Cassandra container running
- Restart policy (`unless-stopped`)
- Cassandra data mounted to `/var/lib/cassandra`
- Cassandra log LaunchAgent loaded
- Expected log files present under `~/.myGPT/logs`
- FastAPI `/health` endpoint reachable

```bash
mygpt ops status
```

This command always exits `0` and is informational.

#### `mygpt ops doctor`
Runs the same checks as `status` but **fails fast** if anything is broken.

```bash
mygpt ops doctor
echo "exit=$?"
```

- Exit code `0` → all required services are healthy
- Exit code `2` → one or more checks failed (details are printed)

---
#### `mygpt ops restart`

Restart one or more myGPT-managed services without calling `brew`, `docker`,
or `launchctl` directly.

This is the recommended way to apply configuration changes or recover from
transient failures.

**Usage:**

```bash
mygpt ops restart
```

### Ollama logs

Ollama is typically managed via Homebrew services and logs to a Homebrew-managed log file.

Common locations:

- Intel Homebrew: `/usr/local/var/log/ollama.log`
- Apple Silicon Homebrew: `/opt/homebrew/var/log/ollama.log`

You can symlink whichever exists into `~/.myGPT/logs`:

```bash
mkdir -p ~/.myGPT/logs
for p in /usr/local/var/log/ollama.log /opt/homebrew/var/log/ollama.log; do
  if [[ -f "$p" ]]; then
    ln -sf "$p" ~/.myGPT/logs/ollama.log
    echo "linked $p -> ~/.myGPT/logs/ollama.log"
    break
  fi
done
```

Verify:

```bash
tail -f ~/.myGPT/logs/ollama.log
```

If you don’t see output, confirm Ollama is running:

```bash
brew services info ollama
curl -s http://127.0.0.1:11434/api/tags | head
```

---

### Local Web UI service (Next.js)

The local web UI is a small Next.js application that connects to the FastAPI backend.

It is intended to run:
- locally only
- as a background service
- using the same configuration file as the rest of myGPT

Configuration is read from `~/.myGPT/config.ini` under the `[web]` and `[paths]` sections.

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

Note: Homebrew/launchd services run with a minimal `PATH`. The web wrapper (`mygpt-web`) and `~/.myGPT/scripts/run-web.sh` ensure `node` is discoverable (via `[paths] node_bin` / `npm_bin`) so `npm` can run reliably in the background.

---

### Web UI background service

The web UI is managed via Homebrew services, similar to the FastAPI backend.

Install the service:

```bash
mygpt ops install
```

Verify status:

```bash
brew services list | grep mygpt-web
```

Start manually if needed:

```bash
brew services start mygpt-web
```

Logs are written to:

```
~/.myGPT/logs/mygpt-web.log
~/.myGPT/logs/mygpt-web.err.log
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

Ollama logs are symlinked into the same directory:

```bash
ln -sf /usr/local/var/log/ollama.log ~/.myGPT/logs/ollama.log
```

Verify:

```bash
tail -f ~/.myGPT/logs/ollama.log
```

---

### API background service

The FastAPI backend is expected to run via Homebrew services.

Verify status:

```bash
brew services list | grep mygpt-api
```

Start if needed:

```bash
brew services start mygpt-api
```

---

### Health verification checklist

After reboot:

```bash
docker ps | grep mygpt-cassandra
brew services list | grep mygpt-api
curl http://127.0.0.1:8000/health
```

Expected:

```json
{ "status": "ok" }
```

---

## Error handling

- All errors return JSON
- HTTP status codes are used consistently
- Internal errors are logged to `~/.myGPT/logs/mygpt.log`

---

## Notes

- The API is **not intended to be exposed publicly**.
- HTTPS termination is expected to be handled externally if ever needed.
- Streaming responses are supported via `/api/v1/chat/stream`.
