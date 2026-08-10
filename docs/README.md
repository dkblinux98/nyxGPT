# nyxGPT Documentation

Index of all nyxGPT documentation, grouped by audience. If you're just getting
started, read [Configuration](configuration.md) and the root
[README](../README.md) first.

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
