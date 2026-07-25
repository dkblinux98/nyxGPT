# Kubernetes Deployment (local clusters)

nyxGPT can be deployed to a **local** Kubernetes cluster (kind, minikube, k3s,
Docker Desktop's built-in cluster, etc.) as an alternative to the Homebrew /
`nyxgpt ops` workflow described in [ops.md](ops.md). This is aimed at running
nyxGPT on your own workstation, in line with the project's local-first
[VISION.md](../product_management/VISION.md) — it is not a guide for deploying to a cloud
provider.

Scope: this deploys the FastAPI backend (`nyxgpt-api`) only. Ollama keeps
running on the host (as it already does today), and Cassandra/RAG stay
disabled unless you point the manifests at your own Cassandra instance. The
web UI and a bundled Cassandra StatefulSet are not part of this deployment —
see [ops.md](ops.md) / `nyxgpt ops` for running the full local stack, or
[docker-compose.md](docker-compose.md) for one-command bring-up of every
component.

`nyxgpt-api` is deployed as a **blue/green pair** (`nyxgpt-api-blue` and
`nyxgpt-api-green`), fronted by a single Service. See
[Blue/Green Deployment](#bluegreen-deployment) below for how to cut traffic
over between them with zero downtime and roll back instantly. A second,
independent pair -- `nyxgpt-api-stable`/`nyxgpt-api-canary` -- supports
gradual weighted rollout; see [Canary Deployment](#canary-deployment).

## One-command bring-up (`nyxgpt ops`)

```bash
nyxgpt ops install --kubernetes --local
```

Per the project's [Operational Command Wrapping](../CLAUDE.md) rule, this is
the supported way to bring this deployment up — no raw `docker build`/
`kubectl` commands required. It wraps the whole documented flow below into
one step: checks prerequisites (a reachable cluster, `kubectl` on PATH),
builds `nyxgpt-api:local` and loads it into the cluster's image cache (kind/
minikube get an explicit load step; Docker Desktop's built-in cluster shares
the host cache already), bootstraps `k8s/secret.yaml` from the example
(prompting for the API key interactively, or pass `--api-key` — the value is
never committed), applies the kustomization, and snapshots Pod/HPA/Service
health.

`--local` is required and explicit — it's the only locality implemented
today, and is the precursor to a future cloud deployment target. `--cloud`
is accepted by the CLI surface but rejected with a "not yet implemented"
message rather than silently doing the wrong thing.

The command refuses to start if the native/Compose stack already owns the
`api` port — run `nyxgpt ops down` (or stop the conflicting components)
first. `nyxgpt ops status`/`doctor` show this namespace's Pod states
alongside native/Compose.

Tear down (removes the `nyxgpt` namespace and everything in it) with:

```bash
nyxgpt ops down --kubernetes
```

The rest of this document walks through what those two commands do, plus
the blue/green and canary rollout tooling that operates on top of this
deployment once it's up — useful if you want to run the bring-up steps
individually or troubleshoot a failure. It is reference material, not
something you're expected to type by hand.

## Prerequisites

- A local cluster: [kind](https://kind.sigs.k8s.io/), [minikube](https://minikube.sigs.k8s.io/), or similar
- `kubectl` (with `kustomize` support, built in since 1.14)
- Docker (to build the image)
- The [metrics-server](https://github.com/kubernetes-sigs/metrics-server) addon, required for the HorizontalPodAutoscaler to read CPU usage
  - minikube: `minikube addons enable metrics-server`
  - kind: `kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml` (add `--kubelet-insecure-tls` to the container args for local clusters without valid kubelet certs)

## 1. Build the image

```bash
docker build -t nyxgpt-api:local .
```

Load it into your cluster's local image cache (skip this for Docker Desktop's
built-in cluster, which shares the host's image cache):

```bash
# kind
kind load docker-image nyxgpt-api:local

# minikube
minikube image load nyxgpt-api:local
```

## 2. Configure the secret

The API key used for `[auth]` (see `k8s/configmap.yaml`) is supplied via a
Secret rather than committed to the repo:

```bash
cp k8s/secret.example.yaml k8s/secret.yaml
# edit k8s/secret.yaml and set a real api-key value
```

`k8s/secret.yaml` is gitignored — never commit real credentials.

## 3. Apply the manifests

```bash
kubectl apply -k k8s/
```

This creates the `nyxgpt` namespace, the ConfigMap, Secret, RBAC
(`k8s/rbac.yaml` — a `nyxgpt-api` ServiceAccount and a Role/RoleBinding
scoped to just the Deployment/Service operations below), the
`nyxgpt-api-blue` and `nyxgpt-api-green` Deployments (each with its own
HorizontalPodAutoscaler), and the `nyxgpt-api` Service. Both colors run at
once (1 replica each by default) so the inactive color can be health-checked
before it ever receives traffic; the Service starts pointed at `blue`.

Every `nyxgpt-api` Pod (blue/green/stable/canary) runs as the `nyxgpt-api`
ServiceAccount and ships its own `kubectl`, so `/admin/deploy` and
`/admin/canary` (and the API endpoints behind them) work when hit through a
Pod running in this cluster — they call `kubectl` in-cluster, authenticated
via the mounted ServiceAccount token, scoped by `k8s/rbac.yaml`'s Role.
This is what makes the Kubernetes deployment mode, not docker-compose, the
one where those dashboards are operable (see
[docker-compose.md](docker-compose.md#bluegreen-and-canary-deployment)).

## 4. Verify

```bash
kubectl -n nyxgpt get pods
kubectl -n nyxgpt get hpa
kubectl -n nyxgpt port-forward svc/nyxgpt-api 8000:8000
curl -H "X-API-Key: <your api-key>" http://127.0.0.1:8000/health
```

## Reaching Ollama (and Cassandra) from inside the cluster

`k8s/configmap.yaml` defaults `ollama.base_url` to
`http://host.docker.internal:11434`, which resolves on Docker Desktop-backed
clusters (kind/minikube using the `docker` driver on macOS/Windows). On Linux
this hostname does not resolve by default; either:

- Run Ollama itself in the cluster and point `base_url` at its Service, or
- Add a `hostAliases` entry to `k8s/deployment.yaml` mapping a hostname to
  your host's IP, or
- Use `minikube ssh -- ...` / the driver's documented host-access method.

The same applies to `rag.cassandra_hosts` if you enable RAG.

## Blue/Green Deployment

`k8s/deployment-blue.yaml` and `k8s/deployment-green.yaml` are two independent
Deployments for `nyxgpt-api`, distinguished by a `color: blue`/`color: green`
label. `k8s/service.yaml`'s selector includes `color`, so at any time the
`nyxgpt-api` Service routes to exactly one of them — there is no in-cluster
load balancer to configure, just a Service selector patch.

### Rolling out a new version

1. Build and load the new image (see step 1 above).
2. Update the **inactive** color's Deployment to the new image and wait for
   it to become ready, e.g. for green while blue is active:
   ```bash
   kubectl -n nyxgpt set image deployment/nyxgpt-api-green nyxgpt-api=nyxgpt-api:local
   kubectl -n nyxgpt rollout status deployment/nyxgpt-api-green
   ```
3. Check status and cut traffic over once the target color is healthy:
   ```bash
   nyxgpt deploy status
   nyxgpt deploy switch          # switches to whichever color is currently inactive
   nyxgpt deploy switch --to green   # or name the color explicitly
   ```
   `switch` refuses to run unless the target Deployment's Pods are fully
   Ready (the same `/health` readinessProbe used by `kubectl rollout status`),
   so a broken new version never receives traffic.
4. If something is wrong after cutover, roll back instantly:
   ```bash
   nyxgpt deploy rollback
   ```
   `rollback` switches back to the previously active color and — unlike
   `switch` — does not wait on a health check, since it's the emergency
   escape hatch.

### CLI reference

```bash
nyxgpt deploy status                    # active color + health of both colors
nyxgpt deploy switch [--to blue|green]  # health-checked cutover (default: the inactive color)
nyxgpt deploy switch --force            # skip the health gate
nyxgpt deploy rollback                  # switch back to the previously active color
```

All three commands accept `--namespace` to override the `[deploy] namespace`
config value (see `example.config.ini`); it defaults to `nyxgpt`.

### SRE/admin dashboard

The same status/switch/rollback actions are available from the web UI at
**Settings → Deployment** (`/admin/deploy`), backed by
`GET/POST /api/v1/deploy/status`, `/api/v1/deploy/switch`, and
`/api/v1/deploy/rollback` on the FastAPI backend.

### Deploy logging & metrics

Every switch/rollback decision is logged from `src/nyxgpt/deploy.py` with
structured fields (via the logging module's `extra={}`, rendered as JSON
when `[logging] format = json` -- see
[configuration.md](configuration.md#logging-section)): the switch attempt
(`deploy: switching traffic from <color> to <color>`), a refusal when the
target is unhealthy (`deploy: refusing switch from ... target unhealthy:
...`), the outcome (`deploy: switched traffic from ... to ...` on success,
`deploy: kubectl patch failed switching ...` on failure), and rollback
requests/outcomes (`deploy: rollback requested`, `deploy: rollback to
<color> succeeded/failed: ...`).

These are exported as Prometheus metrics (scraped from
[`/api/v1/metrics`](api.md#get-metrics)):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `nyxgpt_deploy_active_color` | Gauge | `color` | Whether a color is currently receiving traffic (1) or not (0) |
| `nyxgpt_deploy_switches_total` | Counter | `from_color`, `to_color`, `result` | Switch attempts, by direction and outcome (`ok`/`failed`) |
| `nyxgpt_deploy_rollbacks_total` | Counter | `result` | Rollback attempts, by outcome (`ok`/`failed`) |

The pre-provisioned Grafana **Blue-Green Deployment** dashboard
(`docker/grafana/dashboards/deployment.json`, auto-provisioned like the
other dashboards -- see [docker-compose.md's Monitoring
Dashboards](docker-compose.md#monitoring-dashboards)) shows the active
color, switch/rollback counts, and a Loki-backed switch/rollback timeline.
The Loki saved query behind that timeline (requires the `logging` Compose
profile -- see [Log Aggregation](docker-compose.md#log-aggregation)):

```logql
{job="nyxgpt"} |= `deploy:` |~ `switched|switching|rollback|refusing`
```

`/admin/deploy` links directly to both the Grafana dashboard and Grafana
Explore with this query pre-filled (when the `monitoring`/`logging`
profiles are active).

## Canary Deployment

`k8s/deployment-stable.yaml` and `k8s/deployment-canary.yaml` are two
independent Deployments for `nyxgpt-api`, both labeled
`app: nyxgpt-api-canary-pool` and distinguished by `track: stable`/
`track: canary`. `k8s/service-canary.yaml`'s selector only matches
`app: nyxgpt-api-canary-pool`, so it targets **both** Deployments' Pods at
once -- kube-proxy round-robins Service traffic evenly across every matching
Pod endpoint, so `canary_replicas / total_replicas` approximates the
canary's share of requests. There is no in-cluster proxy or ingress to
configure, just `kubectl scale` (wrapped by `nyxgpt canary`). Unlike the
blue/green pair, neither Deployment has an HPA attached -- autoscaling would
fight the canary tool's replica-count-based traffic split.

### Rolling out a new version

1. Build and load the new image (see step 1 above).
2. Update the canary Deployment to the new image (it starts at 0 replicas,
   so this has no effect on traffic yet):
   ```bash
   kubectl -n nyxgpt set image deployment/nyxgpt-api-canary nyxgpt-api=nyxgpt-api:local
   ```
3. Start the rollout at a small initial traffic weight:
   ```bash
   nyxgpt canary start --weight 10
   ```
4. Watch live error-rate/p95-latency metrics (from `/api/v1/metrics`) and
   check them against the configured thresholds -- this automatically rolls
   the canary back if either is breached:
   ```bash
   nyxgpt canary status
   nyxgpt canary evaluate
   ```
5. If `evaluate` reports the canary is safe, increase its traffic share:
   ```bash
   nyxgpt canary promote          # adds [canary] step_percent (default 25)
   ```
   Repeat steps 4-5 until `promote` reports the canary fully promoted to
   100%. At that point, deploy the new image to `nyxgpt-api-stable` and
   scale `nyxgpt-api-canary` back to 0 before starting the next rollout.
6. If something is wrong at any point, cut all traffic back to stable
   immediately:
   ```bash
   nyxgpt canary rollback
   ```
   `rollback` scales the canary Deployment to 0 first (removing it from the
   Service's endpoints) before restoring stable, and is not blocked by a
   flaky stable-scale-up -- it's the emergency escape hatch.

### CLI reference

```bash
nyxgpt canary status                 # rollout progress, stable/canary health, live metrics
nyxgpt canary start [--weight N]     # start a rollout at N% canary traffic (default: 10)
nyxgpt canary evaluate               # check metrics vs thresholds; auto-rollback on regression
nyxgpt canary promote [--step N]     # add N percentage points to canary's traffic share
nyxgpt canary rollback               # cut all traffic back to nyxgpt-api-stable
```

All five commands accept `--namespace` to override the `[canary] namespace`
config value (see `example.config.ini`); it defaults to `[deploy] namespace`,
then `nyxgpt`. `total_replicas`, `step_percent`,
`error_rate_threshold_percent`, `latency_p95_threshold_ms`, and
`min_requests_for_evaluation` are also configured in `[canary]`.

### SRE/admin dashboard

The same status/start/evaluate/promote/rollback actions are available from
the web UI at **Settings → Canary Rollout** (`/admin/canary`), backed by
`GET/POST /api/v1/canary/status`, `/start`, `/evaluate`, `/promote`, and
`/rollback` on the FastAPI backend.

### Metrics source

`evaluate` reads the same process-wide `ResourceMonitor` that backs
`/api/v1/metrics` (error rate over the last 1000 requests, HTTP 5xx; p95
latency) rather than a dedicated Prometheus scrape, since per-pod Prometheus
metrics haven't landed yet. This means `evaluate`'s error rate/latency
reflect whichever `nyxgpt-api` process the dashboard/CLI talks to, not a
canary-Pod-specific view.

### Canary logging & metrics

Every start/evaluate/promote/rollback decision is logged from
`src/nyxgpt/canary.py` with structured fields (via the logging module's
`extra={}`, rendered as JSON when `[logging] format = json` -- see
[configuration.md](configuration.md#logging-section)): rollout start
(`canary: starting/started rollout at N%`), evaluation results (`canary:
evaluate passed`, `canary: evaluate holding, insufficient data`, `canary:
evaluate detected regression ...; rolling back`), promotion (`canary:
promoting/promoted rollout from N% to M%`), and rollback (`canary: rolling
back/rolled back from N% (trigger=manual|auto)` -- `trigger` distinguishes
an operator-initiated rollback from `evaluate`'s automatic one).

These are exported as Prometheus metrics (scraped from
[`/api/v1/metrics`](api.md#get-metrics)):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `nyxgpt_canary_rollout_active` | Gauge | — | Whether a canary rollout is currently in progress (1) or idle (0) |
| `nyxgpt_canary_weight_percent` | Gauge | — | Current canary traffic weight percentage (0-100) |
| `nyxgpt_canary_evaluations_total` | Counter | `result` | Metric evaluations, by result (`pass`/`insufficient_data`/`regression`) |
| `nyxgpt_canary_events_total` | Counter | `action`, `result` | Lifecycle events (`start`/`promote`/`rollback`), by outcome |

The pre-provisioned Grafana **Canary Rollout** dashboard
(`docker/grafana/dashboards/canary.json`, auto-provisioned like the other
dashboards -- see [docker-compose.md's Monitoring
Dashboards](docker-compose.md#monitoring-dashboards)) shows rollout
active/idle, the live traffic split, evaluation results, lifecycle events,
and a Loki-backed start/promote/rollback timeline. The Loki saved query
behind that timeline (requires the `logging` Compose profile -- see [Log
Aggregation](docker-compose.md#log-aggregation)):

```logql
{job="nyxgpt"} |= `canary:` |~ `starting|started|promoting|promoted|rolling back|rolled back|regression`
```

`/admin/canary` links directly to both the Grafana dashboard and Grafana
Explore with this query pre-filled (when the `monitoring`/`logging`
profiles are active).

## Scaling behavior

Each color has its own HPA (`k8s/hpa-blue.yaml`, `k8s/hpa-green.yaml`) that
scales its Deployment between 1 and 3 replicas, targeting 70% average CPU
utilization. Because sessions and the vector store
default to in-container paths (`/root/.nyxGPT/...`), state is **not** shared
across replicas or persisted across restarts. If you need either, add a
`PersistentVolumeClaim`, mount it at `/root/.nyxGPT`, and switch the
Deployment's access pattern accordingly (not included here, since this
deployment targets a single-user local workflow rather than multi-replica
state sharing).
