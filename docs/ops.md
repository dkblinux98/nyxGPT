# nyxGPT Operations Guide

This document describes operational commands provided by `nyxgpt ops`. These commands manage local services and infrastructure required by nyxGPT, without requiring direct use of `brew`/`launchctl` (macOS) or `systemctl` (Linux), or `docker`.

All commands are safe to run multiple times and are designed for local, single-user systems.

`nyxgpt ops` dispatches by OS: macOS uses Homebrew services + launchd (see
[homebrew.md](homebrew.md)), Linux uses systemd --user units (see
[systemd.md](systemd.md), #3508). The command surface below (`install`,
`status`, `restart`, `stop`, `down`, `doctor`, `logs`, ...) is identical on
both -- only the native service-manager underneath differs.

---

## Overview

`nyxgpt ops` manages:

- FastAPI backend (`nyxgpt-api`)
- Local Web UI (`nyxgpt-web` / Next.js)
- Ollama (via Homebrew on macOS, its own `nyxgpt-ollama.service` on Linux)
- Cassandra container (Docker)
- Cassandra log follower (LaunchAgent on macOS, systemd --user unit on Linux)

Configuration lives outside the repository in:

```
~/.nyxGPT/config.ini
```

Logs default to:

```
~/.nyxGPT/logs/
```

---

## Command Summary

```bash
nyxgpt ops install
nyxgpt ops status
nyxgpt ops restart
nyxgpt ops stop
nyxgpt ops down
nyxgpt ops doctor
nyxgpt ops env-sync
nyxgpt ops secrets-sync
nyxgpt ops logs
nyxgpt ops observability
nyxgpt ops glitchtip-init
nyxgpt ops migrate-volumes
nyxgpt ops verify
```

---

## `nyxgpt ops install`

Reconciles the local machine to nyxGPT's intended **local** topology:
`api`/`web`/`ollama` running natively (Homebrew services on macOS, systemd
--user units on Linux -- see [homebrew.md](homebrew.md) /
[systemd.md](systemd.md)), plus the single ops-managed local Cassandra
container. (The full `docker-compose.yml` stack described in
[docker-compose.md](docker-compose.md) is a separate, alternative
**cloud/server** deployment path — `nyxgpt ops install` never starts it.)

This command:

- Ensures `~/.nyxGPT/config.ini` exists — on a fresh machine it launches the
  interactive setup wizard (the same one behind `nyxgpt wizard`) to create it
  before anything else runs. In a non-interactive shell (no TTY) this step
  fails with instructions to run `nyxgpt wizard` first instead of hanging.
- Migrates any pre-#3346 named-volume container data into
  `~/.nyxGPT/volumes/` (see [`nyxgpt ops migrate-volumes`](#nyxgpt-ops-migrate-volumes)
  below; a no-op if you have none)
- Detects and stops any Docker Compose app-tier containers (`api`, `web`,
  `ollama`, `cassandra`) left running from an earlier raw `docker compose
  up` or a previous mixed-mode install, reporting what it stopped
- **macOS**: installs Homebrew formulas (`nyxgpt-api`, `nyxgpt-web`) if
  missing, or reinstalls them when the vendored source has changed since the
  last install. When `nyxgpt-web` is actually rebuilt this way, the service
  is **restarted** (not just started) so the running `next start` process
  picks up the new `.next` build output instead of continuing to serve the
  old build's chunk manifest against the new on-disk chunks -- see
  [Automatic update recovery](service-worker-pwa.md#automatic-update-recovery)
  for the client-side half of this (#3445).
  **Linux**: builds a self-contained venv (`nyxgpt-api`) and web bundle
  (`nyxgpt-web`) under `~/.nyxGPT/opt/<component>` and (re)starts their
  systemd --user units, unconditionally rebuilding on every run -- see
  [systemd.md](systemd.md#installing-the-services).
- Registers and loads the required log-follower agents (LaunchAgents on
  macOS, systemd --user units on Linux), including the Ollama logs follower
  (tails Ollama's logs into `~/.nyxGPT/logs/ollama.log`, from the
  `nyxgpt-ollama` container's docker logs in Compose mode, or natively from
  Homebrew's own ollama.log on macOS / `nyxgpt-ollama.service`'s own log on
  Linux — see [Ollama logs](api.md#ollama-logs)). On macOS this also
  installs `com.nyxgpt.ollama-env`, which reapplies native Ollama's
  `OLLAMA_MODELS` env var at every login (see
  [Ollama model store](homebrew.md#ollama-model-store)); Linux's
  `nyxgpt-ollama.service` bakes `OLLAMA_MODELS` into the unit's
  `Environment=` directly, so no equivalent agent is needed there (see
  [systemd.md](systemd.md#managing-the-ollama-service-nyxgpt-ollama)).
- Points native Ollama's model store at the same
  `~/.nyxGPT/volumes/ollama/models` directory Compose/Terraform's `ollama`
  container uses, via `OLLAMA_MODELS` (never a symlink), merging in any
  models already pulled natively so they aren't orphaned — see
  [Ollama model store](homebrew.md#ollama-model-store)
- Verifies Docker availability
- Creates the local Cassandra container if it doesn't exist yet (name
  `nyxgpt-cassandra`, image `cassandra:5.0`, bound to
  `${NYXGPT_BIND_ADDR:-127.0.0.1}:${CASSANDRA_PORT:-9042}`, persisted in
  `~/.nyxGPT/volumes/cassandra` -- the same host directory `docker-compose.yml`
  and `terraform/main.tf` bind-mount, see
  [docker-compose.md#volumes](docker-compose.md#volumes)), starts it if it
  exists but is stopped, or leaves it alone if it's already running — via
  plain `docker run`/`docker start`, entirely separate from the Compose
  stack. Refuses to start if the Terraform-managed Cassandra container is
  already running against that same directory.
- Installs log-following helpers
- Starts the observability stack (Grafana, Prometheus, Loki, promtail, the
  OTel collector, Jaeger, GlitchTip) — see
  [`nyxgpt ops observability`](#nyxgpt-ops-observability) below
- Auto-provisions GlitchTip's admin user, organization, project, and DSN —
  see [`nyxgpt ops glitchtip-init`](#nyxgpt-ops-glitchtip-init) below

Usage:

```bash
nyxgpt ops install
```

Pass `--skip-observability` to leave the monitoring/logging/tracing/errors
Compose profiles stopped (e.g. on a host with no Docker, or to save
resources):

```bash
nyxgpt ops install --skip-observability
```

This command is idempotent and **reconciling**: re-running it converges the
machine to the intended local topology rather than only adding new state —
including cleaning up a mixed-mode mess (native services plus a leaked
Compose app tier) left by an earlier run.

### `--terraform`/`--kubernetes`: the other local deployment paths

`nyxgpt ops install --terraform --local` and
`nyxgpt ops install --kubernetes --local` wrap the alternative
[Terraform](terraform.md) and [Kubernetes](kubernetes.md) deployment paths
the same way — one command each, no raw `terraform`/`kubectl` typing. They
are mutually exclusive with each other and with the native reconciliation
above (passing `--terraform`/`--kubernetes` skips the native steps
entirely and runs that deployment's own install sequence instead). `--local`
is required and explicit — see [terraform.md](terraform.md#one-command-bring-up-nyxgpt-ops)
/ [kubernetes.md](kubernetes.md#one-command-bring-up-nyxgpt-ops) for what
each one does and why `--cloud` is rejected today.

Torn down the same way, from [`nyxgpt ops down`](#nyxgpt-ops-down) below:
`nyxgpt ops down --terraform` / `--kubernetes`.

---

## `nyxgpt ops status`

Displays the current runtime status of all managed components.

Usage:

```bash
nyxgpt ops status
```

Reports:

- **Deployment mode** for each component (`api`, `web`, `ollama`, `cassandra`): whether it's
  running natively (Homebrew / the ops-managed Cassandra container) and whether a Docker
  Compose deployment of the same component is also running. If a component is reported
  running in *both* modes, `status` prints a **WARNING** — only one is actually serving
  traffic on the shared port, and config edits to `~/.nyxGPT/config.ini` (native) vs.
  `docker/config.docker.ini` (Compose) reach different, non-interchangeable processes.
- **Terraform component state** for each `nyxgpt-tf-*` core container, when any are
  running. If a component is reported running under Terraform *and* under native/Compose
  at the same time, `status` prints a second **WARNING** — this means an incomplete mode
  switch (e.g. `nyxgpt ops install` after `nyxgpt ops down` without `--terraform`) left
  two whole core stacks up at once, each answering on its own network (#3565).
- Native service state (`started`, `stopped`, `error`) — Homebrew services
  on macOS, systemd --user units on Linux
- Docker container state for Cassandra
- Log-follower agent load state (LaunchAgent on macOS, systemd --user unit
  on Linux)
- A closing pointer to [`nyxgpt ops stop`](#nyxgpt-ops-stop) (stop one
  component) and [`nyxgpt ops down`](#nyxgpt-ops-down) (tear down the whole
  stack) for cleanup

This command does not modify system state.

---

## `nyxgpt ops restart`

Gracefully restarts one or more nyxGPT-managed services.

This is the recommended way to:

- Apply configuration changes
- Recover from transient failures
- Restart services after updates

### Restart all components

```bash
nyxgpt ops restart
```

Equivalent to `nyxgpt ops restart all` -- restarts the native core services
(`api`, `web`, `ollama`), the Cassandra container, the Cassandra logs
LaunchAgent, **and** every currently running observability Compose service
(the `monitoring`/`logging`/`tracing`/`errors` profiles: Prometheus, Grafana,
Loki/promtail, Jaeger, GlitchTip). Unlike `stop`/`down`, `restart all` covers
the whole local stack in one wrapped command -- observability services that
aren't enabled/running are skipped cleanly (no errors) rather than started.

### Restart individual components

```bash
nyxgpt ops restart api
nyxgpt ops restart web
nyxgpt ops restart ollama
nyxgpt ops restart cassandra
nyxgpt ops restart cassandra-logs
nyxgpt ops restart observability
```

### Behavior

- Services are stopped and started cleanly
- Docker containers are **not recreated** unless missing
- Persistent volumes are preserved
- Log-follower agents are reloaded if installed (LaunchAgents on macOS,
  systemd --user units on Linux)
- Before restarting a component, `restart` checks whether a Docker Compose deployment of
  that same component is already running. If so, it **refuses** rather than starting a
  second native process/container that would collide on the same port — you'll see a
  `[FAIL] Refusing to restart native <component>` message naming the port in conflict.
  Stop the Compose deployment (or manage that component through Compose) first.
- Restarting the Cassandra container is **atomic-safe**: if the restart fails in a way that
  leaves a previously-running container stopped, `restart` attempts one recovery start. If
  that also fails, it reports a clear `DOWN: ... is now STOPPED` result instead of silently
  leaving the container down.
- `observability` (included in `all`, or selectable on its own) restarts only the
  observability Compose services that are actually running -- one not currently up is
  reported as "not running (skipped)" rather than being started, since `restart` shouldn't
  change which services are enabled (use [`nyxgpt ops observability`](#nyxgpt-ops-observability)
  to bring the stack up in the first place).

### Exit codes

- `0` — all requested services restarted successfully
- `2` — one or more services failed to restart

After restarting, it is recommended to run:

```bash
nyxgpt ops doctor
```

---

## `nyxgpt ops stop`

Stops one or more nyxGPT-managed services -- native (Homebrew/LaunchAgent)
and/or Docker Compose, whichever is actually running -- without requiring a
raw `docker compose stop` or `brew services stop` command. Data volumes are
never removed; Compose containers are stopped, not brought down (see
[`nyxgpt ops down`](#nyxgpt-ops-down) for that).

### Stop all core components

```bash
nyxgpt ops stop
```

Equivalent to `nyxgpt ops stop all` -- stops `api`, `web`, `ollama`,
`cassandra`, and `cassandra-logs`. Unlike `restart all`, `stop all` does
**not** include `observability` -- that's opt-in via its own target (below),
since it has no native/Homebrew equivalent and stopping it isn't implied by
stopping the core app tier.

### Stop individual components

```bash
nyxgpt ops stop api
nyxgpt ops stop web
nyxgpt ops stop ollama
nyxgpt ops stop cassandra
nyxgpt ops stop cassandra-logs
nyxgpt ops stop observability
```

### Behavior

- For `api`/`web`/`ollama`/`cassandra`, `stop` first detects which
  deployment mode is actually running for that component (native, Compose,
  or both) -- reusing the same detection as
  [`nyxgpt ops status`](#nyxgpt-ops-status) -- and stops the right one.
- If a component is reported running in **both** native and Compose (mixed
  mode), `stop` stops **both** and prints a message calling that out, rather
  than silently leaving the other one live.
- If a component isn't running in either mode, `stop` reports
  `already stopped` and does nothing.
- `cassandra-logs` unloads the log-follower agent (`launchctl bootout` on
  macOS, `systemctl --user stop` on Linux) so it doesn't immediately
  relaunch; an already-unloaded/stopped agent is reported as already stopped
  rather than a failure.
- `observability` stops the running `monitoring`/`logging`/`tracing`/`errors`
  Compose containers (via `docker compose stop`) -- their data (Grafana
  dashboards, Loki logs, GlitchTip issues) is preserved.

### Exit codes

- `0` — every requested component stopped (or was already stopped)
  successfully
- `2` — one or more components failed to stop

---

## `nyxgpt ops down`

Tears down the full local stack -- native services plus the Docker Compose
app and observability tiers -- in one wrapped command, so you never need a
raw `docker compose down`/`brew services stop`/`launchctl` invocation.

### Full teardown

```bash
nyxgpt ops down
```

Stops the native `api`/`web`/`ollama`/`cassandra`/`cassandra-logs`
components (same as `nyxgpt ops stop`), then runs `docker compose down` for
every Compose service in the core app tier and the observability profiles.
Container data (Cassandra data, pulled Ollama models, Grafana/Loki state --
all bind-mounted under `~/.nyxGPT/volumes/`, see
[docker-compose.md#volumes](docker-compose.md#volumes)) is **preserved** by
default.

### Scoping to one tier

```bash
nyxgpt ops down --app-only            # drop only the Compose app tier (api/web/ollama/cassandra)
nyxgpt ops down --observability-only  # drop only the observability Compose profiles
```

`--app-only` and `--observability-only` are mutually exclusive. This is the
wrapped fix for a native/Compose mixed-mode collision (e.g. a stale
`docker compose up` app tier holding a port a native service also wants):
run `nyxgpt ops down --app-only` to drop the Compose app tier while leaving
observability dashboards running.

### Tearing down Terraform/Kubernetes instead

```bash
nyxgpt ops down --terraform    # terraform destroy (see terraform.md)
nyxgpt ops down --kubernetes   # removes the nyxgpt namespace (see kubernetes.md)
```

Mutually exclusive with `--app-only`/`--observability-only` and with each
other -- these tear down the [Terraform](terraform.md)/[Kubernetes](kubernetes.md)
deployments, not the native/Compose stack above.

### Removing data volumes

```bash
nyxgpt ops down --volumes --yes-really
```

`--volumes` also deletes the stack's `~/.nyxGPT/volumes/` bind-mount
directories for the services torn down -- Cassandra's data directory,
pulled Ollama models, Prometheus/Grafana/Loki state. This is **destructive**
and irreversible, so `--volumes` alone is refused; you must also pass
`--yes-really` to confirm. (A raw `docker compose down -v` no longer deletes
anything here -- there are no Docker-managed named volumes left to remove --
this wrapper is what actually deletes the data now.)

### Behavior

- Compose teardown is skipped gracefully (reported as `[OK] Skipped ...`,
  not a failure) on a host with no Docker.
- If neither the app tier nor the observability tier has any Compose
  services actually resolved for the given scope, `down` reports that and
  exits `0` -- it's a no-op, not an error.

### Exit codes

- `0` — teardown completed (or nothing needed tearing down) successfully
- `2` — one or more steps failed, or `--volumes` was passed without
  `--yes-really`

---

## `nyxgpt ops doctor`

Runs a comprehensive system health check.

Usage:

```bash
nyxgpt ops doctor
```

Checks include:

- Required files under `~/.nyxGPT/`
- Native service-manager availability (`brew` on macOS, `systemctl` on Linux)
- Running services
- Docker daemon availability
- Local Cassandra container presence (flags a missing `nyxgpt-cassandra`
  container and suggests `nyxgpt ops install` to create it)
- Log directory writability
- (when log aggregation is enabled) whether the *running* promtail
  container actually has the native-logs bind mount, via `docker inspect`
  -- and, when Loki is reachable, a per-component log volume for the last
  24h so an idle curated component isn't mistaken for a broken pipeline;
  flags a missing or Grafana-rejected doctor service-account token
  (`~/.nyxGPT/secrets/grafana-doctor-token`) instead of silently omitting
  that log volume
  (see [docker-compose.md#log-aggregation](docker-compose.md#log-aggregation))
- (when tracing is enabled) whether something is actually listening on the
  configured `[tracing] otlp_endpoint`, via a live TCP connect -- catches
  the otel-collector container running but not publishing its port to the
  host (the default native deployment's only path to it), which otherwise
  silently drops every span while the panel still reports "active" (see
  [docker-compose.md#distributed-tracing](docker-compose.md#distributed-tracing))
- (when tracing is enabled) whether every `opentelemetry-instrumentation-*`
  package and the OTLP exporter are actually importable in this venv --
  catches a venv that predates a dependency bump (or had a package manually
  uninstalled) running degraded: missing instrumentors are silently skipped,
  or tracing is disabled outright if the exporter itself is missing. Flags
  the missing package names and points at `nyxgpt ops install`
- (macOS only, once the shared Ollama store has been configured) whether
  native Ollama's live `launchctl getenv OLLAMA_MODELS` still matches the
  expected shared `~/.nyxGPT/volumes/ollama/models` path -- catches drift
  back to Ollama's own default store (see
  [homebrew.md#ollama-model-store](homebrew.md#ollama-model-store)). Linux
  has no equivalent check: `nyxgpt-ollama.service`'s `Environment=` is part
  of the unit file itself and can't drift the way a per-session
  `launchctl setenv` can (see
  [systemd.md](systemd.md#managing-the-ollama-service-nyxgpt-ollama))
- (when error tracking is enabled and GlitchTip is reachable) whether the
  configured `[error_tracking] dsn`'s public key still matches a live
  GlitchTip project key -- if GlitchTip's org/project/key ever gets
  re-minted independently of `config.ini` (e.g. its data was reset
  out-of-band), a running api process keeps sending events under a key
  GlitchTip no longer recognizes; every one is rejected (401) and silently
  dropped by sentry_sdk's fire-and-forget transport, the same failure shape
  as the OTLP check above (#3565). Fix: `nyxgpt ops glitchtip-init && nyxgpt
  ops restart api`
- Whether the installed Python environment actually has every dependency
  declared in `pyproject.toml` (via `importlib.metadata`) -- catches a venv
  that wasn't refreshed after a `git pull` added or bumped a dependency,
  reporting exactly which package(s) are missing and to run
  `pip install -e .`
- Whether any Docker Compose service is stuck in a restart/crash loop (state
  `restarting`) -- `nyxgpt ops status` already surfaces that state, but
  doctor now FAILs on it instead of leaving it as an easy-to-miss warning;
  points at `nyxgpt ops logs <service>` for the boot error (#3538)
- Whether a Terraform-managed core component is running at the same time as
  its native/Compose equivalent (dual-stack) -- doctor FAILs on it, since an
  incomplete mode switch otherwise leaves two whole core stacks answering on
  their own networks with `nyxgpt ops status`'s own conflict detector
  reporting no conflict (it only ever compared native vs. Compose). Fix:
  `nyxgpt ops down --terraform` or `nyxgpt ops down`, whichever mode you
  don't want (#3565)

Results are reported with clear PASS / FAIL indicators.

---

## `nyxgpt ops env-sync`

Derives the Docker Compose stack's `.env` secrets from `~/.nyxGPT/config.ini`
(generated by `nyxgpt wizard`), which is the single source of truth for
them. Only the secret lines are touched — `NYXGPT_AUTH_API_KEY` (from
`[auth] api_key`) and `GRAFANA_ADMIN_PASSWORD` (from `[monitoring]
grafana_admin_password`) — everything else already in `.env` (ports, image
tags) is left alone.

Usage:

```bash
nyxgpt ops env-sync
```

If `.env` doesn't exist yet, it's created from `.env.example` first. The
resulting `.env` is chmod'd `600`, same as `config.ini`.

Run this after `nyxgpt wizard` and again any time you rotate a secret in
config.ini, before `docker compose up`.

---

## `nyxgpt ops secrets-sync`

Pushes a declared subset of `~/.nyxGPT/config.ini`'s write-once secrets
(Slack bot token, agent PATs) to this repo's **GitHub Actions** secrets --
one direction only, config.ini → Actions. See [Canonical secret store &
sync to GitHub Actions](configuration.md#canonical-secret-store--sync-to-github-actions)
for the full rationale and the `config.ini` key → Actions secret mapping.

Usage:

```bash
nyxgpt ops secrets-sync            # push every mapped secret that has a value set
nyxgpt ops secrets-sync --dry-run  # show which secrets would be pushed, by name only
```

Requires `[github] pat` (with permission to manage Actions secrets) and
`[github] repo_owner`/`repo_name` in config.ini -- set them with `nyxgpt
secrets setup` if you haven't already. Each value is sealed with the repo's
Actions public key before it's sent (libsodium sealed-box, via PyNaCl); a
value never appears in this command's output, logs, or tracebacks -- only
the secret's name and success/failure. Also available from the web UI at
`/admin/secrets`.

---

## `nyxgpt ops logs`

Prints recent logs for a single component — a wrapped `docker compose
logs`/`docker logs`/`kubectl logs`/native log file read, so reading a
component's output never requires a raw `docker`/`docker compose`/`kubectl`
command, or knowing where its native log file lives.

The command detects which deployment mode the requested component is
actually running under (the same detection `nyxgpt ops status` uses) and
reads the matching source:

| Mode | Source |
| --- | --- |
| Docker Compose | `docker compose logs <service>` |
| Native (`api`) | `~/.nyxGPT/logs/api.log` |
| Native (`ollama`) | `~/.nyxGPT/logs/ollama.log` (see [Ollama logs](api.md#ollama-logs)) |
| Native (`web`) | macOS: Homebrew's own `nyxgpt-web.log`/`.err.log` (see [homebrew.md](homebrew.md#web-ui-logs)). Linux: `~/.nyxGPT/logs/nyxgpt-web.log`/`.err.log` (see [systemd.md](systemd.md#web-ui-logs)) |
| Native (`cassandra`) | `docker logs nyxgpt-cassandra` |
| Terraform | `docker logs <nyxgpt-tf-* container>` |
| Kubernetes | `kubectl logs <pod>` |

A component that's enabled but has no running container/process at all
(e.g. an observability profile enabled in config.ini but torn down via
`nyxgpt ops down`) fails explicitly rather than reporting a hollow success
with no output.

Usage:

```bash
nyxgpt ops logs <service> [--tail N]
```

```bash
nyxgpt ops logs glitchtip
nyxgpt ops logs glitchtip --tail 50
nyxgpt ops logs api
nyxgpt ops logs web
```

`--tail` defaults to 200 lines. This is how to find the GlitchTip
first-account registration confirmation link (see [Error Tracking](api.md#error-tracking)):
the `errors` Compose profile's `EMAIL_URL=consolemail://` prints outgoing
email, confirmation link included, to the `glitchtip` container's stdout
instead of sending it anywhere.

The same logs are reachable without a terminal via `GET
/api/v1/self-heal/logs?service=glitchtip` (see
[api.md](api.md#get-apiv1self-heallogs)) -- the CLI command above is the
scriptable equivalent of that endpoint. (The in-app "View GlitchTip logs"
button that called it lived on the now-retired Error Tracking panel --
Grafana is the single pane of glass now, see
[docker-compose.md](docker-compose.md#grafana-single-pane-of-glass).)

---

## `nyxgpt ops observability`

Starts the full SRE observability stack -- the `monitoring`, `logging`,
`tracing`, and `errors` Docker Compose profiles (Grafana, Prometheus, Loki,
promtail, the OTel collector, Jaeger, GlitchTip) -- so operators never run a
raw `docker compose --profile <name> up` command themselves.

Usage:

```bash
nyxgpt ops observability
```

`nyxgpt ops install` already runs this by default (see
[`nyxgpt ops install`](#nyxgpt-ops-install) above); use this command on its
own to re-run it later (e.g. after a reboot, or if you first installed with
`--skip-observability`).

Behavior:

- Idempotent -- `docker compose up -d` only (re)creates what's
  missing/changed, so re-running never duplicates a dashboard or container.
- Skips (without failing) on a host with no Docker, since these tools have
  no native/Homebrew path -- see [docker-compose.md](docker-compose.md).
- Once the profiles are up, flips `[monitoring]`, `[log_aggregation]`, and
  `[tracing] enabled = true` in `~/.nyxGPT/config.ini` so the Admin
  Dashboard's status badges immediately reflect that they're live, instead
  of still showing "opt-in, not running".
- Deliberately leaves `[error_tracking] enabled` and `dsn` untouched:
  GlitchTip isn't reachable until its container passes its health check,
  which takes a little while after `up -d` returns. `nyxgpt ops install`
  runs [`nyxgpt ops glitchtip-init`](#nyxgpt-ops-glitchtip-init) right
  after this step, which waits for that health check and then flips those
  settings on once it has actually provisioned a DSN.

Grafana dashboards, the Jaeger and Infinity/GlitchTip datasources, and the
SRE Home landing dashboard are all pre-provisioned as code (see
[docker-compose.md](docker-compose.md#grafana-single-pane-of-glass)) --
starting the stack is the only step needed to get a populated SRE view.

---

## `nyxgpt ops glitchtip-init`

Auto-provisions GlitchTip's admin user, organization, project, and DSN --
the last manual step in the SRE observability suite, now zero-touch. No
sign-in, no copy-pasting a DSN.

Usage:

```bash
nyxgpt ops glitchtip-init
```

`nyxgpt ops install` already runs this by default, right after starting
the observability stack (see [`nyxgpt ops install`](#nyxgpt-ops-install)
above); use this command on its own to re-run it later (e.g. after
`--skip-observability`, or if the `glitchtip` container wasn't finished
starting when `install` first ran).

Behavior:

- **Only runs when the `glitchtip` container is up and passes its health
  check** -- it waits out that container's post-start health-check window
  (up to ~2 minutes) before giving up. A no-op with a clear message
  otherwise (no Docker, `--skip-observability`, or the container still
  isn't healthy after waiting) -- it never fails `nyxgpt ops install` for
  this.
- **Idempotent** -- every step (admin user, organization, project, project
  key) first checks for an existing one before creating it, so re-running
  never duplicates anything.
- Bootstraps a superuser non-interactively via GlitchTip's own
  `manage.py createsuperuser --noinput` (no raw `docker exec` -- this
  shells out internally, the user never runs it directly), using
  `[error_tracking] admin_email`/`admin_password` from config.ini if set,
  else generating a strong password and saving it back there (chmod 600,
  same trust model as `[auth] api_key` -- safe because GlitchTip is
  loopback-only). `createsuperuser --noinput` exits rc=1 by design when the
  account already exists; a re-run logs that at INFO ("expected rc=1,
  treated as success"), never a WARNING, so an idempotent re-install on an
  already-provisioned stack never looks like a failure.
- Creates (or reuses) a scoped API token, then the `nyxgpt` organization,
  the `nyxgpt` team (with the provisioning admin confirmed as a member --
  GlitchTip's UI only lists projects on teams the logged-in user belongs
  to, so an org member on no team still sees "This organization has no
  projects", #3565), the `nyxgpt-backend` project (attached to that team),
  and its DSN, all via GlitchTip's Sentry-compatible REST API -- the
  upgrade-stable path, not an ORM/`manage.py shell` seed.
- **Log into the GlitchTip UI (`http://localhost:8080`) as the account
  `[error_tracking] admin_email` in config.ini (`admin@nyxgpt.local` by
  default) -- org `nyxgpt`, project `nyxgpt-backend`.** GlitchTip's open
  self-registration is disabled (`ENABLE_USER_REGISTRATION: "False"` in
  docker-compose.yml, #3565) specifically so a different, self-registered
  account can't end up with its own same-named decoy `nyxgpt` project in a
  different org -- exactly what silently shadowed the real data for days in
  a past acceptance failure. To give a teammate their own login, invite
  them into org `nyxgpt` (`Settings -> Members`) *and* add them to the
  `nyxgpt` team -- an org invite alone leaves them looking at "no
  projects".
- Writes the resulting DSN and `enabled = true` into
  `~/.nyxGPT/config.ini` (native) and `docker/config.docker.ini` (Compose)
  -- the DSN is a public key, safe to store in both. The live
  `docker/config.docker.ini` is a git-ignored, per-machine artifact that
  `nyxgpt ops install`/`env-sync` derive from the native `~/.nyxGPT/config.ini`
  (rewriting only container-network endpoints), so this runtime write never
  dirties a tracked file. That derivation rewrites the DSN's `localhost`
  host:port to the `glitchtip` service's container-network alias (#3565) --
  a containerized api can't reach the native, browser-facing `localhost` DSN
  -- while `glitchtip_ui_url`, opened from the host browser, stays localhost.

Exit codes:

- `0` -- provisioned successfully, or a clean no-op (GlitchTip not
  up/healthy yet)
- `2` -- a step actually failed (e.g. couldn't reach the GlitchTip API,
  or config.ini is missing -- run `nyxgpt wizard` first)

**GlitchTip's "Logs" tab is not error tracking (#3565 acceptance failure).**
GlitchTip 6.x ships a separate structured-logging feature (`apps.logs`,
its own `LogEvent`/`LogResource` models) that's unrelated to the
error/exception tracking (`apps.issue_events`) this integration uses.
nyxGPT's `error_tracking.py` only ever calls `capture_exception`/
`capture_message`, which create GlitchTip **Issues** -- it never sends
anything to the Logs feature, and isn't meant to. GlitchTip's Logs view
saying "No logs found. Configure your SDK to start capturing logs." is
therefore expected and not a signal that error tracking is broken; check
GlitchTip's **Issues** view (or `nyxgpt ops doctor`'s DSN-drift check
above) instead.

---

## `nyxgpt ops alert-test`

Pushes a test notification through the `nyxgpt-slack` contact point, to
verify Slack delivery is wired correctly without waiting for a real
CPU/memory/disk/self-heal/canary threshold breach. This tests **contact-point
delivery only** -- not rule evaluation or notification-policy routing; see
[alerting.md](alerting.md#testing-the-pipeline) for the full walkthrough and
the deliberate-threshold-breach procedure that covers those.

Usage:

```bash
nyxgpt ops alert-test
```

Behavior:

- Requires `[monitoring] enabled = true` and a reachable Grafana -- reports
  a clear, actionable message and exits non-zero otherwise (rather than a
  raw connection error).
- Calls Grafana's receiver-test API
  (`/apis/notifications.alerting.grafana.app/v0alpha1/namespaces/default/receivers/{name}/test`)
  against the `nyxgpt-slack` contact point -- the same API Grafana's own
  **Alerting -> Contact points -> nyxgpt-slack -> Test** button calls, so it
  reuses the currently-provisioned webhook secret rather than needing its own
  copy of it (#3545). An earlier version posted straight into Grafana's
  embedded Alertmanager ingestion API instead
  (`/api/alertmanager/grafana/api/v2/alerts`), which only accepts alerts from
  Grafana's own rule engine and 400s on anything posted externally -- that
  never actually exercised this command against a real Grafana until #3538
  made booting one part of CI.
- If `slack_webhook_url` is unset, the command still confirms the pipeline
  reaches the contact point and attempts delivery -- it reports that clearly
  as an unconfigured-delivery success, not a raw HTTP error.
- If `slack_webhook_url` *is* configured and delivery genuinely fails, the
  command exits non-zero with Grafana's own error message (e.g. an invalid
  webhook token) rather than a bare status code.

Exit codes:

- `0` -- test alert sent successfully
- `2` -- monitoring is disabled, config.ini is missing, or Grafana couldn't
  be reached

---

## `nyxgpt ops migrate-volumes`

Migrates container data out of pre-#3346 named Docker volumes
(`ollama_data`, `cassandra_data`, `nyxgpt_data`, the Terraform `nyxgpt_tf_*`
equivalents, ...) into the `~/.nyxGPT/volumes/<component>/` bind-mount
layout described in
[docs/docker-compose.md#volumes](docker-compose.md#volumes).

Usage:

```bash
nyxgpt ops migrate-volumes
```

`nyxgpt ops install` (native and `--terraform --local`) already runs this
automatically as its first step, so most users never need to run it by
hand -- this is a standalone escape hatch for Compose-only users who never
run `install`.

**Run this before your first `docker compose up`/`up --build` after
upgrading** -- see the ordering warning below.

Behavior:

- **Idempotent via a marker, not directory emptiness** -- each component gets
  a marker file under `~/.nyxGPT/.migration-state/` once it's been
  reconciled (migrated, or confirmed to have no legacy volume), and later
  runs skip components that already have one. This is deliberately *not*
  "destination directory is non-empty", because a freshly-started container
  populates its empty bind mount with new files within seconds -- an
  emptiness check can't tell that apart from "already migrated in a prior
  run".
- Copies each legacy volume's contents through a throwaway container (named
  volumes on macOS/Docker Desktop live inside the Docker VM, not directly
  reachable from the host filesystem), then removes the old volume once the
  copy succeeds. A volume still attached to something else is left behind
  with a note rather than failing the migration.
- A no-op with a clear message if Docker isn't installed, or if no legacy
  volume is found for a component (fresh installs have nothing to migrate).
- **Refuses to auto-migrate, loudly, if a legacy volume still exists for a
  component whose destination is already non-empty and not yet marked
  reconciled** -- e.g. if the new bind-mounted stack was brought up before
  running this command. Auto-merging in that situation risks silently
  overwriting or shadowing whichever side holds the data you actually want,
  so it fails with instructions to inspect both sides, merge by hand, and
  remove the old volume once done (which clears the warning on the next run).

Exit codes:

- `0` -- every component's data was migrated (or already up to date, or had
  nothing to migrate)
- `2` -- a copy failed, or a component was refused because its destination
  was already populated while a legacy volume still exists for it

---

## `nyxgpt ops verify`

The live smoke harness behind #3555/P6-18: boots the stack, generates known
chat/RAG traffic, and asserts it landed via Prometheus and Grafana --
deterministic, scriptable live verification instead of "looks right in the
query syntax." This is what the review agent runs itself in CI on every PR
touching observability, metrics, or UI surfaces (see
[live-verification-ci.md](live-verification-ci.md) and
[review-runbook.md](../agents/runbooks/review-runbook.md)); the same command
also works as a one-command local pre-check before owner acceptance testing.

Usage:

```bash
nyxgpt ops verify                    # boot, test, tear down (ephemeral -- CI's mode)
nyxgpt ops verify --keep-up          # leave the stack up afterward to look around
nyxgpt ops verify --skip-boot        # stack (native or Compose) is already up
nyxgpt ops verify --skip-screenshots # no Playwright browsers installed
nyxgpt ops verify --dashboards rag-performance api-metrics  # override the default set
```

Requires the optional `verify` extra (Playwright is a separate browser-binary
install most commands never need):

```bash
pip install -e ".[verify]"
playwright install --with-deps chromium
```

Behavior:

1. Requires `[monitoring] enabled = true` (same precondition as `nyxgpt ops
   alert-test`) -- exits with an actionable message otherwise.
2. Unless `--skip-boot`: boots the full Docker Compose stack (core app +
   `monitoring`/`logging`/`tracing`/`errors` profiles together -- unlike
   `nyxgpt ops observability`, which deliberately excludes the core app tier
   for native-first installs, this needs everything containerized since CI
   has no native brew/launchd path), waits for `api`/`web`/`ollama`/
   `cassandra` to report healthy, then reconciles Grafana's provisioning
   (same step `install`/`observability` run).
3. Generates one known unit of traffic per required source path: a chat
   round-trip, a RAG document ingest (`POST /rag/ingest`), a RAG file-upload
   ingest (`POST /rag/upload`), a RAG repo ingest (`POST /rag/index-repo`
   against a tiny fixture repo written under the shared `nyxgpt-data`
   volume), and a RAG query.
4. Asserts the traffic landed two independent ways:
   - **Prometheus instant queries** for each expected counter's delta
     (`nyxgpt_chat_requests_total`, `nyxgpt_rag_ingests_total` per source,
     `nyxgpt_rag_queries_total`), polling for Prometheus's next scrape
     rather than racing its 15s interval.
   - **Grafana's HTTP API**, re-executing each touched dashboard panel's own
     query (read straight from the dashboard JSON under
     `docker/grafana/dashboards/`, defaulting to `rag-performance.json` and
     `api-metrics.json`) through Grafana's Prometheus datasource proxy --
     this exercises the actual dashboard wiring, not just whether Prometheus
     has the data. A failure names the exact panel and query.
5. Captures a full-page Playwright screenshot of each touched dashboard to
   `~/.nyxGPT/verify-artifacts/<dashboard-uid>.png` (override with
   `--screenshot-dir`) -- visual evidence for a human, or the review agent's
   Read tool (it's multimodal), to inspect directly.
6. Unless `--skip-boot` was used, tears the stack back down afterward unless
   `--keep-up` is passed.

Exit codes:

- `0` -- every traffic step, Prometheus assertion, Grafana panel assertion,
  and screenshot capture succeeded
- `2` -- config missing, monitoring disabled, the stack failed to boot or
  become healthy, or any assertion/capture failed (each failure's message
  names exactly what failed and why)

---

## Logs

All nyxGPT-managed services write logs under:

```
~/.nyxGPT/logs/
```

Typical files include:

- `api.log` -- the API process's own structured logs (see
  [configuration.md](configuration.md#logging-section))
- `cli.log` -- every `nyxgpt` CLI invocation's own structured logs
- `ollama.log` -- Ollama's logs, tailed in by `follow-ollama-logs.sh`
  (Compose mode: from `docker logs`; native mode: from Homebrew's own
  ollama.log on macOS, or `nyxgpt-ollama.service`'s own
  `ollama-native.log` on Linux -- see [Ollama logs](api.md#ollama-logs))
- `cassandra.log` -- Cassandra's container logs, tailed in by
  `follow-cassandra-logs.sh`
- `cassandra-logfollower.out.log` / `.err.log`, `ollama-logfollower.out.log`
  / `.err.log` -- the log-follower agents' own stdout/stderr (LaunchAgents
  on macOS, systemd --user units on Linux), not the service logs themselves
  (useful only for debugging the follower)
- `ollama-native.log` / `.err.log` -- Linux only: `nyxgpt-ollama.service`'s
  raw `ollama serve` stdout/stderr, the source `follow-ollama-logs.sh` tails
  into the canonical `ollama.log` above

Homebrew's own per-service `nyxgpt-api.log`/`.err.log`/`nyxgpt-web.log`/
`.err.log` (raw process stdout/stderr, useful for a crash before Python/Node
logging is even configured) live under Homebrew's own `var/log` on macOS --
see [homebrew.md](homebrew.md#api-logs). On Linux, the systemd --user units'
equivalent raw stdout/stderr write directly to
`~/.nyxGPT/logs/nyxgpt-api.log`/`.err.log`/`nyxgpt-web.log`/`.err.log` --
see [systemd.md](systemd.md).

### Structured `nyxgpt ops` activity logging

Every `nyxgpt ops` command (`install`, `status`, `restart`, `stop`, `down`, `logs`,
`env-sync`, `secrets-sync`, `doctor`, `observability`, `glitchtip-init`) logs its steps and outcomes from
`src/nyxgpt/ops.py` with structured fields (via the logging module's
`extra={}`, rendered as JSON when `[logging] format = json` -- see
[configuration.md](configuration.md#logging-section)), in addition to the
`[OK]`/`[FAIL]` lines printed to the console:

- **Command start** (`ops: <action> starting ...`) -- `INFO`.
- **Per-step outcome** (`ops: <action> ok/failed: <message>`, with any
  subprocess failure output in `extra["details"]`) -- `INFO` on success,
  `WARNING` on failure (e.g. a missing `brew`/`systemctl`/`docker` binary, a
  port conflict, a failed `npm ci`).
- **Unexpected exception during install** (`ops: install step <step>
  raised ...`) -- `ERROR`, with a full traceback.
- **Deployment-mode conflict** (`ops: native/Compose deployment conflict on
  <components> ...`), logged from `detect_deployment_mode` -- `WARNING`.
- **Command summary** (`ops: <action> succeeded/failed ...`) -- `INFO`.

`nyxgpt ops logs` logs the fetch outcome (service, tail, ok/fail) but not
the tailed log body itself, to avoid duplicating the target service's own
logs into `~/.nyxGPT/logs/cli.log`.

---

## Startup Behavior

Recommended configuration:

- Docker Desktop enabled at login
- Cassandra container started with:

```bash
--restart unless-stopped
```

- nyxGPT services managed exclusively through `nyxgpt ops`

This ensures services survive reboots and recover cleanly.

---

## Troubleshooting

If a service fails to start:

1. Run:
   ```bash
   nyxgpt ops status
   ```
2. Inspect logs in `~/.nyxGPT/logs/`
3. Run:
   ```bash
   nyxgpt ops doctor
   ```

Avoid manually invoking `brew services`/`launchctl` (macOS), `systemctl` (Linux), or `docker run` unless explicitly debugging.

---

## Design Notes

- `nyxgpt ops` intentionally avoids destructive actions by default
- Data loss requires explicit user action
- All operations are local and user-scoped

```
