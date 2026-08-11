# Troubleshooting Guide

This guide covers common issues and their solutions when running nyxGPT.

---

## Quick Health Check

Before troubleshooting, run the built-in health check:

```bash
nyxgpt ops doctor
```

This command verifies:
- Ollama connection and available models
- Cassandra connection (if RAG is enabled)
- Configuration file validity
- Sessions directory access
- Log directory access

---

## Connection Issues

### Ollama Connection Failures

**Symptoms:**
- `ConnectionError: Cannot connect to Ollama at http://127.0.0.1:11434`
- Chat requests hang or timeout
- API returns 500 errors with "Ollama connection failed"

**Solutions:**

1. **Verify Ollama is running:**
   ```bash
   curl http://127.0.0.1:11434/api/tags
   ```

   If this fails, start Ollama:
   ```bash
   # macOS
   open -a Ollama

   # Linux
   systemctl start ollama

   # Or use ops command
   nyxgpt ops install  # Installs and starts Ollama
   ```

2. **Check Ollama base URL in config:**
   ```ini
   [ollama]
   base_url = http://127.0.0.1:11434
   ```

   If Ollama is running on a different host/port, update this setting.

3. **Verify model availability:**
   ```bash
   ollama list
   ```

   If your configured model is missing:
   ```bash
   ollama pull llama3.1:8b  # Or your preferred model
   ```

4. **Check network/firewall:**
   - Ensure localhost connections are allowed
   - Verify no firewall blocking port 11434
   - Test with: `telnet 127.0.0.1 11434`

### Cassandra Connection Failures

**Symptoms:**
- `NoHostAvailable: Unable to connect to Cassandra`
- RAG operations fail with 500/503 errors
- Warning logs: "Cassandra connection failed"

**Solutions:**

1. **Verify Cassandra is running:**
   ```bash
   docker ps | grep cassandra
   ```

   If not running:
   ```bash
   nyxgpt ops install  # Installs and starts Cassandra
   ```

2. **Check connection settings:**
   ```ini
   [rag]
   cassandra_host = 127.0.0.1
   cassandra_port = 9042
   keyspace = nyxgpt
   ```

3. **Verify keyspace and table exist:**
   ```bash
   docker exec -it nyxgpt-cassandra cqlsh -e "DESCRIBE KEYSPACE nyxgpt;"
   ```

   If missing, create schema:
   ```bash
   nyxgpt rag init  # Creates keyspace and table
   ```

4. **Check Cassandra logs:**
   ```bash
   docker logs nyxgpt-cassandra
   ```

5. **Disable RAG if not needed:**
   ```ini
   [rag]
   enabled = false
   ```

---

## Installation / Environment Issues

### `ModuleNotFoundError` After a Pull

**Symptoms:**
- A `nyxgpt` command fails with a raw traceback like
  `ModuleNotFoundError: No module named 'opentelemetry.instrumentation.urllib'`
  right after `git pull`.

**Cause:**

A new (or bumped) dependency was added to `pyproject.toml`, but the local
venv was never refreshed to install it.

**Solutions:**

1. **Refresh the venv:**
   ```bash
   pip install -e .
   ```

2. **Confirm the fix:**
   ```bash
   nyxgpt ops doctor
   ```
   `doctor` checks the installed environment against every dependency
   declared in `pyproject.toml` and names exactly which package(s) are
   missing.

Tracing, error tracking, and metrics collection are all designed to degrade
gracefully when their integration package is missing -- the affected feature
disables itself with a single warning instead of crashing every command --
but `pip install -e .` is still the fix to actually use the feature again.

---

## Configuration Problems

### Invalid Configuration Values

**Symptoms:**
- Warning logs: "Invalid [section].[key] in config, using default"
- Application uses defaults instead of your settings
- Validation errors on startup

**Solutions:**

1. **Check configuration file location:**
   ```bash
   ls -la ~/.nyxGPT/config.ini
   ```

   If missing, create from example:
   ```bash
   cp example.config.ini ~/.nyxGPT/config.ini
   ```

2. **Validate configuration syntax:**
   - INI format requires `[sections]` and `key = value`
   - No quotes needed for string values
   - Comments start with `#` or `;`

   **Valid:**
   ```ini
   [nyxgpt]
   default_model = llama3.1:8b
   sessions_dir = ~/.nyxGPT/sessions
   ```

   **Invalid:**
   ```ini
   [nyxgpt]
   default_model: "llama3.1:8b"  # Wrong: uses colon, has quotes
   sessions dir = ~/.nyxGPT/sessions  # Wrong: space in key name
   ```

3. **Check for type mismatches:**
   - Port numbers must be integers: `port = 8000` (not `port = "8000"` or `port = invalid`)
   - Booleans: `enabled = true` or `enabled = false`
   - Paths: Support `~` expansion

4. **Review logs for specific validation errors:**
   ```bash
   grep WARNING ~/.nyxGPT/logs/api.log | grep config
   ```

5. **Verify file permissions:**
   ```bash
   chmod 600 ~/.nyxGPT/config.ini
   ```

### Hot-Reloadable Settings Not Taking Effect

**Hot-reloadable settings:**
- `nyxgpt.default_model`
- `logging.level`
- `rag.enabled`
- `auth.enabled`, `auth.api_key`, `auth.header`

**If changes don't take effect:**

1. Wait 1-2 seconds for the next request (no restart needed)
2. Verify you edited `~/.nyxGPT/config.ini` (not `example.config.ini`)
3. Check logs for configuration reload messages
4. Ensure no syntax errors in the config section

**Non-hot-reloadable settings require restart:**
- `api.host`, `api.port`
- `ollama.base_url`
- `rag.cassandra_*` connection settings
- `rag.embedding_model`, `rag.embedding_dim` (requires re-ingestion)

---

## Performance Issues

### Slow Chat Responses

**Symptoms:**
- Chat requests take > 30 seconds
- Timeouts on larger prompts
- High CPU usage

**Solutions:**

1. **Check model size:**
   - Larger models (70B, 405B) are slower
   - Try smaller models: `qwen2.5:0.5b`, `llama3.1:8b`

   Update config:
   ```ini
   [nyxgpt]
   default_model = qwen2.5:0.5b
   ```

2. **Increase timeout:**
   ```ini
   [nyxgpt]
   chat_timeout_seconds = 180  # Default is 60
   ```

3. **Check system resources:**
   ```bash
   # Monitor during chat
   top -pid $(pgrep -f ollama)
   ```

   - Ensure sufficient RAM (8GB minimum for 8B models)
   - Check CPU isn't throttling
   - Verify no other heavy processes running

4. **Disable RAG if not needed:**
   ```ini
   [rag]
   enabled = false
   ```

5. **Use GPU acceleration (if available):**
   - Ensure Ollama has GPU access
   - Verify with: `ollama run llama3.1:8b` (should show GPU usage)

### High Memory Usage

**Symptoms:**
- System running out of RAM
- Swap usage increases
- Application crashes with OOM errors

**Solutions:**

1. **Use smaller models:**
   - `qwen2.5:0.5b` (500MB RAM)
   - `llama3.1:8b` (4-6GB RAM)
   - Avoid 70B+ models unless you have 64GB+ RAM

2. **Limit session history:**
   - Longer conversations consume more context
   - Start new sessions periodically: `nyxgpt chat --new`

3. **Check for session accumulation:**
   ```bash
   ls -lh ~/.nyxGPT/sessions/
   ```

   Delete old sessions:
   ```bash
   nyxgpt sessions delete <session-name>
   ```

4. **Monitor Cassandra memory (if using RAG):**
   ```bash
   docker stats nyxgpt-cassandra
   ```

### API Response Delays

**Symptoms:**
- Web UI slow to load
- API latency > 1 second
- Timeouts on health checks

**Solutions:**

1. **Check API is running:**
   ```bash
   curl http://127.0.0.1:8000/health
   ```

2. **Verify no port conflicts:**
   ```bash
   lsof -i :8000
   ```

   If another process is using port 8000:
   ```ini
   [api]
   port = 8001  # Use different port
   ```

3. **Review API logs:**
   ```bash
   grep ERROR ~/.nyxGPT/logs/api.log
   ```

4. **Increase worker processes (production):**
   ```bash
   uvicorn nyxgpt.app:app --workers 4
   ```

---

## RAG Retrieval Problems

### No Results from RAG Queries

**Symptoms:**
- `nyxgpt rag query "search term"` returns empty results
- Chat doesn't use RAG context even when enabled
- Logs show "Retrieved 0 chunks"

**Solutions:**

1. **Verify RAG is enabled:**
   ```ini
   [rag]
   enabled = true
   ```

2. **Check if documents are ingested:**
   ```bash
   docker exec -it nyxgpt-cassandra cqlsh -e "SELECT COUNT(*) FROM nyxgpt.rag_chunks;"
   ```

   If count is 0, ingest documents:
   ```bash
   nyxgpt rag ingest --file documents.txt --doc-id my-doc
   ```

3. **Verify embedding model is available:**
   ```bash
   nyxgpt models list | grep nomic-embed-text
   ```

   If missing:
   ```bash
   nyxgpt models pull nomic-embed-text
   ```

   nyxGPT normally pulls it for you on the first ingest (`[rag]
   embedding_auto_pull = true`). Pull it by hand when that setting is
   disabled, or when the auto-pull failed and ingestion reported
   `Embedding model '...' is not installed in Ollama`.

4. **Check similarity threshold:**
   - RAG filters results by similarity score
   - Very specific queries may not match ingested content
   - Try broader queries

5. **Review RAG configuration:**
   ```ini
   [rag]
   chat_top_k = 3  # Increase to retrieve more chunks
   chat_context_max_chars = 4000  # Increase max context
   ```

### RAG Returns Irrelevant Results

**Symptoms:**
- Retrieved chunks don't match query intent
- Low similarity scores in results
- Chat responses ignore RAG context

**Solutions:**

1. **Improve document chunking:**
   - Use meaningful chunk boundaries
   - Keep chunks focused on single topics
   - Include context in each chunk

2. **Verify embedding model matches schema:**
   ```ini
   [rag]
   embedding_model = nomic-embed-text
   embedding_dim = 768  # Must match table schema
   ```

   If changed, re-create table and re-ingest:
   ```bash
   docker exec -it nyxgpt-cassandra cqlsh -e "DROP TABLE IF EXISTS nyxgpt.rag_chunks;"
   nyxgpt rag init
   nyxgpt rag ingest --file documents.txt --doc-id my-doc
   ```

3. **Inspect retrieved results:**
   ```bash
   nyxgpt rag query "test query" -k 5
   ```

   Check similarity scores - should be > 0.5 for relevant results

4. **Re-ingest with better source documents:**
   - Use high-quality, well-structured content
   - Avoid noisy or unrelated data

### Cassandra Protocol Version Warnings

**Symptoms:**
- Logs show: `Cluster detected server version 5.0, but ProtocolVersion.DSE_V1 requires 6.0`

**Solutions:**

This is a **cosmetic warning** from the Cassandra driver and can be safely ignored. It occurs because the driver tries DSE-specific protocol versions before standard ones.

**To suppress (optional):**
1. Update Cassandra driver:
   ```bash
   pip install --upgrade cassandra-driver
   ```

2. Or filter logs:
   ```python
   import logging
   logging.getLogger('cassandra').setLevel(logging.ERROR)
   ```

---

## Log Analysis

### Understanding Log Levels

```ini
[logging]
level = DEBUG  # Most verbose
# level = INFO  # Normal operation
# level = WARNING  # Warnings and errors only
# level = ERROR  # Errors only
```

**When to use:**
- `DEBUG`: Troubleshooting, development
- `INFO`: Production, normal monitoring
- `WARNING`: Production, reduce noise
- `ERROR`: Production, critical issues only

### Locating Logs

**Default location:**
```bash
~/.nyxGPT/logs/api.log
```

**View recent logs:**
```bash
tail -f ~/.nyxGPT/logs/api.log
```

**Search for errors:**
```bash
grep ERROR ~/.nyxGPT/logs/api.log
grep WARNING ~/.nyxGPT/logs/api.log
```

**Grafana logs show nothing (native mode):** the `logging` Compose
profile's promtail container needs its own bind mount to see
`~/.nyxGPT/logs` -- it's not the same thing as `~/.nyxGPT/volumes/nyxgpt-data`,
the host directory Compose mode's `api` container uses (see
[docker-compose.md#log-aggregation](docker-compose.md#log-aggregation)).
Run `nyxgpt ops doctor` -- it flags a missing/regressed bind mount here
rather than leaving you with a dashboard that just silently shows no
results.

### Common Log Patterns

**Request ID tracking:**
All requests have a unique ID for tracing:
```
2026-01-03 10:15:30 INFO nyxgpt.app: Chat request received [request_id=abc123]
2026-01-03 10:15:32 INFO nyxgpt.chat: Starting chat stream [request_id=abc123]
2026-01-03 10:15:35 INFO nyxgpt.app: Chat completed [request_id=abc123]
```

**Configuration reloads:**
```
2026-01-03 10:20:00 INFO nyxgpt.config: Configuration reloaded
```

**Service health checks:**
```
2026-01-03 10:00:00 INFO nyxgpt.app: Health check passed
```

### Error Signatures

**Connection errors:**
```
ERROR nyxgpt.ollama_client: Connection refused to http://127.0.0.1:11434
→ Solution: Start Ollama service
```

**Validation errors:**
```
WARNING nyxgpt.sessions: Invalid session name: ../etc/passwd
→ Solution: Use valid session names (alphanumeric, dash, underscore only)
```

**Timeout errors:**
```
ERROR nyxgpt.chat: Chat timeout after 60 seconds
→ Solution: Increase chat_timeout_seconds or use smaller model
```

**Model-runtime crashes (chat request 500s):**
```
ERROR nyxgpt.api: Chat request failed: model runtime error
Traceback (most recent call last):
  ...
nyxgpt.ollama_client.ModelRuntimeError: Model failed to run — the model
runtime returned an error. This can happen if the host doesn't have enough
free memory to load the model, but may also be a transient failure (Ollama
HTTP 500: {"error": "model requires more system memory (5.4 GiB) than is
available (3.1 GiB)"})
→ Solution: The chat request itself now returns this same actionable message
  (502, instead of a bare 500) and the web UI shows it inline instead of just
  logging it. Pick a smaller model tag (see performance.md#approximate-memory-by-model-tag)
  or free up memory on the host. If the detail doesn't mention memory, run
  `nyxgpt ops status` to check whether Ollama is still up, and check Ollama's
  own logs (via the Grafana/Loki logs view if log aggregation is enabled --
  see docker-compose.md#log-aggregation) for why the model runtime crashed.
```

**Model-runtime timeouts (slow cold load):**
```
ERROR nyxgpt.chat: Ollama chat stream failed
nyxgpt.ollama_client.ModelRuntimeError: Model failed to run — it may require
more memory than is available on this host (no response within 180s)
→ Solution: A large model can take a while to load into memory on its first
  request. If this happens consistently, increase [nyxgpt] chat_timeout_seconds
  in config.ini, or switch to a smaller model tag.
```

**Authentication errors:**
```
WARNING nyxgpt.app: Invalid API key from 192.168.1.100
→ Solution: Verify API key in request header
```

### Debugging with Request IDs

1. **Find failing request ID:**
   ```bash
   grep ERROR ~/.nyxGPT/logs/api.log | grep "request_id"
   ```

2. **Trace full request lifecycle:**
   ```bash
   grep "request_id=abc123" ~/.nyxGPT/logs/api.log
   ```

3. **Analyze timing:**
   ```bash
   grep "request_id=abc123" ~/.nyxGPT/logs/api.log | awk '{print $1, $2, $NF}'
   ```

---

## Session Management Issues

### Cannot Load Session

**Symptoms:**
- `FileNotFoundError: Session not found`
- API returns 404 for session

**Solutions:**

1. **List available sessions:**
   ```bash
   nyxgpt sessions list
   ```

2. **Verify session file exists:**
   ```bash
   ls ~/.nyxGPT/sessions/<session-name>.json
   ls ~/.nyxGPT/sessions/<session-name>.meta.json
   ```

3. **Check file permissions:**
   ```bash
   ls -la ~/.nyxGPT/sessions/
   ```

   Fix if needed:
   ```bash
   chmod 644 ~/.nyxGPT/sessions/*.json
   ```

4. **Inspect session file for corruption:**
   ```bash
   cat ~/.nyxGPT/sessions/<session-name>.json | jq .
   ```

   If invalid JSON, restore from backup or delete:
   ```bash
   rm ~/.nyxGPT/sessions/<session-name>*.json
   ```

### Session Name Validation Errors

**Symptoms:**
- `ValueError: Invalid session name`
- API returns 422 with "Invalid session name"

**Valid session names:**
- 1-64 characters
- Alphanumeric, dash, underscore only
- No path separators (`.`, `/`, `\`)

**Examples:**
```
✓ Valid: session1, my-session, test_123
✗ Invalid: ../session, session.txt, my session (space), a*1000 (too long)
```

### Concurrent Session Access

**Symptoms:**
- Intermittent read/write errors
- Sessions occasionally reset

**Solutions:**

nyxGPT uses atomic writes to prevent corruption during concurrent access, but:

1. **Avoid concurrent writes to same session:**
   - Use unique session names per client/user
   - Don't run multiple chat instances on same session simultaneously

2. **If corruption occurs, delete and restart:**
   ```bash
   nyxgpt sessions delete <session-name>
   nyxgpt chat --session <session-name> --new "Hello"
   ```

---

## API-Specific Issues

### 422 Unprocessable Entity

**Common causes:**

1. **Missing required fields:**
   ```json
   {"error": "Field required: prompt"}
   ```

   Fix: Include all required fields in request

2. **Invalid data types:**
   ```json
   {"prompt": 123}  // Should be string
   ```

3. **Validation failures:**
   - Session name too long
   - Invalid model name format
   - top_k out of range

**Solution:** Review API documentation and match request schema exactly.

### 500 Internal Server Error

**Indicates server-side failure:**

1. **Check API logs:**
   ```bash
   grep ERROR ~/.nyxGPT/logs/api.log | tail -20
   ```

2. **Common causes:**
   - Ollama connection failed
   - Cassandra unavailable (if RAG enabled)
   - Session file corruption
   - Configuration errors

3. **Look for request_id in response:**
   ```json
   {"error": {"message": "Internal server error", "request_id": "abc123"}}
   ```

   Then search logs:
   ```bash
   grep "abc123" ~/.nyxGPT/logs/api.log
   ```

### 502 Bad Gateway (chat)

`/api/v1/chat` and `/api/v1/chat/stream` return `502` instead of a bare `500`
when the *model runtime itself* fails -- the response `detail` (or the SSE
`error` event's `error` field) already contains the actionable message, e.g.:

```json
{"detail": "Model failed to run — the model runtime returned an error. This can happen if the host doesn't have enough free memory to load the model, but may also be a transient failure (Ollama HTTP 500: ...)"}
```

No log-diving is required for this case; see
[performance.md#approximate-memory-by-model-tag](performance.md#approximate-memory-by-model-tag)
for RAM guidance per model tag, or
[Model-runtime crashes](#log-analysis) above for the log signature.

### CORS Issues (Web UI)

**Symptoms:**
- Browser console: "CORS policy blocked"
- Web UI can't connect to API

**Solutions:**

1. **Verify API is running:**
   ```bash
   curl http://127.0.0.1:8000/health
   ```

2. **Check CORS configuration in app.py:**
   - Should allow `http://localhost:3000` for development
   - Add your domain for production

3. **Use correct API URL in web UI:**
   - Development: `http://localhost:8000`
   - Production: Update API_BASE_URL in web config

---

## Getting Help

If you're still stuck after following this guide:

1. **Check existing issues:**
   ```bash
   gh issue list --repo dkblinux98/nyxGPT
   ```

2. **Gather diagnostic info:**
   ```bash
   nyxgpt ops doctor > diagnostic.txt
   tail -100 ~/.nyxGPT/logs/api.log >> diagnostic.txt
   ```

3. **Create an issue:**
   - Include diagnostic output
   - Describe expected vs actual behavior
   - Include error messages and request IDs
   - Specify nyxGPT version: `nyxgpt --version`

4. **Check documentation:**
   - [Configuration Guide](configuration.md)
   - [API Documentation](api.md)
   - [RAG Guide](rag.md)
   - [Architecture](architecture.md)
