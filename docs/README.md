# nyxGPT Documentation

Index of the nyxGPT product documentation. If you're just getting started,
read [Installing nyxGPT](ops.md#installing-nyxgpt) and
[Configuration](configuration.md) first. The root
[README](../README.md) is deliberately a thin pointer into this index.

**In the product:** these documents ship inside the installed package, so the
web UI serves them under **Support → Docs** — the same documents, matching the
version you are running, readable with no checkout and no internet. The
Support menu's other item, **File an Issue**, offers one entry per ticket type
and opens a report form prefilled with that type, your version and your
platform (it needs internet and a GitHub account). See
[ui.md](ui.md#support-menu).

Documents about how this repository *builds itself* — the agent loop, CI
process, contributor setup — are not product documentation and are not
packaged with the install. They stay in this directory; start from
[CONTRIBUTING.md](../CONTRIBUTING.md) for contributor topics and
[AGENTS.md](../AGENTS.md) for the agent system. The packaged selection is
named in `nyxgpt.support.DOC_SECTIONS` (#3809).

## Feature overview

What nyxGPT does, with the doc that covers each area. Nothing here is a
substitute for those docs — this is a map, not a spec.

**Chat, sessions, and RAG** — local LLM inference via **Ollama**; persistent
sessions stored outside the repository, with pinning, tags, right-click
management, rename/export/delete, LLM-generated titles and filename sync;
message editing, conversation forking, and regeneration; full-text message
search with role/session/case filters; streaming responses in CLI, API, and web
UI ([sessions.md](sessions.md), [ui.md](ui.md)). Optional **RAG** on Cassandra
5.0 native vector search, with per-session controls, config-driven context
pruning and prompt optimization, and async/GPU/adaptive-batch embedding
generation ([rag.md](rag.md), [performance.md](performance.md)).

**Interfaces** — a CLI, a FastAPI backend sharing one core with it, and a local
Next.js web UI, plus optional API rate limiting (off by default for localhost)
([cli.md](cli.md), [api.md](api.md), [ui.md](ui.md),
[architecture.md](architecture.md)).

**Operations and deployment** — native background services (Homebrew/launchd on
macOS, systemd `--user` on Linux), a containerized Compose stack, local
Kubernetes (kind/minikube/k3s), and Terraform-managed local infrastructure —
all driven through `nyxgpt`-wrapped commands, never a raw
`docker`/`kubectl`/`terraform` invocation ([ops.md](ops.md),
[homebrew.md](homebrew.md), [systemd.md](systemd.md),
[docker-compose.md](docker-compose.md), [kubernetes.md](kubernetes.md),
[terraform.md](terraform.md)). Installs come from published artifacts by
default; `nyxgpt up --dev` brings the same stack up from a checkout's working
tree instead, for iterating on unreleased code
([ops.md](ops.md#--dev-run-the-current-checkout-without-an-artifact-build)).
Whichever mode brings the stack up, it pulls the configured chat and embedding
models before reporting itself up, so the first chat message works — with RAG
on or off — without anyone pulling a model by hand
([ops.md](ops.md#nyxgpt-ops-install)). Removal is wrapped too: `nyxgpt ops
uninstall` deregisters the services, the `com.nyxgpt.*` LaunchAgents and the
containers before you remove the artifacts, so nothing is left running that no
supported command can stop
([ops.md](ops.md#nyxgpt-ops-uninstall)).
AWS deployments are `nyxgpt cloud`-wrapped, with
an SSH-tunnel-only access path; `nyxgpt cloud status` says what is deployed
and how to reach it, and `nyxgpt cloud ops` inspects the instance over that
same path ([cloud.md](cloud.md)). Local **canary
deployment** gates a weighted rollout on live metrics before promotion or
rollback, and **self-healing** watches components and restarts them
([kubernetes.md](kubernetes.md), [self-healing.md](self-healing.md)).

**Observability** — Prometheus metrics (`/metrics`), Grafana dashboards and
real alerting, Loki/promtail log aggregation, OpenTelemetry tracing into
Jaeger, and self-hosted GlitchTip error tracking — all local-only,
auto-started and auto-provisioned by `nyxgpt ops install`, and reachable from
the SRE/admin dashboard ([alerting.md](alerting.md),
[docker-compose.md](docker-compose.md), [ops.md](ops.md)). The system health
and metrics screens live in the admin dashboard ([ui.md](ui.md)),
observability logins come from `nyxgpt ops credentials`
([ops.md](ops.md#nyxgpt-ops-credentials)), and `nyxgpt ops verify` proves the
telemetry is real by generating traffic and asserting it landed
([ops.md](ops.md#nyxgpt-ops-verify)).

**Secrets and security** — guided, masked secret entry with per-key help and
format validation (`nyxgpt secrets setup`, and `nyxgpt cloud
credentials-setup` for AWS identity — terminal-only, never a web form), with
`config.ini` as the canonical store ([security.md](security.md),
[configuration.md](configuration.md)).

**Distribution** — the PyPI wheel, a remote Homebrew tap, and GHCR container
images, published by the release pipeline; `nyxgpt ops portability` checks
mechanically that every supported target installs and operates with **no repo
checkout** and no raw orchestrator commands
([ops.md](ops.md#nyxgpt-ops-portability), [homebrew.md](homebrew.md),
[cloud.md](cloud.md#pypi-publishing-rc-and-stable)).

## User guides

- [Configuration](configuration.md) — every `config.ini` section and key
- [CLI](cli.md) — the `nyxgpt` command-line reference
- [Sessions](sessions.md) — chat sessions, pinning, tags, search, export
- [RAG](rag.md) — retrieval-augmented generation: ingestion, collections, querying
- [UI](ui.md) — the web interface
- [API](api.md) — REST API reference (`/api/v1/*`)
- [Troubleshooting](troubleshooting.md) — common problems and fixes
- [Session storage](session-storage.md) — where sessions are stored, and how to change it
- [Service worker / PWA](service-worker-pwa.md) — offline behaviour and installing the web UI as an app

## Operations & deployment

- [Docker Compose](docker-compose.md) — the containerized stack + monitoring profiles
- [Alerting](alerting.md) — Grafana alert rules, the Slack contact point, and `nyxgpt ops alert-test`
- [Kubernetes](kubernetes.md) — local-cluster manifests, canary deploy/gate/promote
- [Terraform](terraform.md) — local-first infrastructure-as-code
- [Cloud (AWS)](cloud.md) — `nyxgpt cloud` (AWS substrate provisioning, SSH-rule IP refresh, lockout recovery)
- [Self-healing](self-healing.md) — the watchdog, healthchecks, and `/admin/self-heal`
- [Deployment checklist](deployment-checklist.md) — pre-deploy security/perf/monitoring
- [Ops helpers](ops.md) — `nyxgpt ops` service management
- [Homebrew](homebrew.md) — macOS install via the Homebrew tap
- [systemd](systemd.md) — Linux `--user` services
- [Performance](performance.md) — tuning guide
- [Security](security.md) — auth, hardening, secrets

## Reference

- [Architecture](architecture.md) — system design and boundaries

## Contributor and agent-system documentation

Not product documentation, and not packaged with the install (#3809) — these
are about building nyxGPT and running this repository. The files live in this
same `docs/` directory in the repository:

- Contributing and local development — [CONTRIBUTING.md](../CONTRIBUTING.md),
  `development.md`, `testing.md`, `adding-api-endpoints.md`,
  `file-lock-audit.md`
- The agent system — [AGENTS.md](../AGENTS.md), `how-this-project-is-run.md`,
  `agent-smoke.md`, `agent-comment-tokens.md`, `acceptance-drain-gate.md`,
  `sprint-autopilot.md`, `KNOWN_LIMITATIONS.md`, `github-tokens.md`
- CI and release process — `live-verification-ci.md`,
  `security-scanning-ci.md`, `cloud-artifact-smoke.md`,
  `portability-matrix.md`
