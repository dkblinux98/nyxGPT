# nyxGPT Documentation

Index of all nyxGPT documentation, grouped by audience. If you're just getting
started, read [Installing nyxGPT](ops.md#installing-nyxgpt) and
[Configuration](configuration.md) first. The root
[README](../README.md) is deliberately a thin pointer into this index.

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
[terraform.md](terraform.md)). AWS deployments are `nyxgpt cloud`-wrapped, with
an SSH-tunnel-only access path ([cloud.md](cloud.md)). Local **canary
deployment** gates a weighted rollout on live metrics before promotion or
rollback, and **self-healing** watches components and restarts them
([kubernetes.md](kubernetes.md), [self-healing.md](self-healing.md)).

**Observability** — Prometheus metrics (`/metrics`), Grafana dashboards and
real alerting, Loki/promtail log aggregation, OpenTelemetry tracing into
Jaeger, and self-hosted GlitchTip error tracking — all local-only,
auto-started and auto-provisioned by `nyxgpt ops install`, and reachable from
the SRE/admin dashboard ([alerting.md](alerting.md),
[docker-compose.md](docker-compose.md), [ops.md](ops.md)). System health,
metrics, and portability screens live in the admin dashboard ([ui.md](ui.md)),
observability logins come from `nyxgpt ops credentials`
([ops.md](ops.md#nyxgpt-ops-credentials)), and `nyxgpt ops verify` proves the
telemetry is real by generating traffic and asserting it landed
([live-verification-ci.md](live-verification-ci.md)).

**Secrets and security** — guided, masked secret entry with per-key help and
format validation (`nyxgpt secrets setup` or `/admin/secrets`), with
`config.ini` as the canonical store ([security.md](security.md),
[configuration.md](configuration.md)).

**Distribution** — the PyPI wheel, a remote Homebrew tap, and GHCR container
images, published by the release pipeline; `nyxgpt ops portability` checks
mechanically that every supported target installs and operates with **no repo
checkout** and no raw orchestrator commands
([portability-matrix.md](portability-matrix.md), [homebrew.md](homebrew.md),
[cloud.md](cloud.md#pypi-publishing-rc-and-stable)).

## User guides

- [Configuration](configuration.md) — every `config.ini` section and key
- [CLI](cli.md) — the `nyxgpt` command-line reference
- [Sessions](sessions.md) — chat sessions, pinning, tags, search, export
- [RAG](rag.md) — retrieval-augmented generation: ingestion, collections, querying
- [UI](ui.md) — the web interface
- [API](api.md) — REST API reference (`/api/v1/*`)
- [Troubleshooting](troubleshooting.md) — common problems and fixes
- [Known limitations](KNOWN_LIMITATIONS.md)

## Operations & deployment

- [Docker Compose](docker-compose.md) — the containerized stack + monitoring profiles
- [Alerting](alerting.md) — Grafana alert rules, the Slack contact point, and `nyxgpt ops alert-test`
- [Kubernetes](kubernetes.md) — local-cluster manifests, canary deploy/gate/promote
- [Terraform](terraform.md) — local-first infrastructure-as-code
- [Cloud (AWS)](cloud.md) — `nyxgpt cloud` (AWS substrate provisioning, SSH-rule IP refresh, lockout recovery)
- [Portability matrix](portability-matrix.md) — which targets install with no repo checkout, and the clean-machine acceptance run
- [Self-healing](self-healing.md) — the watchdog, healthchecks, and `/admin/self-heal`
- [Deployment checklist](deployment-checklist.md) — pre-deploy security/perf/monitoring
- [Ops helpers](ops.md) — `nyxgpt ops` service management
- [Homebrew](homebrew.md) — macOS install via the Homebrew tap
- [Performance](performance.md) — tuning guide
- [Security](security.md) — auth, hardening, secrets

## Developer

- [Architecture](architecture.md) — system design and boundaries
- [Development](development.md) — local dev setup and workflow
- [Testing](testing.md) — test suites (pytest + vitest) and how to run them
- [Adding API endpoints](adding-api-endpoints.md)
- [Service worker / PWA](service-worker-pwa.md)
- [File-lock audit](file-lock-audit.md)

## Agent system

- [How this project is run](how-this-project-is-run.md) — agent roles, the project board's status flow, decision records, the Definition of Done, and the retrospective
- [GitHub tokens](github-tokens.md) — agent identities and required scopes
- [Agent smoke test](agent-smoke.md) — verifying the scrummaster/developer/review loop
- [Live verification in CI](live-verification-ci.md) — `nyxgpt ops verify`, and how the review agent runs it before APPROVE/REQUEST_CHANGES
