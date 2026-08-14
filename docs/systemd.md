# systemd Services (Linux)

nyxGPT provides two persistent background services on Linux using
**systemd --user units**, the Linux twin of [homebrew.md](homebrew.md)'s
Homebrew services on macOS (#3508):

1. **nyxgpt-api** - FastAPI backend (REST API)
2. **nyxgpt-web** - Next.js web UI

A third unit, **nyxgpt-ollama**, runs native Ollama itself, so every native
component is managed through the same `systemctl --user` surface. If a
distro-installed system-wide `ollama.service` already holds Ollama's port
(11434), `nyxgpt ops install` stops and disables it so `nyxgpt-ollama` can
take the port over -- see
[Managing the Ollama service](#managing-the-ollama-service-nyxgpt-ollama)
below.

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
  (That installer enables a system-wide `ollama.service`; `nyxgpt ops
  install` disables it in favour of `nyxgpt-ollama` -- see
  [System-wide `ollama.service` conflicts](#system-wide-ollamaservice-conflicts).)
- Docker is **not** a prerequisite: `nyxgpt ops install` installs the engine
  and the Compose plugin from your distro's package manager if they're
  missing, starts the daemon, and adds you to the `docker` group. It uses
  `sudo -n` (never prompts) for those steps -- on a host without passwordless
  sudo it prints the exact commands to run by hand instead. See
  [Privileged install steps](#privileged-install-steps).

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

### System-wide `ollama.service` conflicts

The official Ollama Linux installer

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

auto-enables and starts a **system-wide** `ollama.service` bound to
`127.0.0.1:11434` -- the same port `nyxgpt-ollama.service` needs, so the two
can never both hold it.

**nyxGPT takes the port over.** `nyxgpt ops install` detects a system
`ollama.service` that is active *or* merely enabled (a stopped-but-enabled
unit would grab the port back at the next boot) and runs

```bash
sudo -n systemctl disable --now ollama.service
```

then waits for port 11434 to actually come free before installing and
starting `nyxgpt-ollama.service`. That way Ollama is managed exactly like
`nyxgpt-api`/`nyxgpt-web` -- one `systemctl --user` surface, restartable via
`nyxgpt ops restart ollama`, and pointed at the shared
`~/.nyxGPT/volumes/ollama/models` store every other deployment mode
bind-mounts (the distro unit uses Ollama's own `~/.ollama/models` instead,
so models pulled in one mode wouldn't show up in the others).

If root isn't available without a password prompt, install does **not**
start `nyxgpt-ollama.service` -- it could only crash-loop against the taken
port, which is the failure this behaviour exists to prevent. It reports the
command to run instead:

```bash
sudo systemctl disable --now ollama.service && nyxgpt ops install
```

`nyxgpt ops doctor` reports the same conflict whenever the system unit is
active or enabled, and until it's resolved self-heal reports `ollama` health
via whichever unit is actually serving rather than restart-looping the one
that can't bind.

### Privileged install steps

`nyxgpt ops install` needs root for exactly three things on Linux, all of
them run through `sudo -n` -- the `-n` is the point: it *never* prompts, so a
non-interactive install can't hang waiting on a TTY. On a host with
passwordless sudo (the default on Ubuntu cloud images) they just work;
anywhere else each one fails immediately and prints the command to run by
hand.

| Step | Command | Why |
| --- | --- | --- |
| Ollama port takeover | `systemctl disable --now ollama.service` | Free 11434 for `nyxgpt-ollama` (above) |
| Docker engine | `apt-get install -y docker.io` (or `dnf`/`yum install -y docker`), `systemctl enable --now docker`, `usermod -aG docker $USER` | Cassandra and the whole observability stack are containers |
| Docker Compose plugin | `apt-get install -y docker-compose-v2` (or `docker-compose-plugin`), else the release binary — see below | The observability stack and the Grafana credential reconcile are Compose-driven |
| Observability data dirs | `chown -R <uid>:<gid> ~/.nyxGPT/volumes/{prometheus,grafana,loki}` | See below (falls back to a rootless POSIX ACL) |

**Engine and Compose plugin are separate transactions.** Not every distro
packages both: Amazon Linux 2023 carries `docker` and no compose package at
all, so a combined `dnf install -y docker docker-compose-plugin` fails as a
unit and takes the engine down with the plugin. Install therefore reconciles
them independently, and when no package provides the plugin it fetches
Docker's own static plugin binary to
`/usr/local/lib/docker/cli-plugins/docker-compose` (system-wide, so it is
visible to root too) and verifies `docker compose version` actually works
before reporting success. If even that is unavailable — no `curl`, no root,
an architecture Docker publishes no build for — the step fails with the exact
commands to run by hand, per distro, and the rest of install still runs.

**Observability bind-mount ownership.** dockerd runs as root and creates a
missing bind-mount source directory as `root:root`. Prometheus runs as uid
65534 inside its container, Grafana as 472, Loki as 10001 -- none of which
can write to a root-owned directory, so the container panics and Compose
reports `dependency failed to start: container nyxgpt-prometheus-1 is
unhealthy`. `nyxgpt ops install` pre-creates those directories before the
stack starts and gives them an ownership their container can write to (macOS
never hits this: Docker Desktop's file sharing remaps ownership for you).

Without passwordless sudo the `chown` isn't available, so install falls back
to a POSIX ACL instead -- `setfacl -R -m u:<uid>:rwx <dir>`, which only needs
you to *own* the directory, not root. The ACL grants write access to exactly
the one uid the container runs as and to no one else; nyxGPT deliberately
does not make these directories world-writable, because `grafana.db` holds
sessions and hashed credentials and every local user would inherit them. If
the acl(5) tools aren't installed, the filesystem was mounted without ACL
support, or the directory still contains root-owned files from an earlier
broken run, install fails with the exact `sudo chown -R` to run instead.
`nyxgpt ops doctor` reports any directory left in the broken state the same
way.

**Docker group membership.** Group membership is stamped into a login
session when it's created, so being added to `docker` does not affect your
current shell -- or, more subtly, the already-running `systemd --user`
manager and every service under it, including `nyxgpt-api`. That is how
`nyxgpt ops status` (fresh shell, has the group) and the web UI's
Infrastructure page (long-lived API process, doesn't) can disagree about
whether Cassandra is running. Fix it by recreating the session:

```bash
sudo loginctl terminate-user "$USER"   # kills this SSH session; reconnect after
```

Install itself does not stop and wait for that, though. When the group change
it just made hasn't reached the running session, `nyxgpt ops install` routes
its own Docker calls through a *hop* for the remainder of that process and
says so in its output, so Cassandra, the observability stack and the Grafana
credential reconcile all complete in the same pass instead of failing with
`permission denied while trying to connect to the Docker daemon socket`. Two
hops are tried, in order:

| Hop | Form | Notes |
| --- | --- | --- |
| `sg docker` | `sg docker -c '<command>'` | Preferred: applies the membership just granted, needs no sudoers configuration, and leaves the environment alone. Same mechanism the cloud provisioning script uses. |
| passwordless sudo | `sudo -n --preserve-env docker …` | For a host where the group change itself couldn't be made. `--preserve-env` needs the sudoers `SETENV` tag, which `NOPASSWD: ALL` implies. |

**A hop must preserve the environment, or it isn't used.** `docker-compose.yml`
interpolates `${HOME}` into every bind-mount source, and the GlitchTip
superuser step forwards its credentials with `docker compose exec -e VAR`
(bare, value only in the environment — never on a command line). A hop that
resets the environment — plain `sudo`, whose `env_reset` plus Amazon
Linux/RHEL's `always_set_home` hands the Docker CLI `HOME=/root` — would
quietly build the observability stack against `/root/.nyxGPT/volumes/...`
while the ownership fixes above chown *your* home, and drop those credentials
before they reach the container. Install therefore probes each candidate hop
with both `HOME` and a forwarded variable before adopting it, and reports the
unreachable daemon and the `loginctl` command above rather than adopting one
that fails the probe.

The group membership is still added — a hop only covers the run that created
it, and disappears once you reconnect. It is a last resort, attempted only
after the real group change was made and found not to have taken effect.

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
`System-wide ollama.service is active/enabled and contends for port 11434`.

**Cause**: a distro-installed system-wide `ollama.service` is holding the
port `nyxgpt-ollama.service` needs -- see
[System-wide ollama.service conflicts](#system-wide-ollamaservice-conflicts)
above. Either it was re-enabled after install, or install couldn't get root
without a password prompt to disable it.

**Solution**: re-run `nyxgpt ops install` -- it stops and disables the system
unit, then starts `nyxgpt-ollama` on the freed port. If it reports it can't
get root:

```bash
sudo systemctl disable --now ollama.service && nyxgpt ops install
```

---

## Notes

- All three units run under your user account (`systemctl --user`), not as root
- The API is bound to `127.0.0.1` by default and is not exposed publicly
- The Web UI is also bound to `127.0.0.1` for local-only access
- `systemctl --user enable` (which `nyxgpt ops install` runs for you) makes
  each unit start automatically at every login
- Use `nyxgpt ops` commands for easier service management
