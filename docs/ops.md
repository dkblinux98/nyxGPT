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

## Installing nyxGPT

nyxGPT installs from published artifacts — the PyPI wheel, the remote Homebrew
tap, or the GHCR container images. A repository checkout is **only** used for
developing nyxGPT itself ([development.md](development.md)); no user-facing
install or operate flow requires one.

### Prerequisites

- Python 3.11+ (or Homebrew, on the macOS native path)
- Docker — required for the Cassandra container behind RAG
- Node.js — for the local web UI on the native paths

Ollama is installed and managed for you by `nyxgpt ops install` (Homebrew
service on macOS, `nyxgpt-ollama.service` on Linux).

### Install

On **macOS**, install the native services from the remote tap, which brings
their launchd wiring with them:

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # one-time: Homebrew gates third-party taps
brew install nyxgpt-api nyxgpt-web
```

(That is the remote tap — see [homebrew.md](homebrew.md#remote-tap), and
[Trusting the tap](homebrew.md#trusting-the-tap-one-time-required) for the
trust step.) pip is not the macOS path: Homebrew's Python is PEP 668
externally managed, so `pip3 install nyxgpt` is refused.

On **Linux**, install the published wheel from PyPI:

```bash
pipx install nyxgpt   # or `pip install nyxgpt` where the system Python is not externally managed
```

Other targets — Docker/Compose, Kubernetes, AWS EC2 — and the exact install
command and current state of each are in the
[portability matrix](portability-matrix.md#the-matrix). To acceptance-test a
build that has not been released yet, install a pinned release candidate rather
than the latest stable: see
[PyPI publishing (rc and stable)](cloud.md#pypi-publishing-rc-and-stable) and
[Accepting code that isn't released yet](portability-matrix.md#accepting-code-that-isnt-released-yet).

### First run

```bash
nyxgpt wizard          # interactive setup: Ollama connection, default model, RAG, config.ini
nyxgpt secrets setup   # guided, masked entry for optional secrets ([openai] api_key, [github] pat)
nyxgpt up              # reconcile and start everything, wait for health, print the web UI URL
nyxgpt chat "Hello"    # or use the web UI at the printed URL
```

The wizard tests your Ollama connection, helps you pick a default model,
optionally configures RAG, and generates `~/.nyxGPT/config.ini` — all runtime
configuration and data live outside the repository (see
[configuration.md](configuration.md#runtime-data-layout)). `nyxgpt secrets
setup` walks through any remaining human-provided secrets one at a time (masked
input, where to obtain each one, format validation) and is safe to re-run.

From there, [`nyxgpt ops`](#command-summary) covers restarting, stopping,
tearing down, and diagnosing every component, and [cli.md](cli.md) is the full
command reference.

---

## Command Summary

```bash
nyxgpt up              # alias for `nyxgpt ops install` + health-wait + URL
nyxgpt down            # alias for `nyxgpt ops down`
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
nyxgpt ops credentials
nyxgpt ops glitchtip-init
nyxgpt ops migrate-volumes
nyxgpt ops port-forward
nyxgpt ops verify
nyxgpt ops portability
```

---

## `nyxgpt up` / `nyxgpt down`

`nyxgpt up` and `nyxgpt down` are thin, top-level aliases for
[`nyxgpt ops install`](#nyxgpt-ops-install) and
[`nyxgpt ops down`](#nyxgpt-ops-down) -- same single code path, no forked
behavior, every mode flag (`--terraform`, `--kubernetes`, `--local`,
`--skip-observability`, `--app-only`, `--volumes --yes-really`, ...) passes
straight through unchanged. They exist because `up`/`down` is the first
thing most operators reach for, before discovering the fuller `nyxgpt ops`
surface.

`nyxgpt up`'s only difference from calling `install` directly: once the
reconcile finishes, it waits for every desired component to report healthy
(reusing the same cross-mode probes `nyxgpt self-heal status` uses --
`self_heal.list_component_status()`) and then prints the web UI URL:

```bash
$ nyxgpt up
...
Waiting for components to report healthy...
nyxGPT is up: http://127.0.0.1:3000
```

- `--timeout SECONDS` — how long to wait for health before giving up
  (default 180)
- `--no-wait` — return as soon as `install` finishes, without waiting for
  health or printing the URL
- Under `--kubernetes`, Services are ClusterIP-only, so `up` prints
  `nyxgpt ops port-forward` instructions instead of claiming the URL is
  directly reachable (see [kubernetes.md](kubernetes.md#4-verify))

Both are idempotent, same as `install`/`down` themselves: re-running `up`
just reconciles and re-waits; re-running `down` on an already-torn-down
stack is a no-op.

```bash
nyxgpt down    # exactly `nyxgpt ops down`
nyxgpt ops verify
```

---

## Live progress output

The long-running commands -- `install`, `down`, `restart`, `stop`,
`observability`, `env-sync`, and `glitchtip-init` -- stream progress as they
run instead of staying silent until the end (#3558). By default, each one
announces every step before running it, with a `[n/m]` counter:

```
[1/18] clear intentional-stop markers...
[OK] Cleared intentional-stop markers for api, web, ollama, cassandra
[2/18] config...
[OK] Config already present at ~/.nyxGPT/config.ini
...
```

A step that runs longer than a few seconds (a Homebrew install, an image
pull, waiting for Cassandra to become ready) prints a periodic "still
running" heartbeat with elapsed time, so a slow step reads differently from
a hung one:

```
[8/18] cassandra container...
    ... still running (5s): cassandra container
    ... still running (10s): cassandra container
[OK] Cassandra container created and healthy
```

If a step fails -- including one that raises an unexpected exception -- the
output names the step, shows the underlying error, and gives a remediation
hint (run `nyxgpt ops doctor` for diagnostics, or `nyxgpt ops logs
<service>` for recent logs) instead of a bare `[FAIL]` line.

When the failure came from a subprocess, that subprocess's own output is
part of the report -- in the console, in the log files, and in `nyxgpt ops
logs` -- so the reason is readable without re-running the command by hand
(#3783):

```
WARNING nyxgpt.ops: Subprocess exited non-zero (rc=1): ~/.nyxGPT/native/nyxgpt-api/venv/bin/pip install nyxgpt-api-3.0.0.tar.gz
--- subprocess output ---
Processing nyxgpt-api-3.0.0.tar.gz
ERROR: Package 'nyxgpt-api' requires a different Python: 3.9.16 not in '>=3.11'
--- end subprocess output ---
WARNING nyxgpt.ops: ops: install failed: Failed to pip install nyxgpt-api
ERROR: Package 'nyxgpt-api' requires a different Python: 3.9.16 not in '>=3.11'
```

The excerpt is bounded: a command that emits thousands of lines (`npm ci`, a
long dependency resolution) is reported as its first few and last twenty
lines with an explicit `... [N lines omitted] ...` marker between them, and
an over-long single line is clipped with `... [line truncated]`. Nothing is
dropped silently, and no failure is ever reported with zero output.

A step that checks several things reports one coherent status rather than a
mix the reader has to reconcile (#3762):

- An attempt that failed but that the *same step* then recovered from --
  adding the invoking user to the `docker` group fails, and ops reaches the
  daemon through `sg docker` instead -- prints as `[NOTE] Superseded: ...`,
  keeping the original diagnostic in its detail line without claiming the
  step failed.
- A step that really did end with some checks failing and others passing
  closes with a single verdict line, so the last thing printed about a step
  is always its answer:

```
[4/23] docker engine...
[FAIL] Could not install Docker Compose automatically
[OK] Docker daemon is reachable
[FAIL] step 4/23 'docker engine' did not fully succeed: 1 of 3 checks failed (Could not install Docker Compose automatically)
```

A run's summary also lists any step that took longer than a few seconds, with its duration,
so slow steps are easy to spot after the fact:

```
Slow steps (over 3s):
  cassandra container: 12.4s
  ollama service: 4.1s
```

Pass `--quiet` to any of these commands for the old terse, scripting-friendly
output -- just the `[OK]`/`[FAIL]`/`[SKIP]`/`[NOTE]`/`[PENDING]` result lines and the
per-step verdict, with no `[n/m]` announcements, heartbeat, or slow-step
summary:

```bash
nyxgpt ops install --quiet
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

- Syncs the packaged Compose file, config/provisioning templates,
  launchd/systemd unit templates, and helper scripts into `~/.nyxGPT` (see
  [Design Notes](#design-notes) below) — every other step in this list reads
  from that synced copy, not from a source checkout, so `nyxgpt ops install`
  works the same whether nyxGPT is running from `pip install -e .` or an
  installed, non-editable package (#3621).
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
- Pulls the models the stack needs into Ollama, if they are not already
  there: the configured chat model (`[nyxgpt] default_model`) and the
  configured embedding model (`[rag] embedding_model`). Both, whether or not
  RAG is enabled — `rag_enabled` is a per-session toggle a user can turn on
  at any moment, and turning it on must not stall on a download. This step
  is what makes the first chat message work on a machine where nobody ran a
  model pull by hand (#3824). It cannot be skipped: there is no flag for it,
  because an install that reports success while chat is broken is the exact
  state it exists to prevent. Idempotent — a model already in the store is
  reported present and nothing is downloaded — and a pull that fails is a
  `[FAIL]` line, so `nyxgpt up` does not report the stack healthy. The
  container-run modes carry the same behavior in their own manifests (the
  Compose `ollama` service's pre-pull and healthcheck, the Kubernetes
  StatefulSet's postStart hook and readiness probe), because no `nyxgpt`
  process runs on the host there to do it for them.
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

### `--dev`: run the current checkout, without an artifact build

`nyxgpt up --dev` (equivalently `nyxgpt ops install --dev`) installs the
**same topology** as above — native api/web service wrappers, Ollama, the
Cassandra container, observability — but builds `api` and `web` from the
checkout you run it in instead of from an artifact. The flag means the same
thing in Kubernetes mode (`nyxgpt ops install --kubernetes --local --dev`,
#3834): the two container images are built from the working tree rather than
from the published artifacts — see
[kubernetes.md](kubernetes.md#install-modes-artifact-and---dev). The table
below is the native pair:

| | artifact path (default) | `--dev` |
| --- | --- | --- |
| `api` | vendored/published `nyxgpt-api-<version>.tar.gz` installed into a venv (Homebrew keg on macOS) | `pip install -e <checkout>` into `~/.nyxGPT/opt/nyxgpt-api/venv` |
| `web` | `npm ci && npm run build`, service runs `npm run start` on the built bundle | service runs `npm run dev` in `<checkout>/web` |
| macOS service manager | `brew services nyxgpt-api` / `nyxgpt-web` | LaunchAgents `com.nyxgpt.api` / `com.nyxgpt.web` |
| Linux service manager | `nyxgpt-api.service` / `nyxgpt-web.service` | the same units — only the wrapper script they exec changes |
| Homebrew involvement | tap + formula + keg build | none for api/web |

What it is for: iterating on, and acceptance-testing, the code that is in
the working tree right now. `git pull && nyxgpt up --dev` brings the stack
up on that HEAD with no keg or tarball build in between; afterwards, a
`nyxgpt ops restart api` picks up further edits (the web dev server picks
them up on its own).

The api venv is built the same way in both modes — through the interpreter
selection described under [systemd prerequisites](systemd.md#prerequisites),
never bare `python3` on faith. `pip install -e` enforces `requires-python`
exactly as installing the tarball does, so a distro whose `python3` is below
the floor fails a dev install for the same reason it failed a cloud deploy
(#3782).

```bash
nyxgpt up --dev                     # install/reconcile in dev mode
nyxgpt ops restart api              # pick up new api code from the tree
nyxgpt up                           # switch back to the artifact path
```

Constraints, by design:

- **Checkout-only.** Dev mode needs `pyproject.toml`, `src/nyxgpt/` and
  `web/` next to the running `nyxgpt`. Run from an installed package it
  refuses immediately, naming the path it looked at, rather than
  half-installing.
- **Not the default, and not a substitute for artifact testing.** A bare
  `nyxgpt up` is the artifact path, unchanged; dev mode exercises neither
  the published tap/wheel nor the production web build, so acceptance of a
  release still runs the artifact path (see
  [portability-matrix.md](portability-matrix.md)).
- **Labelled everywhere.** [`nyxgpt ops status`](#nyxgpt-ops-status) and
  [`nyxgpt ops doctor`](#nyxgpt-ops-doctor) print `Install mode (native
  api/web): dev (editable checkout at …)` and tag a *running* `api`/`web`
  with `[dev]`, so a dev-mode pass can't be read as an artifact-path pass.
  The mode is recorded in `~/.nyxGPT/install-mode.json`.
- **Per deployment.** `--dev` means the same thing for the Kubernetes and
  Terraform deployments — the api/web images built from the working tree
  instead of from the published ones (see
  [terraform.md](terraform.md#install-modes-artifact-default-and---dev)) —
  and each records its mode in its own marker,
  `~/.nyxGPT/install-mode-kubernetes.json` (#3834) and
  `~/.nyxGPT/install-mode-terraform.json` (#3835). They are reported as
  separate `Install mode (…)` lines because they are separate deployments,
  are often in different modes, and one machine can run all three at once;
  none ever speaks for another.
- **Switching modes is reconciled, not layered.** Installing one mode over
  the other stops the previous mode's services first (dev LaunchAgents are
  unloaded and removed; the artifact path's brew services are stopped) and
  rebuilds the shared api venv from empty, so nothing is left holding ports
  8000/3000 or racing for the `nyxgpt` import.
- Self-heal follows the recorded mode too: in dev mode it restarts the
  LaunchAgents rather than `brew services`, so the watchdog can't start an
  old keg on top of a running dev process.
- The admin dashboard's **Infrastructure** page carries the same label: its
  Native card is badged `DEV INSTALL` / `ARTIFACT INSTALL` and, in dev mode,
  names the checkout being served ([ui.md](ui.md)).
- On macOS, `nyxgpt ops down`/`stop` unloads the dev LaunchAgents but leaves
  their plists in `~/Library/LaunchAgents`, so they load again at the next
  login — switching back with `nyxgpt up` removes them outright.

Which parts of this are proven by CI: the install mechanics (editable venv,
dev-server wrapper, mode recording, and the switch back to the artifact path)
are executed on a real runner by `linux-native-smoke.yml`'s
`linux-native-dev-smoke` job; the macOS launchd load is owner acceptance, for
the reason in [live-verification-ci.md](live-verification-ci.md#what-ci-cannot-cover-owner-acceptance-only).

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

`--dev` composes with `--terraform`: it builds that deployment's api/web
images from the checkout instead of deploying the published ones, and the
deployment records and reports its own install mode
([terraform.md](terraform.md#install-modes-artifact-default-and---dev)).

Both deploy observability with the app tier, and `--skip-observability`
means the same thing in all three modes. In `--kubernetes` mode that layer
runs *inside the cluster* (`k8s/observability/`) rather than as Compose
profiles, which cannot reach a cluster — see [Observability in the
cluster](kubernetes.md#observability-in-the-cluster) for the workloads it
deploys and how to reach their UIs.

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

- **Install mode (native api/web)** — `artifact` (the default:
  published/vendored builds) or `dev` (the checkout, see
  [`--dev`](#--dev-run-the-current-checkout-without-an-artifact-build)),
  printed first and repeated as `[artifact]`/`[dev]` next to `api` and `web`
  — only when that component is actually installed, never on a `none`
  (#3834). In dev mode it also names the checkout the services are running,
  and warns if that checkout has since disappeared. When no native api/web
  exists, the line says so rather than letting a leftover record read as a
  statement about whatever *is* serving. A Kubernetes deployment's own
  install mode is reported in the Kubernetes section below.
- **Install mode (terraform)** — the same line for the local Terraform
  deployment when there is one (#3835), naming the images it is running and
  tagging its `api`/`web` components. A deployment that is running with
  nothing recorded is reported as `not recorded` (tagged `[unrecorded]` per
  component) rather than defaulting to `artifact`, which for that path would
  be backwards — see [terraform.md](terraform.md).
- **Deployment mode** for each component (`api`, `web`, `ollama`, `cassandra`): whether it's
  running natively (Homebrew / the ops-managed Cassandra container) and whether a Docker
  Compose deployment of the same component is also running. If a component is reported
  running in *both* modes, `status` prints a **WARNING** — only one is actually serving
  traffic on the shared port, and config edits to `~/.nyxGPT/config.ini` (native) vs.
  `docker/config.docker.ini` (Compose) reach different, non-interchangeable processes.
- **Kubernetes deployment**, when Pods are present: the namespace's Pods, the
  cluster context, the in-cluster observability workloads, the per-component
  canary rollout state — and that deployment's **own install mode**, `artifact`
  (images built from the published `nyxgpt-api`/`nyxgpt-web` artifacts) or
  `dev` (images built from a checkout's working tree, #3834). It is reported
  here, not in the native line above, because the two are separate installs
  ([kubernetes.md](kubernetes.md#install-modes-artifact-and---dev)).
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
- **Required models** — the configured chat and embedding models, and whether
  Ollama has each (`PRESENT`/`MISSING`, or `UNKNOWN` when Ollama itself did
  not answer). A missing one is printed with the `nyxgpt` command that fixes
  it. The same readiness view the SRE/admin dashboard's Required Models panel
  renders, and the one `/api/v1/models/required` returns (#3824)
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

- The **install mode** each deployment on the machine is on (printed before
  the findings, same vocabulary as `status`). In dev mode it FAILs when the
  recorded checkout is gone — the api/web services are then running code
  nothing can rebuild — or when that checkout has no `web/node_modules` for
  the dev server to start from. Fix: `nyxgpt up --dev` from a checkout, or
  `nyxgpt up` to return to the artifact path. A running dev-mode Terraform
  deployment whose checkout is gone FAILs for the same reason: its images
  cannot be rebuilt.
- Required files under `~/.nyxGPT/`
- Native service-manager availability (`brew` on macOS, `systemctl` on Linux)
- Running services
- Docker daemon availability
- Local Cassandra container presence (flags a missing `nyxgpt-cassandra`
  container and suggests `nyxgpt ops install` to create it)
- Required-model presence: whether Ollama actually holds the configured chat
  and embedding models. A missing one is reported with the `nyxgpt` command
  that fixes it (`nyxgpt ops install`, or `nyxgpt models pull <model>`) —
  never a raw `ollama pull`. Silent when Ollama itself is unreachable: that
  is the ollama service's failure, reported by `status`/self-heal, and
  calling it a missing model would misname the fault (#3824)
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
- (when monitoring is enabled and Prometheus is reachable) whether
  Prometheus's own `nyxgpt-api` scrape target is actually `up`, reporting its
  `lastError` verbatim -- the metrics twin of the OTLP check above, and just
  as silent otherwise: a failed scrape leaves every Grafana dashboard
  rendering "No data" while Prometheus, Grafana, and the API all report
  healthy. On Linux the usual cause is that containers have no route to the
  host's `127.0.0.1`, so the hint points at the `host-api-relay` service
  (see
  [docker-compose.md#linux-scraping-the-native-api](docker-compose.md#linux-scraping-the-native-api))
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
the secret's name and success/failure. CLI only: the dashboard button that
used to run this went away with the `/admin/secrets` screen (#3805).

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
| Native (`api`) | `~/.nyxGPT/logs/api.log` plus the native service's own stdout/stderr, as labeled sections (macOS: Homebrew's `nyxgpt-api.log`/`.err.log`, see [homebrew.md](homebrew.md#api-logs); Linux: `~/.nyxGPT/logs/nyxgpt-api.log`/`.err.log`, see [systemd.md](systemd.md#api-logs)) -- so a pre-logging startup failure (e.g. the P6-1 bind refusal, #3500) is still visible (#3629) |
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
nyxgpt ops observability                        # Compose profiles (default)
nyxgpt ops observability --kubernetes --local   # the in-cluster layer
```

`nyxgpt ops install` already runs this by default (see
[`nyxgpt ops install`](#nyxgpt-ops-install) above); use this command on its
own to re-run it later (e.g. after a reboot, or if you first installed with
`--skip-observability`).

`--kubernetes --local` targets a cluster instead of Compose (#3787): it
applies `k8s/observability/` -- Prometheus, Grafana, Loki + promtail, the
OTel collector, Jaeger and GlitchTip as in-cluster workloads -- without
touching the app tier, and generates Grafana's provisioning ConfigMaps from
the same `docker/grafana/` files the Compose path uses. It returns when those
workloads have rolled out, not when the objects were accepted (#3826), so a
first run pulling their images can take several minutes. The Compose profiles
are not an option in that mode (they scrape the host and resolve Compose
service names), which is why this branches rather than reconciling both. See
[kubernetes.md](kubernetes.md#observability-in-the-cluster); reach the UIs
with [`nyxgpt ops port-forward --target
observability`](#nyxgpt-ops-port-forward).

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
- On **Linux**, reconciles the `host-api-relay` service before bringing the
  stack up. Containers there have no route to the host's `127.0.0.1`, so
  Prometheus cannot scrape a natively-installed API and every Grafana panel
  stays empty; the relay listens on the Docker bridge gateway and forwards to
  the host's loopback, so `[api] host` can stay `127.0.0.1` instead of being
  widened to `0.0.0.0`. It's enabled only when it's both needed and safe (not
  on macOS, not when `[api] host` is already non-loopback, not when the bridge
  gateway can't be resolved) and written back to `disabled` when it isn't. See
  [docker-compose.md](docker-compose.md#linux-scraping-the-native-api).

Grafana dashboards, the Jaeger and Infinity/GlitchTip datasources, and the
SRE Home landing dashboard are all pre-provisioned as code (see
[docker-compose.md](docker-compose.md#grafana-single-pane-of-glass)) --
starting the stack is the only step needed to get a populated SRE view.

---

## `nyxgpt ops credentials`

Prints the admin logins for the observability UIs behind the SRE dashboard
(Grafana and GlitchTip), so signing into one never means SSHing to the box
and reading a secret file by hand (#3718).

Usage:

```bash
nyxgpt ops credentials                      # both services
nyxgpt ops credentials --service grafana    # just one
nyxgpt ops credentials --json               # machine-readable
```

Example:

```
grafana
  URL:      http://localhost:3001
  Username: admin
  Password: <the resolved password>
  Source:   /Users/you/.nyxGPT/secrets/grafana-admin-password

glitchtip
  URL:      http://localhost:8080
  Username: admin@nyxgpt.local
  Password: <the resolved password>
  Source:   /Users/you/.nyxGPT/config.ini [error_tracking] admin_password
```

Behavior:

- **Each credential comes from its real source**, so what is printed is
  what the running service actually accepts:
  - Grafana: `[monitoring] grafana_admin_password` from config.ini when you
    have set one (a deliberate override), otherwise the ops-managed secret
    `nyxgpt ops install` generated at
    `~/.nyxGPT/secrets/grafana-admin-password`.
  - GlitchTip: the `[error_tracking] admin_email`/`admin_password` values
    [`nyxgpt ops glitchtip-init`](#nyxgpt-ops-glitchtip-init) provisions
    back into config.ini.
  - The `Source:` line names which one answered -- useful when a login
    fails and you need to know which of the two Grafana is actually set to.
- **Reading never provisions.** If a service has no password yet, that is
  reported as `(not provisioned)` with the wrapped command that creates one
  (`nyxgpt ops install` for Grafana, `nyxgpt ops glitchtip-init` for
  GlitchTip) rather than minting a value no running service would accept.
- **CLI-side only.** These are ops-managed secrets and are deliberately
  absent from every HTTP API response (#3458/#3466) -- `GET
  /api/v1/monitoring` does not include the Grafana password, and this
  command does not change that. The values go to stdout and are never
  written to the logs.
- For a cloud deployment, use
  [`nyxgpt cloud credentials`](cloud.md#nyxgpt-cloud-credentials), which
  reads the same values off the instance over the wrapped SSH access path.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Every requested service resolved a password |
| 2 | At least one has none provisioned yet (the remediation is printed) |

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

## `nyxgpt ops port-forward`

Forwards a Kubernetes Service to `127.0.0.1` so it's reachable from the
operator's own workstation. `k8s/`'s Services are ClusterIP-only -- there's
no Ingress/LoadBalancer (see [kubernetes.md](kubernetes.md#4-verify)) -- so
this is the only way to reach the web UI, or any observability UI, after a
`--kubernetes` install. It's a thin wrapper around `kubectl port-forward` so
operators never need to type the raw `kubectl` command themselves; `nyxgpt up
--kubernetes` prints this command as its next step once the stack reports
healthy.

`--target` selects what to forward (default `web`): `web`, `api`, `grafana`,
`prometheus`, `jaeger`, `glitchtip`, or `observability` for all four
observability UIs at once. Each target's default local port is the one that
UI is published on in every other mode -- Grafana `3001`, Prometheus `9090`,
Jaeger `16686`, GlitchTip `8080` -- which is what makes the admin
dashboard's observability links (built from `[monitoring] grafana_ui_url`
and friends) work unchanged in Kubernetes mode (#3787). `--port` overrides
the local port for a single target; it is rejected with `--target
observability`, where there are four.

Usage:

```bash
nyxgpt ops port-forward                          # nyxgpt-web on 3000
nyxgpt ops port-forward --port 3005              # ... on a different local port
nyxgpt ops port-forward --target grafana         # Grafana on 3001
nyxgpt ops port-forward --target observability   # Grafana, Prometheus, Jaeger, GlitchTip
```

Runs in the foreground until interrupted (`Ctrl-C`), same as `kubectl
port-forward` itself. Exits `2` if `kubectl` isn't on `PATH` or the
target/port combination is invalid; otherwise returns the exit code of the
first forward that stops (with `--target observability`, any one of them
exiting ends the command and tears the rest down, since a half-working set of
tunnels is worse than an obvious failure).

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

## `nyxgpt ops portability`

Reports the **repo-less portability matrix** — which deployment targets install
and operate with no repo checkout, what evidence backs each one, and which gaps
are still open — plus the clean-machine sequence that accepts Phase 6. See
[portability-matrix.md](portability-matrix.md) for the matrix itself and the
full acceptance runbook.

```bash
nyxgpt ops portability            # the operator report
nyxgpt ops portability --json     # machine-readable
nyxgpt ops portability --strict   # gate: non-zero while any target needs a checkout
```

Reads nothing but the matrix definition and (when run from a checkout) the
existence of the paths each row cites as evidence: no subprocesses, no network,
no AWS. Safe to run anywhere, including on a target machine you are accepting.

Each row is checked, not merely printed — no command may fetch source
(`git clone` and friends), and none may be a raw
`docker`/`kubectl`/`terraform` invocation, per CLAUDE.md's Repo-less
Portability and Operational Command Wrapping requirements.

Exit codes:

- `0` -- every row's mechanical checks passed (the default reporting mode
  succeeds even when a target still has an open gap, so the report is usable
  as a status command)
- `1` -- a row failed a check, or `--strict` was passed and at least one
  target is not yet installable without a checkout

This command is the only way to read the matrix. There is no dashboard
screen for it (#3803): the matrix describes the product's portability
claims, not the state of the machine you are on, so there is nothing on a
page to observe or act on.

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
- Runtime data the ops layer needs (the Compose file, its
  config/provisioning templates, launchd/systemd unit templates, and a
  handful of helper scripts) ships inside the installed Python package
  under `nyxgpt.resources`, resolved via `importlib.resources` -- not
  relative to the repo checkout. `nyxgpt ops install` copies that packaged
  tree into `~/.nyxGPT` once per run (`_sync_packaged_resources`); every
  other step reads from that fixed, writable location afterwards. This is
  what lets `nyxgpt ops install`/`up` work identically whether nyxGPT is
  running from a source checkout (`pip install -e .`) or an installed,
  non-editable package with no repo present at all (#3621). A few
  genuinely repo-checkout-dependent operations -- building distributable
  artifacts from source, the Terraform local deploy path (`.tf` files on
  disk), the `web/` npm project, and the `--dev` image builds -- still
  resolve paths relative to the checkout; see
  `tests/unit/test_repo_root_allowlist.py` for the exact, reviewed list. The
  Kubernetes manifests left that list in #3834: they ship as package data
  (`nyxgpt.resources.k8s`) and are synced to `~/.nyxGPT/k8s` like everything
  else above, which is what makes `--kubernetes` runnable with no checkout.
- The native `api`/`web` services are built from the same
  `nyxgpt-api-<version>`/`nyxgpt-web-<version>` source tarballs either way:
  vendored from the checkout when there is one, downloaded from that
  version's published release assets when there isn't -- its own release for
  a candidate, its `<version>-homebrew` release for a stable version
  ([why](homebrew.md#where-the-tarballs-are-published)); macOS reaches the
  same artifacts through the [remote Homebrew tap](homebrew.md#remote-tap).
  So an artifact install installs the services without ever needing a
  checkout-shaped path (#3759) -- see [systemd.md](systemd.md#installing-the-services).
