# systemd Services (Linux)

nyxGPT provides two persistent background services on Linux using
**systemd --user units**, the Linux twin of [homebrew.md](homebrew.md)'s
Homebrew services on macOS (#3508):

1. **nyxgpt-api** - FastAPI backend (REST API)
2. **nyxgpt-web** - Next.js web UI

A third unit, **nyxgpt-ollama**, runs native Ollama itself, so every native
component is managed through the same `systemctl --user` surface -- *unless*
a distro-installed system-wide `ollama.service` already holds Ollama's port
(11434) when you run `nyxgpt ops install`, in which case nyxGPT adopts that
unit instead of fighting it for the port. See
[Managing the Ollama service](#managing-the-ollama-service-nyxgpt-ollama)
below for what "adopt" means and how to switch back.

This is the recommended way to keep all three running locally without
keeping terminals open -- and it's what `nyxgpt ops install` sets up for
you; the commands below are for troubleshooting a component directly.

---

## CI

`scripts/systemd-native-smoke.sh` exercises this whole path end-to-end
(`nyxgpt ops install` -> verify every unit is active and the api/web/ollama
respond, each with a bounded retry window rather than a single immediate
probe -> `nyxgpt ops down`), mirroring
`.github/workflows/terraform-local-smoke.yml`'s shape for the Terraform
path. It runs via `.github/workflows/linux-native-smoke.yml`, scoped to
`src/nyxgpt/ops.py`, `src/nyxgpt/self_heal.py`, `ops/systemd/**`, and the
script itself -- or run it by hand with `./scripts/systemd-native-smoke.sh`.

---

## Prerequisites

- Linux with systemd (`systemctl --user` available -- true for any modern
  desktop or server distro: Ubuntu, Debian, Fedora, Arch, ...)
- Python 3.11+ and `npm`/`node` on PATH (nyxGPT builds its own venv/web
  bundle; it doesn't need them pre-installed system-wide beyond that)
- `ollama` on PATH for the `ollama` component -- nyxGPT doesn't install
  Ollama itself on Linux the way the Homebrew formula does on macOS. Install
  it first:
  ```bash
  curl -fsSL https://ollama.com/install.sh | sh
  ```

---

## Installing the services

```bash
nyxgpt ops install
```

Unlike the macOS Homebrew formulas (which build inside a Homebrew Cellar
keg), each Linux service gets a self-contained install root under
`~/.nyxGPT/opt/<component>`:

- `~/.nyxGPT/opt/nyxgpt-api/venv` -- a plain Python venv, `pip install`ed
  from a freshly vendored `pyproject.toml` + `src/nyxgpt/` tree (not the
  repo checkout's own editable `.venv`)
- `~/.nyxGPT/opt/nyxgpt-web/build/nyxgpt-web-<version>` -- a vendored
  `web/` tree, built fresh with `npm ci && npm run build`

Neither depends on the repo checkout existing or staying in place
afterwards. Each also gets a small wrapper script
(`~/.nyxGPT/opt/<component>/bin/nyxgpt-<component>`) and a systemd --user
unit installed to `~/.config/systemd/user/`.

`nyxgpt ops install` always rebuilds both on every run (unlike the macOS
path's checksum-gated skip-if-unchanged) -- a slower but simpler first Linux
implementation.

---

## Managing the API service (nyxgpt-api)

### Start the API

```bash
systemctl --user start nyxgpt-api
```

Verify status:

```bash
systemctl --user status nyxgpt-api
```

### Restart and stop

```bash
systemctl --user restart nyxgpt-api
systemctl --user stop nyxgpt-api
```

### API logs

The application's own structured logs (requests, self-heal, canary, RAG, ...) are written to:

```
~/.nyxGPT/logs/api.log
```

```bash
tail -f ~/.nyxGPT/logs/api.log
```

If the service fails to start (e.g. a crash before Python logging is even
configured), check the unit's own raw stdout/stderr instead:

```bash
tail -f ~/.nyxGPT/logs/nyxgpt-api.log
tail -f ~/.nyxGPT/logs/nyxgpt-api.err.log
```

`nyxgpt ops logs api` (and its `GET /api/v1/self-heal/logs?service=api`
API equivalent) already tails all three as labeled sections, so a
pre-logging startup failure -- e.g. the `[api] host`/`[auth] enabled` bind
refusal -- shows up there too, without needing the raw paths above (#3629).

---

## Managing the Web UI service (nyxgpt-web)

### Start the Web UI

```bash
systemctl --user start nyxgpt-web
```

The web UI will be available at: `http://127.0.0.1:3000`

### Restart and stop

```bash
systemctl --user restart nyxgpt-web
systemctl --user stop nyxgpt-web
```

### Web UI logs

The web UI has no structured `nyxgpt.logging`-based logging yet (tracked by
#3430), so the unit's own raw stdout/stderr is the only place to look:

```bash
tail -f ~/.nyxGPT/logs/nyxgpt-web.log
tail -f ~/.nyxGPT/logs/nyxgpt-web.err.log
```

---

## Managing the Ollama service (nyxgpt-ollama)

### System-wide `ollama.service` conflicts (adoption)

The official Ollama Linux installer

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

auto-enables and starts a **system-wide** `ollama.service` bound to
`127.0.0.1:11434` -- the same port `nyxgpt-ollama.service` needs. Since
stopping/disabling a system-scope unit needs root, and nyxGPT never invokes
`sudo` on its own for anything else either, `nyxgpt ops install` does not try
to take the port by force. Instead it **adopts** the system unit: it detects
the running `ollama.service` and skips installing/starting
`nyxgpt-ollama.service` entirely, leaving the system unit to keep serving
Ollama. If a previous install already created `nyxgpt-ollama.service` (e.g.
before this reconciliation existed, now crash-looping against the same
port), that unit -- unlike the system one -- IS a `--user` unit nyxGPT owns
outright, so `nyxgpt ops install` stops and disables it.

While adopted, `nyxgpt ops status`/the SRE dashboard report `ollama` healthy
via the system unit, and self-heal does not try to restart the absent
`nyxgpt-ollama.service`. `nyxgpt ops doctor` flags the crash-loop state (a
stale `nyxgpt-ollama.service` installed but not active while the system unit
is active) actionably if it's ever encountered.

To have nyxGPT manage Ollama itself instead of the distro's system service,
free the port first, then reinstall:

```bash
sudo systemctl disable --now ollama.service
nyxgpt ops install
```

### Commands (nyxgpt-managed Ollama)

```bash
systemctl --user start nyxgpt-ollama
systemctl --user restart nyxgpt-ollama
systemctl --user stop nyxgpt-ollama
```

`nyxgpt-ollama.service` runs `ollama serve` directly with
`Environment=OLLAMA_MODELS=~/.nyxGPT/volumes/ollama/models` baked into the
unit file -- the same shared model store Compose/Terraform's `ollama`
container uses, so a model pulled in any one local launch mode shows up in
all of them, with no duplicate downloads. `nyxgpt ops install` merges
anything already pulled into Ollama's own default `~/.ollama/models` store
into the shared one automatically the first time it runs.

Unlike macOS (where `launchctl setenv` only applies to the current login
session, requiring a separate `com.nyxgpt.ollama-env` RunAtLoad LaunchAgent
to reapply it every login -- see
[homebrew.md#ollama-model-store](homebrew.md#ollama-model-store)), a
systemd unit's `Environment=` directive is part of the unit file itself and
applies on every start with no companion agent needed. There's nothing to
drift, so `nyxgpt ops doctor`'s launchd-only env-drift check doesn't run on
Linux.

Its logs (the raw `ollama serve` process output) are written to:

```
~/.nyxGPT/logs/ollama-native.log
~/.nyxGPT/logs/ollama-native.err.log
```

and consolidated (with rotation, like macOS's own Ollama log) into the
canonical `~/.nyxGPT/logs/ollama.log` by the `nyxgpt-ollama-logs.service`
log-follower unit -- see [Ollama logs](api.md#ollama-logs).

---

## Managing all three together

```bash
nyxgpt ops restart
```

is the recommended wrapped way to bounce everything at once. The raw
equivalent:

```bash
systemctl --user restart nyxgpt-api nyxgpt-web nyxgpt-ollama
```

### Check status of all units

```bash
systemctl --user list-units 'nyxgpt-*.service'
```

or the wrapped equivalent:

```bash
nyxgpt ops status
```

---

## Service dependencies

**Important:** The Web UI depends on the API service.

- **nyxgpt-api** must be running for the Web UI to function
- Start the API before starting the Web UI
- If the API is down, the Web UI will show connection errors

---

## Configuration

All three systemd services use the same configuration file as the CLI:

```
~/.nyxGPT/config.ini
```

API settings are in `[api]`, web UI settings in `[web]` -- same sections
and hot-reload behavior as documented in
[homebrew.md#configuration](homebrew.md#configuration). Settings that
aren't hot-reloadable require a restart:

```bash
nyxgpt ops restart
```

---

## Log-follower units

`nyxgpt ops install` also installs two always-on systemd --user units that
mirror macOS's `com.nyxgpt.cassandra-logs`/`com.nyxgpt.ollama-logs`
LaunchAgents, running the same OS-agnostic helper scripts:

- `nyxgpt-cassandra-logs.service` -- runs `scripts/follow-cassandra-logs.sh`,
  tailing the `nyxgpt-cassandra` Docker container's logs into
  `~/.nyxGPT/logs/cassandra.log` with rotation
- `nyxgpt-ollama-logs.service` -- runs `scripts/follow-ollama-logs.sh` (see
  [Managing the Ollama service](#managing-the-ollama-service-nyxgpt-ollama)
  above)

```bash
systemctl --user status nyxgpt-cassandra-logs nyxgpt-ollama-logs
```

---

## Accessing the services

After starting all three:

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
   systemctl --user status nyxgpt-api
   curl http://127.0.0.1:8000/health
   ```

2. Check API logs:
   ```bash
   tail -f ~/.nyxGPT/logs/api.log
   ```

3. Restart API service:
   ```bash
   systemctl --user restart nyxgpt-api
   ```

### Service won't start

**Symptom**: `systemctl --user start` succeeds but the service isn't running

**Solutions**:

1. Check the unit's own logs for a crash before Python/Node logging is even set up:
   ```bash
   tail -f ~/.nyxGPT/logs/nyxgpt-api.err.log
   tail -f ~/.nyxGPT/logs/nyxgpt-web.err.log
   journalctl --user -u nyxgpt-api -n 100 --no-pager
   ```

2. Verify configuration is valid:
   ```bash
   cat ~/.nyxGPT/config.ini
   ```

3. Check port conflicts:
   ```bash
   ss -ltnp | grep :8000  # API port
   ss -ltnp | grep :3000  # Web UI port
   ```

4. Run `nyxgpt ops doctor` for health checks:
   ```bash
   nyxgpt ops doctor
   ```

### `systemctl --user` reports "Failed to connect to bus"

**Symptom**: Every `systemctl --user` command fails with a D-Bus connection
error, typically on a freshly provisioned server or inside a minimal
container that has no active login session.

**Solutions**:

1. Ensure a user session (and its D-Bus session bus) actually exists --
   logging in over SSH normally starts one automatically. On a headless
   server that should keep running services after logout, enable lingering
   for the account once:
   ```bash
   loginctl enable-linger "$USER"
   ```

2. Re-run `nyxgpt ops install` after lingering is enabled.

### ollama not found on PATH

**Symptom**: `nyxgpt ops install` reports `ollama not found on PATH`.

**Solutions**:

1. Install Ollama via its official Linux installer:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```

2. Re-run `nyxgpt ops install`.

### nyxgpt-ollama.service crash-looping / port 11434 already in use

**Symptom**: `systemctl --user status nyxgpt-ollama` shows
`activating (auto-restart)` or `failed`, and `nyxgpt ops doctor` reports
`System-wide ollama.service is bound to port 11434, so nyxgpt-ollama.service
can't start`.

**Cause**: a distro-installed system-wide `ollama.service` is already
serving on the port `nyxgpt-ollama.service` needs -- see
[System-wide ollama.service conflicts](#system-wide-ollamaservice-conflicts-adoption)
above. This only happens on a machine that installed before that
reconciliation existed, or that had `ollama.service` re-enabled afterwards.

**Solution**: re-run `nyxgpt ops install` -- it detects the conflict, stops
and disables the stale `nyxgpt-ollama.service`, and adopts the system unit.

---

## Notes

- All three units run under your user account (`systemctl --user`), not as root
- The API is bound to `127.0.0.1` by default and is not exposed publicly
- The Web UI is also bound to `127.0.0.1` for local-only access
- `systemctl --user enable` (which `nyxgpt ops install` runs for you) makes
  each unit start automatically at every login
- Use `nyxgpt ops` commands for easier service management
