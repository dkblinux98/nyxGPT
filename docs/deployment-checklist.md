# Deployment Checklist

A pre-flight checklist for taking nyxGPT beyond a single-user localhost setup —
onto a shared network, a container platform, or a local Kubernetes cluster.
Each section links to the detailed guide that explains the *why*; this page
is the condensed, actionable version to walk through before you flip the
switch.

nyxGPT is [local-first](../VISION.md): nothing below is required to run it on
your own machine. Work through this checklist only when nyxGPT will be
reachable by more than just you.

---

## 1. Security configuration

See [`docs/security.md`](security.md) for full details on each item.

- [ ] `[auth] enabled = true` in `~/.nyxGPT/config.ini`, with a freshly
      generated `api_key` (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
- [ ] `[rate_limit] enabled = true` with limits appropriate to expected traffic
- [ ] `chmod 600 ~/.nyxGPT/config.ini` and `chmod 700 ~/.nyxGPT`
- [ ] API/web bound to `127.0.0.1` behind an SSH tunnel, VPN, or
      TLS-terminating reverse proxy — not directly exposed on `0.0.0.0`
- [ ] `NYXGPT_CORS_ORIGINS` set to the exact origin(s) serving the web UI (no
      wildcards, since `allow_credentials=True`)
- [ ] Secrets (`auth.api_key`, `openai.api_key`, `github.*token*`) rotated if
      any may have been exposed
- [ ] `[logging] level` kept at `INFO` (not `DEBUG`) in shared environments

## 2. Performance configuration

See [`docs/performance.md`](performance.md#quick-performance-checklist) for
tuning guidance and hardware-based recommendations.

- [ ] Model size matches available hardware (see
      [Model Selection](performance.md#model-selection))
- [ ] Caching enabled for your workload (embedding cache for RAG; response
      cache only for testing/demos, not production chat)
- [ ] RAG tuned for the use case (`chat_top_k`, `max_chunks`,
      `chat_context_max_chars`) or disabled entirely if unused
- [ ] `chat_timeout_seconds` / `embedding_timeout_seconds` set for the
      target hardware
- [ ] Cassandra memory/CPU sized for the expected document volume (see
      [Cassandra Optimization](performance.md#cassandra-optimization))

## 3. Monitoring setup

- [ ] `/health` endpoint reachable and returning `200` (see
      [`docs/api.md`](api.md#get-health))
- [ ] `[monitoring] enabled = true` if using the Prometheus/Grafana stack, so
      the admin dashboard's Monitoring Dashboards card links out correctly
      (see [`docs/api.md#monitoring-dashboards`](api.md#monitoring-dashboards))
- [ ] Metrics scrape target (`/metrics`) reachable from Prometheus, or the
      opt-in `monitoring` Compose profile started (see
      [`docs/docker-compose.md#monitoring-dashboards`](docker-compose.md#monitoring-dashboards))
- [ ] Log aggregation configured if running multiple instances/containers
      (see [`docs/docker-compose.md#log-aggregation`](docker-compose.md#log-aggregation)),
      otherwise confirm `~/.nyxGPT/logs/` is writable and rotating
- [ ] Distributed tracing and error tracking enabled if diagnosing
      cross-service issues (see [`docs/api.md`](api.md#distributed-tracing)
      and [`docs/api.md#error-tracking`](api.md#error-tracking))
- [ ] Alerting rules reviewed for the metrics that matter to you (error rate,
      p95 latency — the same signals `nyxgpt canary evaluate` reads, see
      [`docs/kubernetes.md#metrics-source`](kubernetes.md#metrics-source))

## 4. Backup configuration

nyxGPT keeps all durable state under `~/.nyxGPT/` (or the `nyxgpt_data` /
`cassandra_data` volumes when running via
[Docker Compose](docker-compose.md#volumes)). There is no built-in backup
command — back these up with your own snapshot/cron tooling:

- [ ] `~/.nyxGPT/config.ini` — runtime configuration and secrets (store the
      backup with the same access restrictions as the original, `chmod 600`)
- [ ] `~/.nyxGPT/sessions/` — conversation history; contains user content,
      handle with the same care as the live data (see
      [`docs/security.md#session-security`](security.md#session-security))
- [ ] Cassandra data (`~/.nyxGPT/cassandra` locally, or the `cassandra_data`
      Docker volume) — the RAG vector store; losing it means re-ingesting all
      documents
- [ ] `ollama_data` / pulled models — large but re-downloadable; back up only
      if re-pulling is impractical for your network
- [ ] A documented, tested restore procedure — a backup you haven't restored
      from is not verified. At minimum, confirm you can restore
      `~/.nyxGPT/config.ini` and Cassandra data into a fresh environment and
      pass the health checks in the next section
- [ ] Backup frequency matches your tolerance for data loss (session/RAG data
      changes with usage, not on a fixed schedule)

## 5. Health check verification

- [ ] `curl http://127.0.0.1:8000/health` (add `-H "X-API-Key: <key>"` if
      auth is enabled) returns `200` (see [`docs/api.md`](api.md#get-health))
- [ ] `nyxgpt ops doctor` passes for Homebrew/local deployments (see
      [`docs/ops.md`](ops.md#nyxgpt-ops-doctor))
- [ ] For Kubernetes: `kubectl -n nyxgpt get pods` shows all pods `Ready`,
      and the readinessProbe backing `kubectl rollout status` is green (see
      [`docs/kubernetes.md`](kubernetes.md#4-verify))
- [ ] For blue/green or canary rollouts: `nyxgpt deploy status` /
      `nyxgpt canary status` confirm the target color/track is healthy
      *before* cutting traffic over (see
      [Blue/Green Deployment](kubernetes.md#bluegreen-deployment) and
      [Canary Deployment](kubernetes.md#canary-deployment))
- [ ] For Docker Compose: `docker compose ps` shows all services `healthy`,
      not just `running` (see [`docs/docker-compose.md`](docker-compose.md))
- [ ] A rollback path is confirmed working (`nyxgpt deploy rollback` /
      `nyxgpt canary rollback`) before relying on it in an incident

---

## Related documentation

- [`docs/security.md`](security.md) — full security hardening guide
- [`docs/performance.md`](performance.md) — performance tuning guide
- [`docs/ops.md`](ops.md) — local service management (`nyxgpt ops`)
- [`docs/docker-compose.md`](docker-compose.md) — full-stack Docker Compose deployment
- [`docs/kubernetes.md`](kubernetes.md) — Kubernetes deployment, blue/green and canary rollouts
- [`docs/configuration.md`](configuration.md) — full configuration reference
- [`docs/troubleshooting.md`](troubleshooting.md) — common issues
