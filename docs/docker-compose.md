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
| `~/.nyxGPT/volumes/ollama`               | `/root/.ollama`                | Pulled Ollama models | Compose + Terraform + native `nyxgpt ops install` |
| `~/.nyxGPT/volumes/cassandra`            | `/var/lib/cassandra`           | Cassandra's data directory (chats/RAG vectors) | Compose + Terraform + native `nyxgpt ops install` |
| `~/.nyxGPT/volumes/nyxgpt-data`          | `/root/.nyxGPT` (in `api`)     | The containerized api's chat sessions/vector store/logs | Compose + Terraform |
| `~/.nyxGPT/volumes/nyxgpt-web-logs`      | `/var/log/nyxgpt-web` (in `web`) | The containerized web tier's structured logs (#3430) | Compose only today |
| `~/.nyxGPT/volumes/prometheus`           | `/prometheus`                  | `monitoring` profile's metrics | Compose only today |
| `~/.nyxGPT/volumes/grafana`              | `/var/lib/grafana`             | `monitoring` profile's dashboard state | Compose only today |
| `~/.nyxGPT/volumes/loki`                 | `/loki`                        | `logging` profile's indexed/stored log chunks | Compose only today |
| `~/.nyxGPT/volumes/glitchtip-postgres`   | `/var/lib/postgresql/data`     | GlitchTip's database | Compose only today |
| `~/.nyxGPT/volumes/glitchtip-uploads`    | `/code/uploads`                | GlitchTip's uploaded attachments | Compose only today |

Native mode doesn't touch `nyxgpt-data` here at all: the native api process
uses `~/.nyxGPT` directly (as itself, not `/root/.nyxGPT` in a container) --
that's unrelated to this table. `ollama` and `cassandra` are the two
components native mode *does* share (see #3431 for `ollama`): `nyxgpt ops
install` points native Ollama's `OLLAMA_MODELS` env var at
`~/.nyxGPT/volumes/ollama/models` -- the same directory the container stores
models in via this bind mount -- instead of native Ollama's own default
`~/.ollama/models` (see `_ensure_ollama_service` in `src/nyxgpt/ops.py`; set
via `launchctl setenv`, never a symlink). `nyxgpt ops install`'s Cassandra
container (`_ensure_cassandra_container` in `src/nyxgpt/ops.py`) binds the
exact same `~/.nyxGPT/volumes/cassandra` directory. Either way, a model or
chat survives switching between native, Compose, and Terraform. Cassandra
refuses to start if the Terraform-managed Cassandra container is already
running against that same directory (two writers on one Cassandra data
directory would corrupt it) -- run `nyxgpt ops down --terraform` first if you
hit that.

**Kubernetes** is out of scope for this unification (its own PVC, no
bind-mount to the host's home directory possible) -- tracked separately, see
issue #3431.

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

**Migrating models from before #3431:** if you'd already pulled models
natively (into Ollama's own default `~/.ollama/models`) before native mode
started sharing `~/.nyxGPT/volumes/ollama/models`, `nyxgpt ops install`
automatically merges anything under `~/.ollama/models` that isn't already in
the shared store the first time it runs (it never overwrites a file already
present at the destination). The old `~/.ollama/models` files are left in
place afterwards -- safe to remove by hand once you've confirmed the shared
store has everything you need.

Run `nyxgpt ops down --volumes --yes-really` to discard all persisted state,
including Cassandra data and pulled models -- see
[`nyxgpt ops down`](ops.md#nyxgpt-ops-down). Never run a raw
`docker compose down -v`; besides also stopping native services and
requiring the explicit `--yes-really` confirmation, a raw `-v` no longer even
deletes anything here (there are no named volumes left for Compose to
manage) -- the wrapper explicitly removes the `~/.nyxGPT/volumes/` directories
for the torn-down services instead.

## Full-stack local deploy = `nyxgpt ops install --terraform --local`

> The supported way to bring up the **full** containerized stack locally is
> `nyxgpt ops install --terraform --local` (or `--kubernetes --local`), which
> orchestrates the api/web/ollama/cassandra containers **and** the observability
> stack on one network, deriving the container config from your real
> `~/.nyxGPT/config.ini`. There is no raw `docker compose up` full-stack path.
> The Compose file here defines the observability profiles (which the terraform
> `--local` install brings up) and is the source those container definitions are
> built from; the raw-Compose notes below are for debugging individual services.

## Quickstart (observability profiles / individual-service debugging)

`~/.nyxGPT/config.ini` (generated by `nyxgpt wizard`) is the single source of
truth. `.env` and `docker/config.docker.ini` (the api container's mounted
config) are both **generated artifacts derived from it** — git-ignored,
never hand-maintained. `nyxgpt ops env-sync` (and `nyxgpt ops install`)
regenerate `.env` from config.ini and derive `docker/config.docker.ini` from it,
rewriting only the container-network endpoints:

```bash
cp .env.example .env
nyxgpt wizard        # generates config.ini with a fresh api_key + Grafana password
nyxgpt ops env-sync  # derives .env AND docker/config.docker.ini from config.ini

# Upgrading from before #3346 (named Docker volumes)? Run this first -- see
# "Migrating from before #3346" above for why order matters here.
nyxgpt ops migrate-volumes
```

After `env-sync` has generated `docker/config.docker.ini`, individual Compose
services can be driven directly for debugging (e.g. `docker compose up -d
grafana`). For the full stack, use the terraform `--local` install above.

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
  - `NYXGPT_CHAT_STREAM_HEADERS_TIMEOUT_MS` (optional, default `300000` — 5
    minutes) overrides how long the `chat/stream` proxy route waits for the
    api to send response headers before giving up. Raise it if a cold local
    model load (first chat after a restart) regularly takes longer than the
    default (#3440).

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

## Grafana Single Pane of Glass

Grafana is the single pane of glass for every SRE signal (#3411) — there is
no in-app SRE Overview page. The Admin Dashboard's **SRE Overview** tile
opens Grafana's **SRE Home** dashboard (`nyxgpt-sre-home`, set as the org
default/home dashboard) in a new browser tab, built from `grafana_ui_url`.
SRE Home is a real overview, not a link list — its content is laid out
top to bottom in a fixed order:

1. **Above the fold: health and metrics graphs** — request rate, 5xx error
   rate, request latency (p50/p95/p99), RAG query/ingest activity, chat
   request rate, resource usage (memory/CPU/cache hit rate), self-heal
   status (unhealthy components, restarts, backoff state), and canary state
   (active/idle, traffic split, rollbacks) — pulled from the same metrics as
   [System Overview, API Metrics, RAG Performance, Resource Usage,
   Self-Healing, and Canary Rollout](#monitoring-dashboards) below. Rather
   than one grouped link list, each deep-dive dashboard's link sits directly
   above the section it deepens (e.g. the System Overview / API Metrics link
   sits above the request-rate/error-rate/latency graphs, the Self-Healing
   link sits above the self-heal stat tiles) — navigation lives next to the
   content it navigates to.
2. **Logs** — a featured, queryless `{job="nyxgpt"}` logs panel rendered
   directly on SRE Home, with its panel title itself carrying a direct link
   ("Open Logs Drilldown") into Grafana's **Logs Drilldown** app
   (`/a/grafana-lokiexplore-app`), pre-filtered to `job=nyxgpt` so the label
   filter never has to be typed by hand. No saved/curated Explore queries
   needed for general log search — Drilldown's own label filtering (`level`,
   `logger`) replaces them. See [Log Aggregation](#log-aggregation) below.
3. **Traces** — a Jaeger datasource is provisioned in Grafana alongside
   Prometheus and Loki, so a live traces panel renders inline; the "Recent
   nyxgpt traces" panel's title carries a link (opens in a new tab) to the
   native Jaeger UI for its own trace search when the inline panel isn't
   enough. See [Distributed Tracing](#distributed-tracing) below.
4. **Errors** — GlitchTip's Sentry-compatible REST API, queried through the
   Infinity datasource plugin, surfaces an open-issues count and a recent-
   errors table as Grafana panels; both panels' titles link (new tab) to
   the GlitchTip UI for issue management. See [Error Tracking](#error-tracking)
   below.

The Prometheus and Jaeger native UIs remain reachable directly (their ports
are still published to the host) — Grafana is where you look first, but
each backing tool's own UI is one click away via a panel-title link on the
section it backs (2026-07-30 owner decision, #3471): Jaeger on the traces
panel, GlitchTip on its two panels, and Prometheus (which backs the whole
health/metrics section rather than one dedicated panel) on the overview
panel heading that section. There's no top-of-dashboard link row. The
dashboard JSON is static, so these links are hardcoded to the same
localhost defaults `nyxgpt.config` falls back to for the in-app pages —
if you change `jaeger_ui_url`/`glitchtip_ui_url`/`prometheus_ui_url` in
config, update `docker/grafana/dashboards/sre-home.json`'s panel links to
match.

### Manual verification: RAG panel legibility at local traffic (#3469)

The RAG panels on SRE Home and the RAG Performance dashboard use
`increase(...[$__rate_interval])` (not a per-second `rate()`) precisely so
a handful of interactive local queries/ingests render as a visible integer
blip instead of vanishing under a requests/second axis. Query-syntax review
and unit tests (`tests/unit/test_monitoring_stack.py`,
`tests/unit/test_metrics.py`) cover the panel JSON and metric emission, but
only a live pass against a running stack catches scrape-target and
range-vs-activity issues, so re-run these steps after any change to
`docker/grafana/dashboards/{sre-home,rag-performance}.json` or to RAG
metric emission (`src/nyxgpt/app.py`, `src/nyxgpt/chat.py`):

1. Start the stack (`nyxgpt ops install`) and open Grafana's **SRE Home**
   dashboard from the Admin Dashboard's SRE Overview tile. Set the time
   range to cover "now" (e.g. Last 15 minutes) with auto-refresh on — a
   flat/blank panel with a stale time range is a range mismatch, not a
   metrics bug.
2. Issue one RAG query from the chat pane (any prompt that triggers
   retrieval) and confirm "RAG queries by source" and "Total RAG queries by
   source" move within one scrape interval (Prometheus scrapes `/metrics`
   every 15s — see `docker/prometheus.yml`).
3. Ingest one document through each real RAG path — playground/API upload
   (`POST /rag/ingest`) and index-repo — and confirm each bumps "RAG ingests
   by source and outcome" / "Total RAG ingests by source" with the correct
   `source`/`outcome` labels.
4. Attach one document via the chat paperclip flow and confirm it moves the
   SRE Home "Chat attachments" stat *only* — per the two-function decision
   (#3463), a paperclip attachment is inlined into the prompt and never
   touches the vectorstore, so it must leave every RAG panel ("RAG queries
   by source", "RAG ingests by source and outcome", and both RAG Performance
   dashboards) unchanged (#3469 acceptance failure: an earlier fix
   incorrectly counted paperclip attachments in `nyxgpt_rag_ingests_total`).
5. Confirm the "Total RAG queries/ingests by source" and "Chat attachments"
   stat panels render `0` (not blank or "No data") for any source/result
   with no activity in the selected window.
6. In the Prometheus UI (started by `nyxgpt ops observability`; reachable
   at its published port), check **Status → Targets** and confirm the API
   scrape target is `UP`. A dead scrape reproduces the same "flat
   zero/blank stat" symptom regardless of whether the counters are
   incrementing correctly.

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
endpoint every 15s using `docker/prometheus.yml`) and `grafana` (UI at
[http://localhost:3001](http://localhost:3001), reachable via the Admin
Dashboard's SRE Overview tile). Grafana is pre-provisioned on first boot
with a Prometheus datasource, a Jaeger datasource, an Infinity datasource
(queries GlitchTip's REST API), seven dashboards under
`docker/grafana/dashboards`, and real alerting (rules, a Slack contact
point, and a notification policy — see [alerting.md](alerting.md)), plus
the SRE Home landing dashboard described above:

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
- **Canary Rollout** — rollout active/idle, live traffic split, rollback
  count (24h), evaluation results, lifecycle events by action/outcome,
  per-track running versions, and a Loki-backed deploy/start/promote/
  rollback event timeline — see
  [kubernetes.md](kubernetes.md#canary-logging--metrics). (The blue-green
  deployment dashboard was retired -- canary is the sole deployment model,
  see #3409.)

Every dashboard above is linked from the **SRE Home** dashboard
(`docker/grafana/dashboards/sre-home.json`, uid `nyxgpt-sre-home`), which is
provisioned as Grafana's org/home default — it's what you land on when the
Admin Dashboard's SRE Overview tile opens Grafana. (The former in-app
Dashboard Catalog component and the standalone Operational Logs dashboard
were retired in #3411 — the Logs Drilldown app replaces curated per-component
log queries, see [Log Aggregation](#log-aggregation) below.)

Log in with username `admin` and the password in `~/.nyxGPT/config.ini`'s
`[monitoring] grafana_admin_password` (auto-generated by `nyxgpt wizard`), or,
if that key is unset, the ops-managed secret `nyxgpt ops install` generates at
`~/.nyxGPT/secrets/grafana-admin-password`. `nyxgpt ops install` resets the
running container's actual admin password to match whichever of those applies
via `grafana cli admin reset-admin-password`, so log-in works the same way on
a fresh volume and on an existing long-lived one — see
[security.md](security.md#api-key-management).

`[monitoring] enabled = true` is already set in `docker/config.docker.ini`
so the Admin Dashboard's status badges show monitoring as active as soon as
the profile is up -- see
[configuration.md](configuration.md#monitoring-section) and
[api.md](api.md#monitoring-dashboards). If you changed `GRAFANA_UI_PORT` in
`.env`, update `grafana_ui_url` in config to match, or the SRE Overview
tile will point at the wrong port.

Alerting is real: rules provisioned as code under
`docker/grafana/provisioning/alerting/` evaluate continuously in Grafana
(not Prometheus) and show their state (Normal/Pending/Firing) on Grafana's
own **Alerting** page, with a Slack contact point you can configure from the
config wizard. See [alerting.md](alerting.md) for the full rule list,
how to wire up Slack, and how to fire a test alert with `nyxgpt ops
alert-test`.

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

`docker/promtail-config.yml` scrapes both paths under the same `job` label
(`nyxgpt`), so a query filtering only on `job` still spans both deployment
modes. Each target additionally carries an explicit `service_name` label
from a fixed, bounded set -- `api`, `cli`, `ollama`, `cassandra` (`web`
joins once #3430 lands) -- so Grafana's Logs Drilldown service breakdown
shows the real services instead of one flat, undifferentiated bucket
(#3441). The API process writes `api.log` and every `nyxgpt` CLI invocation
writes `cli.log` (see [configuration.md](configuration.md#logging-section)),
so those two are distinguishable even though both can appear in either
directory. Ollama and Cassandra are never nyxgpt Python processes -- their
logs only ever land in the native-mode directory, written as real files by
the `follow-ollama-logs.sh`/`follow-cassandra-logs.sh` LaunchAgents
(never a symlink -- a symlink whose target lives outside
`~/.nyxGPT/logs` is unreachable from inside promtail's own bind-mounted
container view, see [api.md#ollama-logs](api.md#ollama-logs)).

If you're running native mode and don't see logs in Grafana, run `nyxgpt
ops doctor` first -- it flags a missing native-log bind mount by
inspecting the *running* promtail container's mounts (`docker inspect`,
not just the compose file's text), catching a container that was created
before a `docker-compose.yml` edit rather than leaving it to a
silently-empty dashboard. `nyxgpt ops doctor` also reports a per-component
log volume for the last 24h (via Loki), so an idle curated component
(e.g. canary on a native install that's never run a k8s operation)
isn't mistaken for a broken pipeline.

That log-volume check queries Loki through Grafana's datasource-proxy API
(`/api/datasources/proxy/uid/loki/...`), which requires authentication. It
does **not** use the shared `[monitoring] grafana_admin_password` for this
-- that password lives in config.ini and can drift from whatever a
long-running Grafana container's own database actually has (a regenerated
config.ini, a hand-edited `.env`, ...), which used to make the check 401 on
an otherwise-healthy stack (#3438). Instead, `nyxgpt ops install`/
`nyxgpt ops observability` mint a dedicated Viewer-scoped Grafana
service-account token the first time they run and write it to
`~/.nyxGPT/secrets/grafana-doctor-token` (0600, git-ignored) -- the same
`~/.nyxGPT/secrets` pattern the GlitchTip→Grafana token uses (see
[Error Tracking](#error-tracking) below), just read by this process
directly instead of by Grafana's own provisioning. If that file is missing,
or Grafana rejects the token (401), `nyxgpt ops doctor` reports it as an
actionable finding instead of silently omitting the log-volume line; fix
either case by re-running `nyxgpt ops install` (delete the token file first
if Grafana rejected it, to force minting a fresh one).

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
profiles together), which is pre-provisioned with a Loki datasource.

The primary way to search logs (#3411) is Grafana's **Logs Drilldown** app
(`grafana-lokiexplore-app`, pinned/provisioned so it's present on a fresh
install), reached via the Admin Dashboard's SRE Overview tile → SRE Home
dashboard → the featured logs panel's "Open Logs Drilldown" title link,
pre-filtered to `{job="nyxgpt"}` so the label filter never has to be typed by
hand. Drilldown's own label filtering
(`level`, `logger`) replaces the older curated per-component saved queries,
so the standalone **Operational Logs** dashboard was retired. SRE Home also
renders a featured, queryless `{job="nyxgpt"}` logs panel directly on the
page (not a buried link) for at-a-glance recent activity before jumping
into Drilldown for filtering; the older standalone **Logs Explorer**
dashboard was retired in favor of that panel plus Drilldown.

The Loki datasource also carries a derived-field extraction for `trace_id`
that links a log line straight to its trace in the Jaeger datasource (see
[Distributed Tracing](#distributed-tracing) below). This is live: both
`nyxgpt.logging` (`TraceContextFilter`) and the web tier's structured
logger (`web/src/lib/logger.ts`) append a ` trace_id=<hex> span_id=<hex>`
suffix to any log line emitted while a span is active, and are otherwise
unchanged (#3430) — click the trace icon next to a log line in Drilldown
to jump straight into that request's spans in Jaeger; from a slow/errored
span in Jaeger, its `service.name` + timestamp narrow a Drilldown query
back to the matching log lines.

The web tier (Next.js server routes) now participates in the same log
pipeline instead of writing unparseable bare `console.error` lines: every
route handler logs through `web/src/lib/logger.ts` (level, `nyxgpt.web`
logger name, request id, UTC timestamp — the same line shape
`nyxgpt.logging.DEFAULT_FMT` produces), wrapped via
`web/src/lib/withRequestLog.ts` so the request id is ambient for the whole
handler. In Compose mode the web service writes to a dedicated
`~/.nyxGPT/volumes/nyxgpt-web-logs` volume (`NYXGPT_LOG_DIR`) that promtail
also mounts; in native mode brew's `StandardOutPath` already redirects
`nyxgpt-web`'s stdout into `~/.nyxGPT/logs`, which the existing native
scrape path picks up with no extra wiring. Filter on `{job="nyxgpt",
logger="nyxgpt.web"}` in Drilldown to see web-tier logs alongside the API's.

The self-heal and canary pages still show their own curated Loki query
links (`GET /api/v1/log-aggregation`, see [api.md](api.md#log-aggregation))
for jumping straight to their own event stream in Grafana Explore — those
are unrelated to the retired Log Aggregation panel and Operational Logs
dashboard.

`nyxgpt ops observability` starts both the `monitoring` and `logging`
profiles together (Grafana needs the `logging` profile's Loki instance for
its datasource) — see [ops.md](ops.md#nyxgpt-ops-observability).

`[log_aggregation] enabled = true` is already set in
`docker/config.docker.ini` so the Admin Dashboard's status badges and the
self-heal/canary pages' Explore links are active as soon as the profile is
up -- see [configuration.md](configuration.md#log_aggregation-section) and
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
and `jaeger` (stores traces and serves its own UI at
[http://localhost:16686](http://localhost:16686) -- a debug tool now, not
primary navigation; browse traces inside Grafana via the Jaeger datasource
instead, reached through the Admin Dashboard's SRE Overview tile).

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
and both Jaeger and Grafana's traces view stay empty while `[tracing]
enabled` still reports `true` -- `nyxgpt ops doctor` checks that something
is actually listening on the configured `otlp_endpoint` and flags the gap.

### Correlation backbone (browser → Next.js → FastAPI → Ollama)

The web tier participates in the same trace as the API (#3430) via
standard OTel instrumentation, not hand-rolled header forwarding:

- **Browser**: `web/src/instrumentation-client.ts` registers a
  `WebTracerProvider` with `FetchInstrumentation`, so a browser `fetch()`
  to any `/api/**` route starts a span and attaches a W3C `traceparent`
  header.
- **Next.js server**: `web/src/instrumentation.ts` uses `@vercel/otel` to
  instrument the server's own outgoing `fetch()` (i.e. every
  `apiProxy.ts` `apiFetch` call) -- it continues the browser's trace (or
  starts a new one) and forwards `traceparent` into the call to FastAPI.
- **FastAPI → Ollama**: `nyxgpt.tracing.init_tracing` now also
  instruments `urllib.request` (`ollama_client.py`'s HTTP client), so the
  outbound Ollama call gets its own client span and W3C header, continuing
  the same trace one hop further.

One chat request therefore produces a single trace spanning browser click
to Ollama and back, browsable as one trace in Jaeger (via the Grafana
Jaeger datasource) and joinable to the log lines each hop produced via the
`trace_id=`/Loki derived-field link described above. `request_id` (the
human-facing id in `X-Request-Id` and log lines) is derived from the trace
id whenever a trace is active, both in `nyxgpt.app`'s middleware and the
web tier's `requestContext.ts` -- so the two ids agree without a second,
disconnected identifier.

The web tier's OTel setup degrades gracefully exactly like the API's: a
missing/unreachable collector (fresh install, `--skip-observability`, a
collector restart) never blocks a request -- span export failures are
handled by the exporter itself (silently dropped, same OTLP exporter
contract as the Python side), and `instrumentation.ts`/
`instrumentation-client.ts` wrap their own setup in `try`/`catch` so a
setup-time failure can't crash the web server or the browser bundle. Point
`web`'s `NYXGPT_OTLP_ENDPOINT`/`NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT` at the
same collector the API uses -- see
[configuration.md](configuration.md#web-tier-environment-variables).

### Verifying the backbone end to end

Reproducible on a fresh `nyxgpt ops install` (which brings up the
`tracing`/`logging`/`monitoring` profiles automatically):

1. Open the web UI and send one chat message.
2. **Loki → Jaeger**: open Grafana's Logs Drilldown (Admin Dashboard → SRE
   Overview → SRE Home), filter to `{job="nyxgpt", logger="nyxgpt.web"}`,
   find the `chat/stream: start ...` line for that request, and click its
   trace-id derived-field link -- it opens the matching trace in the
   Jaeger datasource.
3. **Jaeger → Loki**: from that trace, note its `trace_id`; back in
   Drilldown, query `{job="nyxgpt"} |= "<trace_id>"` -- it returns every
   log line from every hop (web, api, and any RAG/Cassandra spans) for that
   one request.
4. **Single trace, multiple hops**: the trace itself should show a root
   span from the browser's `fetch` (or the Next.js server span if browser
   tracing didn't start one), a child span for the Next.js → FastAPI call,
   and a further child span for the FastAPI → Ollama call --
   `service.name` distinguishes `nyxgpt-web-browser`, `nyxgpt-web`, and
   `nyxgpt-api`.

If step 2's derived-field link is missing or step 4 shows disconnected
traces instead of one, `nyxgpt ops doctor`'s OTLP-reachability check
(#3350) is the first thing to check -- it flags `[tracing] enabled` but
nothing listening on `otlp_endpoint`, the most common reason spans go
missing silently.

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
[http://localhost:8080](http://localhost:8080), click-through target for
error detail from Grafana's GlitchTip panels), and `glitchtip-worker`.

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

`glitchtip-init` also mints a scoped GlitchTip API token for Grafana's
Infinity datasource (never a hand-pasted token) and writes it to
`~/.nyxGPT/secrets/glitchtip-grafana-token` (0644 in a 0755 dir --
world-readable but owner-writable-only, git-ignored -- since Grafana's
container runs as non-root uid 472 and a native Linux bind mount needs
that uid able to read the file), which the `grafana` service mounts
read-only and the Infinity datasource reads via Grafana's `$__file{}`
provisioning expansion. Grafana only reads that file
at startup, so `glitchtip-init` restarts the `grafana` container directly
once the token is written (if it's already running); re-run
`nyxgpt ops glitchtip-init` if the GlitchTip panels in the SRE Home
dashboard show an auth error.

### Web tier (browser + Next.js server errors)

The Python API is one reporter into GlitchTip; the web tier is a second,
independent one (#3430), using the JS Sentry SDK (`@sentry/nextjs`)
instead of `sentry_sdk` -- same GlitchTip project, same opt-in-with-a-DSN
contract, no data ever leaves the machine:

- **Browser**: `web/src/instrumentation-client.ts` calls `Sentry.init()`
  with `NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN` when set, capturing
  uncaught exceptions and unhandled rejections from client components with
  full stack traces/breadcrumbs -- a real Sentry-protocol event, not just
  the message/stack strings `ClientErrorReporter.tsx`'s legacy relay
  forwards through the API (`ClientErrorReporter` is left in place as a
  harmless fallback; both are no-ops with tracking disabled).
- **Next.js server**: `web/src/instrumentation.ts` calls `Sentry.init()`
  for the Node.js runtime and wires Next's `onRequestError` hook to
  `Sentry.captureRequestError`, so uncaught errors from route handlers/
  Server Components are reported the same way.

Both set `tracesSampleRate: 0` -- Sentry is errors-only here; distributed
tracing is `@vercel/otel`'s job (see
[Distributed Tracing](#distributed-tracing) above), so the two SDKs never
compete for the global TracerProvider. `NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN`
is a Next.js build arg (baked into the browser bundle, like
`NEXT_PUBLIC_API_BASE_URL`), so set it to match `[error_tracking] dsn` in
config.ini and rebuild the web image/formula to enable it -- see
[configuration.md](configuration.md#web-tier-environment-variables). This
manual sync (there's no automatic config.ini → build-arg pipeline yet) is
a known follow-up.

**Linux note (#3432):** `nyxgpt ops install` creates `~/.nyxGPT/secrets`
itself, before Grafana's bind mount can touch it, so the token write above
just works. If you ever see `nyxgpt ops glitchtip-init` fail with a
permission error on that directory, it means something (usually a raw
`docker compose up` run outside `nyxgpt ops`, or a pre-fix install) let
Docker create `~/.nyxGPT/secrets` as root first -- `docker`'s daemon runs
as root on Linux and auto-creates a missing bind-mount source directory
owned by root the first time a container starts, which then blocks this
(non-root) write. `nyxgpt ops doctor` flags this (secrets dir not writable,
or the token file missing while the observability stack is up); fix with
`sudo chown -R $(whoami) ~/.nyxGPT/secrets` and re-run
`nyxgpt ops install`. The GlitchTip datasource also lives in its own
provisioning file (`docker/grafana/provisioning/datasources/glitchtip.yml`,
separate from `datasource.yml`'s Prometheus/Loki/Jaeger) so a missing token
never takes those other datasources down with it -- only the GlitchTip
panels are affected until the token is fixed. This doesn't affect macOS:
Docker Desktop's VM handles bind-mount ownership differently.

## Self-Healing

Every core service (`ollama`, `cassandra`, `api`, `web`) and most opt-in
profile services have Docker healthchecks and `restart: unless-stopped`.
On top of that, a self-heal watchdog inside the `api` container watches
`docker compose ps` and automatically restarts anything unhealthy or
stopped — see [self-healing.md](self-healing.md) for the full design, how
to turn it on (`/admin/self-heal`, `nyxgpt self-heal`, or the API), and
`scripts/smoke-test.sh`, the documented end-to-end smoke test (deploy →
verify chat/RAG → kill each component → observe auto-heal → teardown).

## Canary Deployment

`/admin/canary` (deploy/gate/promote weighted canary rollout — see
[kubernetes.md](kubernetes.md#canary-deployment)) is not operable under
docker-compose: there is no Kubernetes cluster here for `kubectl` to reach,
and the compose stack runs a single `api` container rather than the
stable/canary Deployment pair the feature shifts traffic between. The page
detects this deployment mode and explicitly says canary doesn't apply here
and which mode provides it, instead of inferring "unavailable" from a
`kubectl not found` error. Use the [Kubernetes deployment](kubernetes.md) to
operate this feature — the same `api` image ships `kubectl` and the RBAC it
needs there.

## Rebuilding after code changes

```bash
docker compose build api web
docker compose up -d
```
