# nyxGPT

**nyxGPT** is a local-first, private, extensible ChatGPT-style system designed to run entirely on your own machine.

It uses **Ollama** for local LLM inference, supports persistent **conversation sessions**, optional **Retrieval‑Augmented Generation (RAG)** backed by **Apache Cassandra**, a powerful **CLI**, a **FastAPI backend**, and a lightweight **local web UI** built with Next.js.

Your data stays on your machine. No cloud dependency is required.

---

## What nyxGPT actually is

The chat/RAG application above is real and working, but the code is not
the point — it's the vehicle. **nyxGPT is a reference implementation of
full-lifecycle software delivery discipline** — observability, canary
deployment, self-healing, release management, and an agent-run delivery
process — held together at a scale small enough for one person to read
end-to-end and check every claim against the actual commit and issue
history.

**This is an agent-coded project — AI agents wrote the overwhelming
majority of the code — managed by a person with 25 years of SRE and
Release Management experience, from individual-contributor through
Director-level leadership roles, in medium-size startups and mature
organizations.** That background is the explanation for the project's
shape, not a bio aside: what follows is what running an engineering
organization staffed by AI agents, part-time, looks like when the person
building the guardrails has spent a career running release trains and
on-call rotations.

The core story is standard SRE/release discipline pointed at a new kind of
worker:

- **Work is specified precisely enough to delegate.** Every issue carries
  acceptance criteria before an agent writes a line of implementation
  (see [Creating Issues](CLAUDE.md#creating-issues)).
- **Gates exist because delegated work fails, and are built to catch it.**
  Code review with automatic escalation after repeated rejection, CI that
  blocks merge, and human acceptance testing after every merge (see
  [How this project is run](docs/how-this-project-is-run.md) below) are
  failure catchers for AI-agent output, not bureaucracy. **Agent failures
  are expected and routine — it is the surrounding process, not agent
  infallibility, that turns that into production-grade output.** A
  rejected review, a red CI run, or a bounced acceptance test is the
  system working, not the system failing; this project's own history of
  those gates firing (see the retrospective linked below) is the
  credibility evidence, not a claim taken on faith.
- **Release discipline holds even though no one reviews every line.** A
  system that produces changes faster than any one person could review
  line-by-line still needs a release manager's judgment about what ships,
  when, and what gets rolled back — that judgment is recorded in this
  repo's phased releases and [decision records](product_management/).

The agent system itself (charters, runbooks, prompts, and the workflow
automation under `.github/workflows/`) is planned to become its own
project, with nyxGPT as its primary case study.

See **[How this project is run](docs/how-this-project-is-run.md)** for the
mechanics.

---

## Installing from PyPI — read this first

The `nyxgpt` package on PyPI provides the Python package (CLI, API, core)
as a versioned artifact. As of this codebase, `nyxgpt ops install`/`up` no
longer resolve any runtime resource (Compose file, config templates,
launchd/systemd unit templates, Grafana/Prometheus/promtail provisioning,
helper scripts) relative to a source checkout — everything ships as package
data (#3621), so `pip install nyxgpt` and `nyxgpt ops install` work
end-to-end with **no repo checkout**. Container images for the Compose/k8s
paths are also published to GHCR, and a remote Homebrew tap replaces the
old local `file://` tap for macOS (#3622) — see
[docs/homebrew.md#remote-tap](docs/homebrew.md#remote-tap).

**This has not shipped in a PyPI release yet** — the currently published
version predates this self-containment work. Until the next version is
published (owner-run release ceremony, `scripts/release_ceremony.sh`), a
repo checkout is still required:

```bash
git clone https://github.com/dkblinux98/nyxGPT.git
cd nyxGPT
pip install -e .
nyxgpt ops install
```

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
- Native background services — Homebrew on macOS, systemd on Linux (see [docs/homebrew.md](docs/homebrew.md) / [docs/systemd.md](docs/systemd.md))
- Optional **Kubernetes deployment** for local clusters (kind/minikube/k3s)
- **Local canary deployment** — deploy a versioned build to canary only, gate a gradual weighted rollout on live metrics, then promote it to stable (or roll back) — operable from the SRE/admin dashboard (`nyxgpt canary` CLI or `/admin/canary`)
- **System health dashboard** — service uptime, dependency reachability checks (Ollama, Cassandra), resource utilization, and alert indicators live from Grafana's real alerting (falling back to a labeled local estimate if Grafana is unreachable), surfaced in the SRE/admin dashboard (`/admin/health`)
- **Prometheus metrics** (`/metrics`) — request counts, latency histograms, error rates, and chat/RAG business metrics, surfaced in the SRE/admin dashboard (`/admin`)
- **Monitoring dashboards** (Grafana) — local-only system overview, RAG performance (including ingest activity), API metrics, resource usage (CPU/mem/disk/queue/cache/rate-limit), and self-healing dashboards backed by Prometheus, plus real alerting (CPU/memory/disk/service-down/self-heal/canary rules, a Slack contact point, `nyxgpt ops alert-test` — see [docs/alerting.md](docs/alerting.md)), auto-started with `nyxgpt ops install` (`nyxgpt ops observability` to start/re-run standalone), linked from the SRE/admin dashboard (`/admin`)
- **Log aggregation** (Loki + promtail) — local-only centralized search over `~/.nyxGPT/logs` (api, web, Ollama, Cassandra — Ollama captured automatically by `nyxgpt ops install` whether it's running natively or as a Compose container) with a retention policy, searched via Grafana's Logs Drilldown app and a featured queryless logs panel (`{job="nyxgpt"}`) on the SRE Home dashboard, auto-started with `nyxgpt ops install`
- **Distributed tracing** (OpenTelemetry) — local-only request/RAG/Ollama/Cassandra spans exported to a local Jaeger instance and browsed inside Grafana via a Jaeger datasource, auto-started with `nyxgpt ops install`
- **Live smoke verification** (`nyxgpt ops verify`) — boots the stack, generates known chat/RAG traffic, and asserts it landed via Prometheus counter deltas, Grafana panel-query re-execution, and Playwright dashboard screenshots; the review agent runs this itself in CI before approving any PR touching observability/metrics/UI, and it doubles as a one-command local pre-check before acceptance testing — see [docs/live-verification-ci.md](docs/live-verification-ci.md)
- **Error tracking** (self-hosted GlitchTip) — local-only backend exception and web UI client error reporting via the Sentry SDK protocol, auto-started and auto-provisioned (admin user, org, project, DSN, and a Grafana API token) with `nyxgpt ops install` — zero-touch, no manual sign-in step — surfaced as Grafana panels via the Infinity datasource
- **SRE Overview** — Grafana is the single pane of glass: the Admin Dashboard's SRE Overview tile (`/admin/dashboard`) opens Grafana's SRE Home dashboard in a new tab, reaching every Grafana dashboard, Logs Drilldown, traces, and GlitchTip error tracking above, all provisioned as code
- **Guided secrets setup** — masked entry, plain-language per-key help, and format validation for human-provided secrets (`nyxgpt secrets setup` CLI or `/admin/secrets`); `config.ini` is the canonical store for write-once external tokens, pushed one-way to this repo's GitHub Actions secrets via `nyxgpt ops secrets-sync`
- Optional **Docker Compose** stack for one-command bring-up of every component
- **Published install artifacts** — container images for the Compose/k8s paths on GHCR (`ghcr.io/dkblinux98/nyxgpt-{api,web}`) and a remote Homebrew tap, built and published by `.github/workflows/release-artifacts.yml` on every GitHub Release, alongside the owner-run PyPI publish (`scripts/release_ceremony.sh`) — see [docs/homebrew.md#remote-tap](docs/homebrew.md#remote-tap)
- Robust unit and integration test suite

---

## Quick start

### Requirements

- Python 3.11+
- Ollama
- Homebrew (macOS) or systemd (Linux) — see [docs/systemd.md](docs/systemd.md) for Linux prerequisites
- Docker (required for Cassandra / RAG)
- Node.js (for the local web UI)

### Install and configure

```bash
pip install -e .
nyxgpt wizard          # interactive setup: Ollama connection, default model, RAG, config.ini
nyxgpt secrets setup   # guided, masked entry for [openai] api_key / [github] pat (optional)
```

The wizard tests your Ollama connection, helps you pick a default model,
optionally configures RAG, and generates `~/.nyxGPT/config.ini` — all
runtime configuration lives outside the repository. `nyxgpt secrets setup`
walks through any remaining human-provided secrets one at a time (masked
input, where to obtain each one, format validation) and is safe to re-run.
See [Configuration](docs/configuration.md) for every `config.ini` section
and key, and [CLI](docs/cli.md) for the full command reference.

### Start services

```bash
nyxgpt up    # installs and starts API, web UI, Cassandra helpers, observability;
             # waits for everything to report healthy, then prints the web UI URL
```

Then chat from the CLI or the [local web UI](docs/ui.md#local-web-ui-nextjs)
(printed by `nyxgpt up`, normally `http://127.0.0.1:3000`):

```bash
nyxgpt chat "Hello"
```

`nyxgpt up`/`nyxgpt down` are thin aliases for `nyxgpt ops install`/
`nyxgpt ops down` (see [Ops helpers](docs/ops.md#nyxgpt-up--nyxgpt-down));
`nyxgpt ops` itself also covers restarting, stopping, and checking the
health of every component (`nyxgpt ops doctor`) — see
[Ops helpers](docs/ops.md). Alternative deployment paths
(a single-command containerized stack, a local Kubernetes cluster with
canary rollout, or Terraform-managed local infrastructure)
are documented in [Docker Compose](docs/docker-compose.md),
[Kubernetes](docs/kubernetes.md), and [Terraform](docs/terraform.md) —
each is driven through `nyxgpt`-wrapped commands, never a raw
`docker`/`docker compose`/`kubectl`/`terraform` invocation. AWS deployments
are `nyxgpt cloud`-wrapped too — see [Cloud (AWS)](docs/cloud.md), covering
`nyxgpt cloud infra` (provisions the AWS substrate: a VPC, subnet, an
SSH-only security group scoped to your own IP, and one EC2 instance) and
`nyxgpt cloud allow-ip` (SSH security-group lockout recovery).

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

## How this project is run

nyxGPT's engineering process is itself part of what this project
demonstrates — see [What nyxGPT actually is](#what-nyxgpt-actually-is)
above for why. **[How this project is run](docs/how-this-project-is-run.md)**
indexes the mechanics: the agent roles and their charters/runbooks, the
GitHub project board's status flow (Backlog → In Progress → In Review →
Acceptance Testing → For Release), the decision-record practice, the
Definition of Done and the acceptance-failure/improvement taxonomy, and
the retrospective.

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

---

## License

nyxGPT is released under the [MIT License](LICENSE).
