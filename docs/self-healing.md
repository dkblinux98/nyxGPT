# Self-Healing (Docker Compose stack)

The self-heal watchdog is the "self-heal" pillar of the local DevOps/SRE
capstone (#3160): it watches every component of the [Docker Compose
stack](docker-compose.md) and automatically restarts anything unhealthy or
stopped, so a killed or crashed container recovers without an operator
running `docker restart` by hand.

It's implemented in `src/nyxgpt/self_heal.py` and runs as a background
thread inside the `api` container's FastAPI process — the same process that
already hosts the [blue/green](api.md#deployment-bluegreen) and
[canary](api.md#canary-deployment) deployment logic.

## How it works

1. Every `check_interval_seconds` (default 15s), the watchdog runs
   `docker compose -f docker-compose.yml ps -a --format json` to list every
   container the Compose project has created — the core services
   (`ollama`, `cassandra`, `api`, `web`) plus any of the opt-in
   `monitoring`/`logging`/`tracing`/`errors` profiles that happen to be up.
   A profile that was never started simply doesn't appear; it isn't treated
   as "down".
2. A component is **healthy** when its Compose `State` is `running` and its
   `Health` is either `healthy` or empty (no healthcheck configured — see
   [Container healthchecks](#container-healthchecks) below for which
   services have one).
3. Anything unhealthy runs `docker compose restart <service>`, then records
   a heal event (service, reason, restart count, success/failure) to
   `~/.nyxGPT/self_heal_state.json`.
4. **Backoff and giving up**: a component won't be restarted again within
   `backoff_seconds` (default 30s) of its last restart attempt, and after
   `max_consecutive_restarts` (default 5) consecutive attempts the watchdog
   stops touching it automatically — a component that keeps failing needs a
   human to look at it, not an infinite restart loop. The counter resets to
   0 the next time the component is observed healthy.
5. One-shot services (`glitchtip-migrate`, which runs a DB migration and is
   expected to exit 0 and stay exited) are never treated as "down".

## Turning it on

The watchdog thread always runs once the API starts, but it only takes
action when **enabled** — controlled at runtime, not by editing
`config.ini` and restarting:

- **Dashboard**: `/admin/self-heal` has an "Enable auto-heal" toggle, a
  "Heal now" button per component (and one for "heal everything
  unhealthy"), and a recent-events log.
- **CLI**: `nyxgpt self-heal status` / `enable` / `disable` / `heal
  [--service NAME]`.
- **API**: `GET /api/v1/self-heal/status`, `POST
  /api/v1/self-heal/toggle`, `POST /api/v1/self-heal/heal` — see
  [api.md](api.md#self-heal-watchdog).

`[self_heal] enabled` in `config.ini` (default `false`) only seeds the
*initial* state on a fresh install (`~/.nyxGPT/self_heal_state.json` doesn't
exist yet); once that file exists, the dashboard/CLI/API toggle is the
source of truth and config.ini is no longer consulted.

## Container healthchecks

Self-heal can only tell "unhealthy" from "healthy" for containers that have
a Docker `HEALTHCHECK`. These were verified directly against the actual
images (checking for a shell and `wget`/`curl`/`redis-cli`/`python3`, then
running each container and confirming the probe succeeds) rather than
assumed:

| Service | Healthcheck | Probe |
|---|---|---|
| `ollama`, `cassandra`, `api` | pre-existing | (unchanged by this work) |
| `web` | added | `wget --spider http://127.0.0.1:3000/` |
| `prometheus` | added | `wget --spider http://127.0.0.1:9090/-/healthy` |
| `grafana` | added | `wget --spider http://127.0.0.1:3000/api/health` |
| `jaeger` | added | `wget --spider http://127.0.0.1:16686/` |
| `glitchtip-redis` | added | `redis-cli ping` |
| `glitchtip` | added | Python TCP connect to `127.0.0.1:8080` |
| `glitchtip-postgres` | pre-existing | `pg_isready` |
| `loki`, `promtail`, `otel-collector`, `glitchtip-worker` | **none** | see below |

`loki`, `otel-collector`, and `glitchtip` ship images built without a
shell (`opentelemetry-collector-contrib` is FROM-scratch; `loki`'s image
has no `sh`), and `promtail`/`glitchtip-worker` have a shell but no
`wget`/`curl`/`python` to probe an HTTP endpoint with — there is no
`CMD-SHELL` healthcheck possible for them. Self-heal still detects these
four going to a non-`running` state (a crash) via Compose's `State` field;
it just can't distinguish "running but stuck" for them the way it can for
the services above.

## Known limitation: healing the `api` container itself

The watchdog runs *inside* the `api` container. If `api` is killed
(`docker kill` / `docker compose kill api` — not a graceful `docker compose
stop`), the watchdog thread dies with it, so nothing inside the stack
restarts `api` automatically. Docker's own `restart: unless-stopped` policy
does **not** cover this case either: Docker deliberately treats an explicit
`kill`/`stop` as intentional and won't auto-restart the container for it
(confirmed empirically — `restart: unless-stopped` recovers a crash/OOM
fine, but not an explicit kill).

In practice this means: self-heal fully covers `ollama`, `cassandra`,
`web`, and every opt-in-profile container being killed or crashing, but a
killed `api` container currently needs `docker compose up -d api` run by
an operator (or a future external supervisor with access to the Docker
socket, which is out of scope for this pass — see the open follow-up items
in the #3160 issue). The [smoke test](#smoke-test) below exercises and
reports on this explicitly rather than silently glossing over it.

## Smoke test

`scripts/smoke-test.sh` is the documented end-to-end smoke test: it brings
the stack up, verifies chat and RAG work through the API, kills each core
component one at a time and watches the dashboard's underlying API confirm
recovery, then tears the stack down.

```bash
./scripts/smoke-test.sh              # full run: deploy, verify, kill/heal every component, teardown
./scripts/smoke-test.sh --skip-deploy  # stack is already up; skip straight to verify/kill/heal
./scripts/smoke-test.sh --keep-up      # leave the stack running after the test for manual poking
```

It requires an Ollama model to already be pulled (`docker compose exec
ollama ollama pull <model>` — see [docker-compose.md](docker-compose.md))
since the chat verification step performs a real inference call. For
`ollama`, `cassandra`, and `web`, it asserts the watchdog restores the
component to healthy automatically. For `api`, per the limitation above, it
kills the container, confirms self-heal does *not* recover it (which is
expected, not a bug), and then brings it back with `docker compose up -d
api` itself before continuing — this is the one step in the whole test
that isn't hands-off.
