

# Homebrew Services

myGPT provides two persistent background services using **Homebrew services**:

1. **mygpt-api** - FastAPI backend (REST API)
2. **mygpt-web** - Next.js web UI

This is the recommended way to keep both services running locally without keeping terminals open.

---

## Prerequisites

- macOS
- Homebrew installed
- Python environment already set up for myGPT

---

## Homebrew tap

The myGPT Homebrew formula lives in a custom tap:

```
dkblinux98/mygpt-local
```

Add the tap:

```bash
brew tap dkblinux98/mygpt-local
```

---

## Installing the services

Install both service formulas:

```bash
# Add the tap (if not already added)
brew tap dkblinux98/mygpt-local

# Install both services
brew install mygpt-api
brew install mygpt-web
```

Each service installs:
- A small wrapper script
- A Homebrew launch agent plist

---

## Managing the API service (mygpt-api)

### Start the API

Start the FastAPI backend as a background service:

```bash
brew services start mygpt-api
```

Verify status:

```bash
brew services info mygpt-api
```

### Restart and stop

Restart the API service:

```bash
brew services restart mygpt-api
```

Stop the API service:

```bash
brew services stop mygpt-api
```

### API logs

Service logs are written to:

```
~/.myGPT/logs/mygpt.log
```

Tail logs in real time:

```bash
tail -f ~/.myGPT/logs/mygpt.log
```

If the service fails to start, check these logs first.

---

## Managing the Web UI service (mygpt-web)

### Start the Web UI

Start the Next.js web UI as a background service:

```bash
brew services start mygpt-web
```

Verify status:

```bash
brew services info mygpt-web
```

The web UI will be available at: `http://127.0.0.1:3000`

### Restart and stop

Restart the web service:

```bash
brew services restart mygpt-web
```

Stop the web service:

```bash
brew services stop mygpt-web
```

### Web UI logs

Service logs are written to:

```
~/.myGPT/logs/mygpt-web.log
~/.myGPT/logs/mygpt-web.err.log
```

Tail logs in real time:

```bash
# Standard output logs
tail -f ~/.myGPT/logs/mygpt-web.log

# Error logs
tail -f ~/.myGPT/logs/mygpt-web.err.log
```

---

## Managing both services together

### Start both services

```bash
brew services start mygpt-api
brew services start mygpt-web
```

Or use the `mygpt ops restart` command for a coordinated restart:

```bash
mygpt ops restart
```

### Stop both services

```bash
brew services stop mygpt-api
brew services stop mygpt-web
```

### Check status of all services

```bash
brew services list | grep mygpt
```

Example output:

```
mygpt-api  started username ~/Library/LaunchAgents/homebrew.mxcl.mygpt-api.plist
mygpt-web  started username ~/Library/LaunchAgents/homebrew.mxcl.mygpt-web.plist
```

---

## Service dependencies

**Important:** The Web UI depends on the API service.

- **mygpt-api** must be running for the Web UI to function
- Start the API before starting the Web UI
- If the API is down, the Web UI will show connection errors

Recommended startup order:

```bash
brew services start mygpt-api
# Wait a few seconds for API to be ready
brew services start mygpt-web
```

---

## Configuration

Both Homebrew services use the same configuration file as the CLI:

```
~/.myGPT/config.ini
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
brew services restart mygpt-api

# Restart Web UI service
brew services restart mygpt-web

# Restart both
mygpt ops restart
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
   brew services list | grep mygpt-api
   curl http://127.0.0.1:8000/health
   ```

2. Check API logs:
   ```bash
   tail -f ~/.myGPT/logs/mygpt.log
   ```

3. Restart API service:
   ```bash
   brew services restart mygpt-api
   ```

### Service won't start

**Symptom**: `brew services start` succeeds but service isn't running

**Solutions**:

1. Check logs for errors:
   ```bash
   tail -f ~/.myGPT/logs/mygpt.log
   tail -f ~/.myGPT/logs/mygpt-web.err.log
   ```

2. Verify configuration is valid:
   ```bash
   cat ~/.myGPT/config.ini
   ```

3. Check port conflicts:
   ```bash
   lsof -i :8000  # API port
   lsof -i :3000  # Web UI port
   ```

4. Run `mygpt ops doctor` for health checks:
   ```bash
   mygpt ops doctor
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
   node_bin = /usr/local/bin/node
   npm_bin = /usr/local/bin/npm
   ```

3. Restart web service:
   ```bash
   brew services restart mygpt-web
   ```

---

## Notes

- Both services run under your user account (not as root)
- The API is bound to `127.0.0.1` by default and is not exposed publicly
- The Web UI is also bound to `127.0.0.1` for local-only access
- Homebrew services automatically restart both services on login
- Use `mygpt ops` commands for easier service management