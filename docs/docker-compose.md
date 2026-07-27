# Docker Compose (full stack)

`docker-compose.yml` brings up every component of nyxGPT with one command:
the FastAPI backend, the Next.js web UI, Ollama, and Cassandra (for RAG).
This is an alternative **cloud/server** deployment path to the native-mode
Homebrew / `nyxgpt ops` workflow described in [ops.md](ops.md) and the
API-only [Kubernetes deployment](kubernetes.md) — useful when you want the
whole stack running in containers with a single command, e.g. for
evaluation, a non-macOS host, or a server deployment.

Don't mix the two on the same machine: in native mode, `api`/`web`/`ollama`
run natively and only Cassandra runs in a container (see
[`nyxgpt ops install`](ops.md#nyxgpt-ops-install)), so bringing up this
Compose stack's app-tier services (`api`/`web`/`ollama`/`cassandra`)
alongside a native install collides on the same ports. `nyxgpt ops install`
detects and stops any of those Compose containers it finds running.

## Services

| Service     | Image             | Purpose                                   | Host port (default) |
|-------------|-------------------|--------------------------------------------|----------------------|
| `ollama`    | `ollama/ollama:0.32.4`   | Local LLM inference                  | `11434`              |
| `cassandra` | `cassandra:5.0.8`   | Vector store for RAG                     | `9042`               |
| `api`       | built from `Dockerfile`     | FastAPI backend (`nyxgpt-api`)  | `8000`               |
| `web`       | built from `web/Dockerfile` | Next.js web UI (`nyxgpt-web`)   | `3000`               |
| `prometheus` <sup>*</sup> | `prom/prometheus:v3.13.1` | Scrapes the API's `/metrics` endpoint, evaluates alerting rules | `9090` |
| `grafana` <sup>*</sup> | `grafana/grafana:13.1.1` | Pre-provisioned dashboards (system overview, RAG performance, API metrics, logs explorer) | `3001` |
| `loki` <sup>§</sup> | `grafana/loki:3.6.13` | Log storage + search API, with retention | — |
| `promtail` <sup>§</sup> | `grafana/promtail:3.6.11` | Tails logs from both deployment modes and ships to Loki | — |
| `otel-collector` <sup>†</sup> | `otel/opentelemetry-collector-contrib:0.157.0` | Receives OTLP spans from the API, forwards to Jaeger | `4318` (HTTP), `4317` (gRPC) |
| `jaeger` <sup>†</sup> | `jaegertracing/all-in-one:1.76.0` | Trace storage + UI      | `16686`              |
| `glitchtip` <sup>‡</sup> | `glitchtip/glitchtip:6.2.0` | Self-hosted error tracker UI + ingest | `8080` |
| `glitchtip-worker` <sup>‡</sup> | `glitchtip/glitchtip:6.2.0` | GlitchTip Celery worker/beat | —                    |
| `glitchtip-migrate` <sup>‡</sup> | `glitchtip/glitchtip:6.2.0` | One-shot GlitchTip DB migration | —              |
| `glitchtip-postgres` <sup>‡</sup> | `postgres:16.14` | GlitchTip's database         | —                    |
| `glitchtip-redis` <sup>‡</sup> | `redis:7.4.9-alpine` | GlitchTip's queue/cache          | —                    |

<sup>*</sup> Started via the `monitoring` Compose profile, automatically by
`nyxgpt ops install` — see [Monitoring Dashboards](#monitoring-dashboards)
below.

<sup>†</sup> Started via the `tracing` Compose profile, automatically by
`nyxgpt ops install` — see [Distributed Tracing](#distributed-tracing)
below.

<sup>‡</sup> Started via the `errors` Compose profile, automatically by
`nyxgpt ops install`, which also auto-provisions the GlitchTip
project/DSN that makes error reporting actually active — see
[Error Tracking](#error-tracking) below.

<sup>§</sup> Started via the `logging` Compose profile, automatically by
`nyxgpt ops install` — see [Log Aggregation](#log-aggregation) below.

All four profiles above are part of the observability stack started by
`nyxgpt ops install`/`nyxgpt ops observability` (see
[ops.md](ops.md#nyxgpt-ops-observability)) — pass `nyxgpt ops install
--skip-observability` if you'd rather they stayed off.

All services share a single bridge network (`nyxgpt`) and reach each other by
service name (e.g. the API talks to Ollama at `http://ollama:11434` and to
Cassandra at `cassandra:9042` — see `docker/config.docker.ini`).

## Image Pinning

Every third-party image above is pinned to a specific version (major.minor at
minimum, exact patch where available) — no `:latest` and no bare major-only
tag (e.g. `postgres:16` or `redis:7-alpine`), since either can silently pull
different software on a different day, and a bad upstream `latest` push can
break the stack with nothing in git explaining the change. First-party images
built from this repo (`nyxgpt-api`, `nyxgpt-web`) are out of scope — their
version is whatever commit they're built from.

`ollama` and `cassandra` also run in [Terraform](terraform.md) and (Cassandra
only) via `nyxgpt ops` (`CASSANDRA_IMAGE` in `src/nyxgpt/ops.py`) — each of
those three definitions carries a comment pointing at the other two, and all
three must be bumped together so the pinned version never diverges between
deployment paths.

To bump a pinned version: pick the new tag, update it in every file listed
above for that image (cross-reference comments call out the other files),
update the version in this table, then verify with `nyxgpt ops install` (for
`ollama`/`cassandra`) or `nyxgpt ops observability` (for the monitoring/
logging/tracing/errors profiles) before committing.
`tests/unit/test_image_pins.py` fails the build if `:latest` (or an untagged
image reference) reappears in `docker-compose.yml`, `terraform/main.tf`, or
`src/nyxgpt/ops.py`.

## Volumes

Container data is **not** kept in opaque named Docker volumes -- every
component bind-mounts a plainly labeled directory under `$HOME/.nyxGPT/volumes/`
on the host (see issue #3346), so it's visible on the host filesystem,
attributable to its component, and (for the three "Shared" rows below) reused
as-is by every deployment mode that mounts it rather than each mode keeping
its own duplicate copy:

| Host directory                         | Container path                | Component  | Shared across |
|-----------------------------------------|--------------------------------|------------|---------------|
| `~/.nyxGPT/volumes/ollama`               | `/root/.ollama`                | Pulled Ollama models | Compose + Terraform |
| `~/.nyxGPT/volumes/cassandra`            | `/var/lib/cassandra`           | Cassandra's data directory (chats/RAG vectors) | Compose + Terraform + native `nyxgpt ops install` |
| `~/.nyxGPT/volumes/nyxgpt-data`          | `/root/.nyxGPT` (in `api`)     | The containerized api's chat sessions/vector store/logs | Compose + Terraform |
| `~/.nyxGPT/volumes/prometheus`           | `/prometheus`                  | `monitoring` profile's metrics | Compose only today |
| `~/.nyxGPT/volumes/grafana`              | `/var/lib/grafana`             | `monitoring` profile's dashboard state | Compose only today |
| `~/.nyxGPT/volumes/loki`                 | `/loki`                        | `logging` profile's indexed/stored log chunks | Compose only today |
| `~/.nyxGPT/volumes/glitchtip-postgres`   | `/var/lib/postgresql/data`     | GlitchTip's database | Compose only today |
| `~/.nyxGPT/volumes/glitchtip-uploads`    | `/code/uploads`                | GlitchTip's uploaded attachments | Compose only today |

Native mode doesn't touch `ollama`/`nyxgpt-data` here at all: native Ollama is
a plain Homebrew service with its own model store, and the native api process
uses `~/.nyxGPT` directly (as itself, not `/root/.nyxGPT` in a container) --
those are unrelated to this table. Cassandra is the one component native mode
*does* share: `nyxgpt ops install`'s Cassandra container
(`_ensure_cassandra_container` in `src/nyxgpt/ops.py`) binds the exact same
`~/.nyxGPT/volumes/cassandra` directory, so chats survive switching between
native, Compose, and Terraform. It refuses to start if the Terraform-managed
Cassandra container is already running against that same directory (two
writers on one Cassandra data directory would corrupt it) -- run
`nyxgpt ops down --terraform` first if you hit that.

**Backup guidance:** backing up `~/.nyxGPT` now captures all container state
in addition to the native config/logs already there -- there's nothing left
in Docker's own storage area to separately back up.

**Migrating from before #3346:** if you're upgrading from a version that used
named Docker volumes (`ollama_data`, `cassandra_data`, `nyxgpt_data`, ...),
run `nyxgpt ops migrate-volumes` **once, before your first `docker compose up`
(or `up --build`) after upgrading**, to copy that data into the layout above
(the old volumes are removed only after a successful copy). This also runs
automatically as the first step of `nyxgpt ops install` (native or
`--terraform --local`) -- `migrate-volumes` is there for Compose-only users
who never run `install`.

Order matters here: bringing up the new bind-mount-based Compose file
*before* running `migrate-volumes` lets Cassandra/Ollama populate the new
(empty) `~/.nyxGPT/volumes/...` directories with fresh data in seconds, and
`migrate-volumes` then refuses to guess whether that's "already migrated" or
"real new data" -- it fails loudly instead of silently discarding your old
volume. If you see a `refusing to auto-migrate` message, your pre-upgrade
data is still intact in the old named volume; follow the message's steps to
merge it in by hand, then remove the old volume to clear the warning.

Run `nyxgpt ops down --volumes --yes-really` to discard all persisted state,
including Cassandra data and pulled models -- see
[`nyxgpt ops down`](ops.md#nyxgpt-ops-down). Never run a raw
`docker compose down -v`; besides also stopping native services and
requiring the explicit `--yes-really` confirmation, a raw `-v` no longer even
deletes anything here (there are no named volumes left for Compose to
manage) -- the wrapper explicitly removes the `~/.nyxGPT/volumes/` directories
for the torn-down services instead.

## Quickstart

`~/.nyxGPT/config.ini` (generated by `nyxgpt wizard`) is the single source of
truth for secrets like the API key and Grafana's admin password — `.env` is
a generated artifact derived from it, not something you maintain by hand:

`docker/config.docker.ini` (the api container's mounted config) is likewise a
generated, per-machine artifact: it is git-ignored and seeded from the tracked
`docker/config.docker.ini.example` template. `nyxgpt ops env-sync` creates it
below (as does `nyxgpt ops install`), so you don't copy it by hand.

```bash
cp .env.example .env
nyxgpt wizard      # generates config.ini with a fresh api_key + Grafana password
nyxgpt ops env-sync  # derives .env from config.ini + seeds docker/config.docker.ini

# Upgrading from before #3346 (named Docker volumes)? Run this first -- see
# "Migrating from before #3346" above for why order matters here.
nyxgpt ops migrate-volumes

docker compose up --build
```

(For a quick one-off evaluation without installing the `nyxgpt` CLI locally,
you can instead edit `.env` directly and set a real `NYXGPT_AUTH_API_KEY` —
but then config.ini and `.env` are no longer in sync, so prefer the flow
above for anything longer-lived.)

First boot takes a few minutes: Cassandra needs time to become healthy and
Ollama needs models. Once `ollama` is up, pull the default chat model and
the embedding model RAG uses for `/api/embed` (this only needs to be done
once — both are stored in `~/.nyxGPT/volumes/ollama`). The chat model does
*not* serve embeddings, so both pulls are required for chat with RAG context
to work:

```bash
docker compose exec ollama ollama pull qwen2.5:0.5b
docker compose exec ollama ollama pull nomic-embed-text
```

Then verify:

```bash
curl -H "X-API-Key: <NYXGPT_AUTH_API_KEY from .env>" http://127.0.0.1:8000/health
```

Open the web UI at [http://localhost:3000](http://localhost:3000).

To stop the stack later, use `nyxgpt ops stop <target>` (stops one
component, containers preserved) or `nyxgpt ops down` (full teardown) --
see [`nyxgpt ops stop`](ops.md#nyxgpt-ops-stop) and
[`nyxgpt ops down`](ops.md#nyxgpt-ops-down). Never run a raw
`docker compose stop`/`down`.

## Configuration

- **API config**: `docker/config.docker.ini` is mounted read-only into the
  `api` container at `/etc/nyxgpt/config/config.ini` and copied to
  `~/.nyxGPT/config.ini` by `docker/entrypoint.sh` on startup, which also
  merges in `NYXGPT_AUTH_API_KEY`. Edit this file for persistent settings
  (default model, RAG thresholds, etc.) — see [configuration.md](configuration.md)
  for the full option reference.
- **Environment**: all other tunables (ports, CORS origins, the web UI's API
  base URL, image tags) are set via `.env` — see `.env.example` for the full
  list. The two secret values in `.env` (`NYXGPT_AUTH_API_KEY`,
  `GRAFANA_ADMIN_PASSWORD`) are derived from `~/.nyxGPT/config.ini` via
  `nyxgpt ops env-sync` rather than set independently — see
  [security.md](security.md#api-key-management).
- **Web UI → API URL**: there are two distinct settings, and both are wired
  up by `docker-compose.yml` — you shouldn't need to touch either for a
  standard Compose deploy:
  - `NEXT_PUBLIC_API_BASE_URL` is inlined into the web UI's *browser* bundle
    at build time (Next.js `NEXT_PUBLIC_*` semantics), so changing it
    requires rebuilding: `docker compose build web`.
  - `NYXGPT_API_BASE_URL` is a runtime env var read *server-side* by the
    `web` container's Next.js API proxy routes (see
    `web/src/lib/apiProxy.ts`) — it's set to `http://api:8000` so those
    routes reach the `api` service over the compose network. The same
    proxy routes also forward `NYXGPT_AUTH_API_KEY` as the `X-API-Key`
    header on every backend call, since `docker/config.docker.ini` enables
    auth for exactly this reason (see the `[auth]` section).

## Network binding

Every port published above is bound to `NYXGPT_BIND_ADDR` (`.env`, defaults
to `127.0.0.1`), so the stack is reachable from this machine only — matching
the [local-first](../product_management/VISION.md) posture of a native install. Without this,
Docker publishes on `0.0.0.0` and every service becomes reachable from
anyone on the same LAN, regardless of the `[api] host` / `[web] host`
settings in `config.ini` (those only control the bind *inside* the
container).

If you need the stack reachable from other machines, prefer an SSH tunnel or
a TLS-terminating reverse proxy in front of the loopback-bound ports (see
[security.md#network-security](security.md#network-security)) over widening
`NYXGPT_BIND_ADDR`. If you do widen it (e.g. `NYXGPT_BIND_ADDR=0.0.0.0` in
`.env`), first set `[auth] enabled = true` in `~/.nyxGPT/config.ini` and
re-run `nyxgpt ops env-sync` — otherwise the full API, including the
filesystem tools endpoints, is reachable with no credential.

## Disabling RAG / Cassandra

RAG is enabled by default in `docker/config.docker.ini` since Cassandra ships
as part of this stack. To run without RAG, set `enable_chat_context = false`
under `[rag]` in `docker/config.docker.ini`; you can also remove the
`cassandra` service and its `depends_on` entry under `api` in
`docker-compose.yml` if you don't want the container running at all.

Chat works immediately on a fresh install even though the Cassandra keyspace
doesn't exist yet: RAG retrieval degrades to empty context instead of
erroring, and the keyspace/table are created automatically the first time
you ingest a document (`POST /rag/ingest` or uploading a file from the web
UI) — no manual bootstrap step is required.

## Monitoring Dashboards

Grafana dashboards are local-only — metrics never leave this machine. It
ships as a separate `monitoring` Compose profile, started automatically by
`nyxgpt ops install` (part of the observability stack, see
[ops.md](ops.md#nyxgpt-ops-observability)). To start it on its own or
re-run it later, use:

```bash
nyxgpt ops observability
```

Never run `docker compose --profile monitoring up` directly — the wrapper
above is the supported way to start this profile (see
[ops.md](ops.md#nyxgpt-ops-observability)); pass `nyxgpt ops install
--skip-observability` if you'd rather it stayed off.

This starts `prometheus` (scrapes the API's [`/metrics`](api.md#get-metrics)
endpoint every 15s using `docker/prometheus.yml`, and evaluates the alerting
rules in `docker/prometheus-alerts.yml`) and `grafana` (UI at
[http://localhost:3001](http://localhost:3001), also linked from the
SRE/admin dashboard's Resource Usage step). Grafana is pre-provisioned on
first boot with a Prometheus datasource and eight dashboards under
`docker/grafana/dashboards` (a ninth, Loki-only dashboard is listed under
[Log Aggregation](#log-aggregation) below):

- **System Overview** — request rate, error rate, request latency
  (p50/p95/p99), total requests, and API up/down status.
- **RAG Performance** — RAG query rate/totals by source, RAG ingest rate/totals
  by source and outcome, chat request rate by streaming mode, and chat
  requests by model.
- **API Metrics** — top request paths by rate, requests by method, p95
  latency by path, errors by path, and requests by status code.
- **Resource Usage** — process memory (RSS) and CPU usage, batch queue
  depth, cache hit rate and request volume by cache, and rate-limit
  rejections by path.
- **Self-Healing** — live unhealthy-component count, restarts in the last
  24h, consecutive-restart count per service (backoff state), restart rate
  by service/outcome, time since last recovery, and a Loki-backed
  restart/recovery event timeline — see
  [self-healing.md](self-healing.md#observability-logs-metrics-and-the-self-healing-dashboard).
- **Blue-Green Deployment** — active color, switch/rollback counts (24h),
  switches by direction/outcome, rollbacks by outcome, and a Loki-backed
  switch/rollback event timeline — see
  [kubernetes.md](kubernetes.md#deploy-logging--metrics).
- **Canary Rollout** — rollout active/idle, live traffic split, rollback
  count (24h), evaluation results, lifecycle events by action/outcome, and a
  Loki-backed start/promote/rollback event timeline — see
  [kubernetes.md](kubernetes.md#canary-logging--metrics).

Every dashboard above (plus Logs Explorer and Operational Logs — see
[Log Aggregation](#log-aggregation)) is reachable from one place: the
`/admin/observability` **SRE Overview** page, whose **Dashboard Catalog**
deep-links directly into each Grafana dashboard alongside curated Jaeger
trace views and the GlitchTip error tracker. The catalog uses the same
tile grid as the admin dashboard's quick-nav (`DASHBOARD_GROUPS` in
`web/src/components/DashboardCatalog.tsx`): each tile shows the dashboard
name plus a one-line description of what it charts, echoed as a hover
tooltip, with no arrow decoration and same-tab navigation, grouped under
App functionality / Self-healing & deployment / Logs.

Log in with username `admin` and the password in `~/.nyxGPT/config.ini`'s
`[monitoring] grafana_admin_password` (auto-generated by `nyxgpt wizard`).
Run `nyxgpt ops env-sync` before starting this profile so `.env`'s
`GRAFANA_ADMIN_PASSWORD` picks up that value — see
[security.md](security.md#api-key-management).

`[monitoring] enabled = true` is already set in `docker/config.docker.ini`
so the SRE/admin dashboard's "Monitoring Dashboards" card shows the Grafana
link as soon as the profile is up -- see
[configuration.md](configuration.md#monitoring-section) and
[api.md](api.md#monitoring-dashboards). If you changed `GRAFANA_UI_PORT` in
`.env`, update `grafana_ui_url` in config to match, or the link will point
at the wrong port.

Alerting rules (`docker/prometheus-alerts.yml`) evaluate continuously and
show their state (inactive/pending/firing) on Prometheus's own Alerts page at
[http://localhost:9090/alerts](http://localhost:9090/alerts) if the API
becomes unreachable, its 5xx error rate exceeds 5%, or its p95 latency
exceeds 2 seconds. This is local-only rule evaluation — no Alertmanager or
external notification channel (email/Slack/PagerDuty) is deployed by
default.

## Log Aggregation

Centralized log search (Loki + promtail) is local-only — logs never leave
this machine. It's a reduced-footprint alternative to a full ELK stack,
sized for a single-workstation, local-first system. It ships as a separate
`logging` Compose profile, started automatically by `nyxgpt ops install`
alongside `monitoring` (part of the observability stack, see
[ops.md](ops.md#nyxgpt-ops-observability)):

```bash
nyxgpt ops observability
```

This starts `promtail` (ships logs to Loki using `docker/promtail-config.yml`)
and `loki` (stores and indexes log lines using `docker/loki-config.yml`,
which sets a 14-day retention policy via the compactor).

promtail is always a Compose container, but the core app (`api`,
self-heal, `nyxgpt ops`) can be running either natively or as Compose
services (see [ops.md](ops.md)) -- and those two modes write logs to two
different places, so promtail is wired to tail both:

- **Compose mode**: the `api` container writes to `/root/.nyxGPT`, backed
  by `~/.nyxGPT/volumes/nyxgpt-data` (see [Volumes](#volumes) above).
  promtail mounts that same host directory read-only at `/var/log/nyxgpt`.
- **Native mode** (the primary local path): `api`/self-heal/`nyxgpt ops`
  run on the host and write to `~/.nyxGPT/logs` directly -- a separate plain
  host directory, **not** part of `~/.nyxGPT/volumes/nyxgpt-data`. promtail
  separately bind-mounts that host directory read-only at
  `/var/log/nyxgpt-native`.

`docker/promtail-config.yml` scrapes both paths under the same `job`
label, so log streams from either mode are indistinguishable in Grafana.
If you're running native mode and don't see logs in Grafana, run `nyxgpt
ops doctor` first -- it flags a missing native-log bind mount by
inspecting the *running* promtail container's mounts (`docker inspect`,
not just the compose file's text), catching a container that was created
before a `docker-compose.yml` edit rather than leaving it to a
silently-empty dashboard. `nyxgpt ops doctor` also reports a per-component
log volume for the last 24h (via Loki), so an idle curated component
(e.g. deploy/canary on a native install that's never run a k8s operation)
isn't mistaken for a broken pipeline.

nyxgpt logs timestamps in UTC (see `nyxgpt.logging.configure_logging`) and
`docker/promtail-config.yml`'s `timestamp` stage is told the same
(`location: UTC`) -- both sides must agree, or a non-UTC host's promtail
would parse a local timestamp as if it were already UTC and shift every
line hours into the past, outside the curated Explore links' `now-1h`
window. promtail's extraction regex also tolerates the log line's optional
comma-millisecond suffix and `[request_id]` bracket, so both nyxgpt's
canonical format and older/varying line shapes still yield `level`/`logger`
labels.

promtail extracts both `level` and `logger` (the Python module, e.g.
`nyxgpt.self_heal`) as Loki labels from the API's log format, so log
streams can be filtered per-component. Search logs in the Grafana
instance from the `monitoring` profile (also required — start both
profiles together), which is pre-provisioned with a Loki datasource and
two dashboards under `docker/grafana/dashboards`:

- **Logs Explorer** — log volume by level, plus a filterable live log view.
- **Operational Logs** — curated saved queries for the key operational
  streams: self-heal events, deploy events, canary events, chat errors
  (`ERROR`/`CRITICAL`), and a per-component filter covering every traced
  module. The same query text is also returned by `GET
  /api/v1/log-aggregation` (see
  [api.md](api.md#log-aggregation)) and shown on the SRE/admin dashboard,
  so it's copy-pasteable into Grafana Explore without opening the
  dashboard.

`nyxgpt ops observability` starts both the `monitoring` and `logging`
profiles together (Grafana needs the `logging` profile's Loki instance for
its datasource) — see [ops.md](ops.md#nyxgpt-ops-observability).

`[log_aggregation] enabled = true` is already set in
`docker/config.docker.ini` so the SRE/admin dashboard's "Log Aggregation"
card shows the Grafana Explore link as soon as the profile is up -- see
[configuration.md](configuration.md#log_aggregation-section) and
[api.md](api.md#log-aggregation).

## Distributed Tracing

Distributed tracing (OpenTelemetry) is local-only — no spans are ever sent
to an external/cloud endpoint. It ships as a separate `tracing` Compose
profile, started automatically by `nyxgpt ops install` (part of the
observability stack, see [ops.md](ops.md#nyxgpt-ops-observability)):

```bash
nyxgpt ops observability
```

This starts `otel-collector` (receives spans from the API over OTLP/HTTP)
and `jaeger` (stores traces and serves the UI at
[http://localhost:16686](http://localhost:16686), also linked from the
SRE/admin dashboard's Resource Usage step).

`[tracing] enabled = true` is already set in `docker/config.docker.ini` so
the API actually emits spans as soon as the profile is up — see
[configuration.md](configuration.md#tracing-section) and
[api.md](api.md#distributed-tracing).

`otel-collector`'s OTLP receivers (`4318` HTTP, `4317` gRPC) are published
to the host, bound to `127.0.0.1` per [security.md](security.md#network-security)
-- not just reachable over the internal Compose network. This matters even
though the app tier's default deployment is native (see [ops.md](ops.md)):
in native mode the api process runs on the host, not in this Compose
network, so it can only reach the collector via the published host port
(`http://localhost:4318/v1/traces`, the `[tracing] otlp_endpoint` default in
`example.config.ini`). Without that host port, spans are silently dropped
and Jaeger stays empty while the SRE dashboard's Distributed Tracing panel
still reports "active" -- `nyxgpt ops doctor` checks that something is
actually listening on the configured `otlp_endpoint` and flags the gap.

## Error Tracking

Self-hosted error tracking (GlitchTip) is local-only — no exception data is
ever sent to Sentry's own SaaS. It ships as a separate `errors` Compose
profile, started automatically by `nyxgpt ops install` alongside the rest
of the observability stack (see
[ops.md](ops.md#nyxgpt-ops-observability)). `nyxgpt ops install` then runs
`nyxgpt ops glitchtip-init` (see
[ops.md](ops.md#nyxgpt-ops-glitchtip-init)), which auto-provisions its
admin user, organization, project, and DSN with no manual sign-in step,
flipping `[error_tracking] enabled = true` and filling in `dsn` once it
succeeds. If you need to (re-)start just this profile, or re-run
provisioning on its own:

```bash
nyxgpt ops observability
nyxgpt ops glitchtip-init
```

This starts `glitchtip-postgres`, `glitchtip-redis`, a one-shot
`glitchtip-migrate` job, the `glitchtip` web/API service (UI at
[http://localhost:8080](http://localhost:8080), also linked from the
SRE/admin dashboard's Resource Usage step), and `glitchtip-worker`.

Set a real `GLITCHTIP_SECRET_KEY` in `.env` before running this profile
(see `.env.example`). `glitchtip-init` is idempotent (safe to re-run any
time) and no-ops with a clear message if the `glitchtip` container isn't
up/healthy yet. Its admin login lands in `~/.nyxGPT/config.ini`'s
`[error_tracking] admin_email`/`admin_password` (generated if left blank,
chmod 600); the DSN itself is also written into
`docker/config.docker.ini` since it's a public key, safe to store there.
nyxGPT reports via the **Python** `sentry_sdk`, not the Node.js
instructions GlitchTip's own onboarding screen shows — see
[configuration.md](configuration.md#error_tracking-section) and
[api.md](api.md#error-tracking) for the full guided flow, including how to
configure it by hand instead.

## Self-Healing

Every core service (`ollama`, `cassandra`, `api`, `web`) and most opt-in
profile services have Docker healthchecks and `restart: unless-stopped`.
On top of that, a self-heal watchdog inside the `api` container watches
`docker compose ps` and automatically restarts anything unhealthy or
stopped — see [self-healing.md](self-healing.md) for the full design, how
to turn it on (`/admin/self-heal`, `nyxgpt self-heal`, or the API), and
`scripts/smoke-test.sh`, the documented end-to-end smoke test (deploy →
verify chat/RAG → kill each component → observe auto-heal → teardown).

## Blue/Green and Canary Deployment

`/admin/deploy` and `/admin/canary` (blue/green cutover and weighted canary
rollout — see [kubernetes.md](kubernetes.md#bluegreen-deployment)) are not
operable under docker-compose: there is no Kubernetes cluster here for
`kubectl` to reach, and the compose stack runs a single `api` container
rather than the multiple colored/tracked Deployments those features cut
traffic between. The dashboards detect this deployment mode and show a
banner explaining that instead of a per-color `kubectl not found` error.
Use the [Kubernetes deployment](kubernetes.md) to operate these features —
the same `api` image ships `kubectl` and the RBAC it needs there.

## Rebuilding after code changes

```bash
docker compose build api web
docker compose up -d
```
