

# Homebrew Services

nyxGPT provides two persistent background services using **Homebrew services**:

1. **nyxgpt-api** - FastAPI backend (REST API)
2. **nyxgpt-web** - Next.js web UI

This is the recommended way to keep both services running locally without keeping terminals open.

---

## Prerequisites

- macOS
- Homebrew installed
- Python environment already set up for nyxGPT

---

## Homebrew tap

The nyxGPT Homebrew formula lives in a custom tap:

```
dkblinux98/nyxgpt-local
```

Add the tap:

```bash
brew tap dkblinux98/nyxgpt-local
```

---

## Installing the services

Install both service formulas:

```bash
# Add the tap (if not already added)
brew tap dkblinux98/nyxgpt-local

# Install both services
brew install nyxgpt-api
brew install nyxgpt-web
```

Each service installs:
- A small wrapper script
- A Homebrew launch agent plist

---

## Managing the API service (nyxgpt-api)

### Start the API

Start the FastAPI backend as a background service:

```bash
brew services start nyxgpt-api
```

Verify status:

```bash
brew services info nyxgpt-api
```

### Restart and stop

Restart the API service:

```bash
brew services restart nyxgpt-api
```

Stop the API service:

```bash
brew services stop nyxgpt-api
```

### API logs

Service logs are written to:

```
~/.nyxGPT/logs/nyxgpt.log
```

Tail logs in real time:

```bash
tail -f ~/.nyxGPT/logs/nyxgpt.log
```

If the service fails to start, check these logs first.

---

## Managing the Web UI service (nyxgpt-web)

### Start the Web UI

Start the Next.js web UI as a background service:

```bash
brew services start nyxgpt-web
```

Verify status:

```bash
brew services info nyxgpt-web
```

The web UI will be available at: `http://127.0.0.1:3000`

### Restart and stop

Restart the web service:

```bash
brew services restart nyxgpt-web
```

Stop the web service:

```bash
brew services stop nyxgpt-web
```

### Web UI logs

Service logs are written to:

```
~/.nyxGPT/logs/nyxgpt-web.log
~/.nyxGPT/logs/nyxgpt-web.err.log
```

Tail logs in real time:

```bash
# Standard output logs
tail -f ~/.nyxGPT/logs/nyxgpt-web.log

# Error logs
tail -f ~/.nyxGPT/logs/nyxgpt-web.err.log
```

---

## Managing both services together

### Start both services

```bash
brew services start nyxgpt-api
brew services start nyxgpt-web
```

Or use the `nyxgpt ops restart` command for a coordinated restart:

```bash
nyxgpt ops restart
```

### Stop both services

```bash
brew services stop nyxgpt-api
brew services stop nyxgpt-web
```

### Check status of all services

```bash
brew services list | grep nyxgpt
```

Example output:

```
nyxgpt-api  started username ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api.plist
nyxgpt-web  started username ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-web.plist
```

---

## Service dependencies

**Important:** The Web UI depends on the API service.

- **nyxgpt-api** must be running for the Web UI to function
- Start the API before starting the Web UI
- If the API is down, the Web UI will show connection errors

Recommended startup order:

```bash
brew services start nyxgpt-api
# Wait a few seconds for API to be ready
brew services start nyxgpt-web
```

---

## Configuration

Both Homebrew services use the same configuration file as the CLI:

```
~/.nyxGPT/config.ini
```

### API configuration

API settings are in the `[api]` section:

```ini
[api]
host = 127.0.0.1
port = 8000
```

### Web UI configuration

Web UI settings are in the `[web]` section:

```ini
[web]
host = 127.0.0.1
port = 3000
api_base_url =  # Optional: override API URL
```

### Applying configuration changes

Some settings are hot-reloadable (take effect immediately):
- `default_model`
- `rag.enable_chat_context`
- `logging.level`
- `auth.enabled` and `auth.api_key`

Other changes require service restart:

```bash
# Restart API service
brew services restart nyxgpt-api

# Restart Web UI service
brew services restart nyxgpt-web

# Restart both
nyxgpt ops restart
```

---

## Accessing the services

After starting both services:

- **API**: `http://127.0.0.1:8000`
- **Web UI**: `http://127.0.0.1:3000`
- **API docs**: `http://127.0.0.1:8000/docs`

---

## Troubleshooting

### Web UI can't connect to API

**Symptom**: Web UI shows "Connection Error" or "API Unavailable"

**Solutions**:

1. Verify API is running:
   ```bash
   brew services list | grep nyxgpt-api
   curl http://127.0.0.1:8000/health
   ```

2. Check API logs:
   ```bash
   tail -f ~/.nyxGPT/logs/nyxgpt.log
   ```

3. Restart API service:
   ```bash
   brew services restart nyxgpt-api
   ```

### Service won't start

**Symptom**: `brew services start` succeeds but service isn't running

**Solutions**:

1. Check logs for errors:
   ```bash
   tail -f ~/.nyxGPT/logs/nyxgpt.log
   tail -f ~/.nyxGPT/logs/nyxgpt-web.err.log
   ```

2. Verify configuration is valid:
   ```bash
   cat ~/.nyxGPT/config.ini
   ```

3. Check port conflicts:
   ```bash
   lsof -i :8000  # API port
   lsof -i :3000  # Web UI port
   ```

4. Run `nyxgpt ops doctor` for health checks:
   ```bash
   nyxgpt ops doctor
   ```

### Node.js not found (Web UI)

**Symptom**: Web UI logs show "node: command not found"

**Solutions**:

1. Verify Node.js is installed:
   ```bash
   which node
   which npm
   ```

2. Update `[paths]` in config:
   ```ini
   [paths]
   node_bin = /opt/homebrew/bin/node
   npm_bin = /opt/homebrew/bin/npm
   ```

3. Restart web service:
   ```bash
   brew services restart nyxgpt-web
   ```

---

## Notes

- Both services run under your user account (not as root)
- The API is bound to `127.0.0.1` by default and is not exposed publicly
- The Web UI is also bound to `127.0.0.1` for local-only access
- Homebrew services automatically restart both services on login
- Use `nyxgpt ops` commands for easier service management
