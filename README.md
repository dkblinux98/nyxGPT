# nyxGPT

**nyxGPT** is a local-first, private, extensible ChatGPT-style system designed to run entirely on your own machine.

It uses **Ollama** for local LLM inference, supports persistent **conversation sessions**, optional **Retrieval‑Augmented Generation (RAG)** backed by **Apache Cassandra**, a powerful **CLI**, a **FastAPI backend**, and a lightweight **local web UI** built with Next.js.

Your data stays on your machine. No cloud dependency is required.

---

## Why nyxGPT?

- Local‑only by default (no cloud calls)
- Your prompts, sessions, and embeddings never leave your machine
- Clear separation between CLI, API, UI, and core logic
- Designed for experimentation, learning, and extension
- Production‑like ops tooling for a local system

---

## Key features

- Local LLM inference via **Ollama**
- Persistent sessions stored outside the repository
- **Message editing and regeneration** - Edit messages and fork conversations, regenerate responses
- **Message search** - Full-text search across all sessions with filters for role, session, and case-sensitivity
- **Automatic session naming** with LLM‑generated titles and smart filename sync
- **Session management** with right-click context menus, rename, export, delete, and pin
- Optional **RAG** using Cassandra 5.0 native vector search
- **Per‑session RAG controls** via WebUI and API
- Config‑driven RAG context pruning and prompt optimization
- **Optimized embedding generation** with async processing, GPU utilization, and adaptive batching
- Streaming responses (CLI, API, Web UI)
- Unified core shared between CLI and FastAPI
- Optional **API rate limiting** (disabled by default for localhost use)
- Homebrew‑managed background services
- Optional **Kubernetes deployment** for local clusters (kind/minikube/k3s)
- **Local canary deployment** — deploy a versioned build to canary only, gate a gradual weighted rollout on live metrics, then promote it to stable (or roll back) — operable from the SRE/admin dashboard (`nyxgpt canary` CLI or `/admin/canary`)
- **System health dashboard** — service uptime, dependency reachability checks (Ollama, Cassandra), resource utilization, and threshold-based alert indicators, surfaced in the SRE/admin dashboard (`/admin/health`)
- **Prometheus metrics** (`/metrics`) — request counts, latency histograms, error rates, and chat/RAG business metrics, surfaced in the SRE/admin dashboard (`/admin`)
- **Monitoring dashboards** (Grafana) — local-only system overview, RAG performance (including ingest activity), API metrics, resource usage (CPU/mem/queue/cache/rate-limit), and self-healing dashboards backed by Prometheus, plus alerting rules, auto-started with `nyxgpt ops install` (`nyxgpt ops observability` to start/re-run standalone), linked from the SRE/admin dashboard (`/admin`)
- **Log aggregation** (Loki + promtail) — local-only centralized search over `~/.nyxGPT/logs` (api, web, Ollama, Cassandra — Ollama captured automatically by `nyxgpt ops install` whether it's running natively or as a Compose container) with a retention policy, searched via Grafana's Logs Drilldown app and a featured queryless logs panel (`{job="nyxgpt"}`) on the SRE Home dashboard, auto-started with `nyxgpt ops install`
- **Distributed tracing** (OpenTelemetry) — local-only request/RAG/Ollama/Cassandra spans exported to a local Jaeger instance and browsed inside Grafana via a Jaeger datasource, auto-started with `nyxgpt ops install`
- **Error tracking** (self-hosted GlitchTip) — local-only backend exception and web UI client error reporting via the Sentry SDK protocol, auto-started and auto-provisioned (admin user, org, project, DSN, and a Grafana API token) with `nyxgpt ops install` — zero-touch, no manual sign-in step — surfaced as Grafana panels via the Infinity datasource
- **SRE Overview** — Grafana is the single pane of glass: the Admin Dashboard's SRE Overview tile (`/admin/dashboard`) opens Grafana's SRE Home dashboard in a new tab, reaching every Grafana dashboard, Logs Drilldown, traces, and GlitchTip error tracking above, all provisioned as code
- Optional **Docker Compose** stack for one-command bring-up of every component
- Robust unit and integration test suite

---

## Quick start

### Requirements

- Python 3.11+
- Ollama
- Homebrew
- Docker Desktop (required for Cassandra / RAG)
- Node.js (for the local web UI)

### Install and configure

```bash
pip install -e .
nyxgpt wizard        # interactive setup: Ollama connection, default model, RAG, config.ini
```

The wizard tests your Ollama connection, helps you pick a default model,
optionally configures RAG, and generates `~/.nyxGPT/config.ini` — all
runtime configuration lives outside the repository. See
[Configuration](docs/configuration.md) for every `config.ini` section and
key, and [CLI](docs/cli.md) for the full command reference.

### Start services

```bash
nyxgpt ops install    # installs and starts API, web UI, Cassandra helpers, observability
nyxgpt ops doctor      # verify everything is healthy
```

Then chat from the CLI or the [local web UI](docs/ui.md#local-web-ui-nextjs)
(`http://127.0.0.1:3000`, started by `nyxgpt ops install` or
`nyxgpt ops restart web`):

```bash
nyxgpt chat "Hello"
```

`nyxgpt ops` also covers restarting, stopping, and tearing down every
component — see [Ops helpers](docs/ops.md). Alternative deployment paths
(a single-command containerized stack, a local Kubernetes cluster with
canary rollout, or Terraform-managed local infrastructure)
are documented in [Docker Compose](docs/docker-compose.md),
[Kubernetes](docs/kubernetes.md), and [Terraform](docs/terraform.md) —
each is driven through `nyxgpt`-wrapped commands, never a raw
`docker`/`docker compose`/`kubectl`/`terraform` invocation.

---

## Logs & runtime data

All runtime state lives under:

```text
~/.nyxGPT/
```

Including:

- `sessions/` – conversation sessions
- `logs/` – API, web UI, Ollama, and Cassandra logs
- `scripts/` – service wrapper scripts

No runtime data is stored in the git repository.

---

## Documentation

Full documentation lives under [`docs/`](docs/README.md) — see the
**[documentation index](docs/README.md)** for the complete, grouped list
(User guides · Operations & deployment · Developer · Agent system).

Common starting points:

- **Configuration** – [`docs/configuration.md`](docs/configuration.md)
- **CLI** – [`docs/cli.md`](docs/cli.md)
- **API** – [`docs/api.md`](docs/api.md)
- **UI (Web)** – [`docs/ui.md`](docs/ui.md)
- **RAG** – [`docs/rag.md`](docs/rag.md)
- **Sessions & Memory** – [`docs/sessions.md`](docs/sessions.md)
- **Docker Compose** – [`docs/docker-compose.md`](docs/docker-compose.md)
- **Self-healing** – [`docs/self-healing.md`](docs/self-healing.md)
- **Security** – [`docs/security.md`](docs/security.md)
- **Architecture** – [`docs/architecture.md`](docs/architecture.md)
- **Troubleshooting** – [`docs/troubleshooting.md`](docs/troubleshooting.md)

If you are new to the project, start with **configuration**, then **architecture**, then **api**.

---

## GitHub Automation

This repository is developed by an automated agent loop (scrummaster →
developer → review) plus an on-demand `@claude` mention workflow, and ships
several Claude Code automations (MCP servers, hooks, a subagent, a skill)
that activate automatically in this directory. See
[docs/development.md](docs/development.md) for the full workflow reference
and [AGENTS.md](AGENTS.md) for agent roles and permissions.

---

## Project notes

- Distribution name: **nyxGPT**
- Python package name: **nyxgpt**
- Runtime data is always externalized
- Build artifacts such as `*.egg-info/` must not be committed

---

## Status

The core architecture, ops tooling, streaming, web UI, and RAG foundations are complete.

Future work focuses on:
- UX refinement
- performance tuning
- richer session metadata and search
- optional multi‑user and auth extensions
