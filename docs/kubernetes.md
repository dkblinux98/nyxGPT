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

`nyxgpt-api` is deployed as a **stable/canary pair** (`nyxgpt-api-stable`/
`nyxgpt-api-canary`), fronted by a single Service, supporting a deploy ->
gate -> promote (or rollback) release cycle with metrics-gated gradual
traffic shift; see [Canary Deployment](#canary-deployment). (Blue/green --
a separate two-color pair with instant all-or-nothing cutover -- was retired
in favor of canary: 0%/100% traffic weight reproduces the same cutover, plus
canary adds the gradual shift and auto-rollback blue/green never had.)

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
never committed), applies the kustomization, and snapshots Pod/Service
health.

The image build mirrors the Homebrew reinstall-if-needed behavior (see
[ops.md](ops.md)): it fingerprints the app source (`src/nyxgpt/` +
`pyproject.toml`) and only re-runs `docker build` when that source changed
since the image was last built, reporting `nyxgpt-api:local: built` /
`rebuilt (source changed since last build)` / `already up to date (skipped
rebuild)` instead of always rebuilding.

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
the canary rollout tooling that operates on top of this deployment once
it's up — useful if you want to run the bring-up steps individually or
troubleshoot a failure. It is reference material, not something you're
expected to type by hand.

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
`nyxgpt-api-stable` Deployment (4 replicas by default) and
`nyxgpt-api-canary` Deployment (0 replicas — idle until a rollout starts),
and both the `nyxgpt-api` and `nyxgpt-api-canary` Services (both select
every Pod from either Deployment; traffic split is by replica count, not
Service selector).

Every `nyxgpt-api` Pod (stable/canary) runs as the `nyxgpt-api`
ServiceAccount and ships its own `kubectl`, so `/admin/canary` (and the API
endpoints behind it) works when hit through a Pod running in this cluster —
it calls `kubectl` in-cluster, authenticated via the mounted ServiceAccount
token, scoped by `k8s/rbac.yaml`'s Role. This is what makes the Kubernetes
deployment mode, not docker-compose, the one where that dashboard is
operable (see [docker-compose.md](docker-compose.md#canary-deployment)).

## 4. Verify

```bash
kubectl -n nyxgpt get pods
kubectl -n nyxgpt get hpa
kubectl -n nyxgpt port-forward svc/nyxgpt-api 8000:8000
curl -H "X-API-Key: <your api-key>" http://127.0.0.1:8000/health
```

The `nyxgpt-api` Pods deployed here are also watched by the same
[self-heal watchdog](self-healing.md) as every other deployment path -- see
[self-healing.md#kubernetes-mode](self-healing.md#kubernetes-mode) for how it
checks Pod readiness via `kubectl get pods` and heals via `kubectl delete
pod`, on top of (not instead of) the liveness probes and the canary
mechanism described below. `k8s/rbac.yaml`'s `nyxgpt-api` Role grants the
`get`/`list`/`delete` on `pods` this needs, alongside what canary already
used.

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

## Canary Deployment

Canary is the sole deployment model (#3409 retired blue/green -- a separate
two-color pair with instant all-or-nothing cutover -- in favor of canary,
which is a strict superset for traffic purposes: 0%/100% weight reproduces
the same cutover, plus canary adds metrics-gated gradual shift and
auto-rollback blue/green never had).

`k8s/deployment-stable.yaml` and `k8s/deployment-canary.yaml` are two
independent Deployments for `nyxgpt-api`, both labeled
`app: nyxgpt-api-canary-pool` and distinguished by `track: stable`/
`track: canary`. `k8s/service.yaml` and `k8s/service-canary.yaml` both
select `app: nyxgpt-api-canary-pool`, targeting **both** Deployments' Pods
at once -- kube-proxy round-robins Service traffic evenly across every
matching Pod endpoint, so `canary_replicas / total_replicas` approximates
the canary's share of requests. There is no in-cluster proxy or ingress to
configure, just `kubectl scale`/`kubectl set image` (wrapped by
`nyxgpt canary`). Neither Deployment has an HPA attached -- autoscaling
would fight the canary tool's replica-count-based traffic split (see
[Scaling behavior](#scaling-behavior)).

**Coverage**: `api` today. `web`/`ollama` canary coverage is tracked in
follow-up issue #3419 -- neither has Kubernetes manifests yet, so it's new
infrastructure, not an extension of this pair. **Cassandra is explicitly out
of scope**: two Cassandras behind a canary split would mean two divergent
datasets, which is a data-migration problem, not a traffic-split problem. A
schema/version-upgrade story for Cassandra will be designed when a version
upgrade actually requires one (a future issue, not this one).

### The deploy -> gate -> promote cycle

1. **Deploy** the current checkout to canary only -- builds a versioned
   image (`<project version>-<git short sha>`, e.g. `nyxgpt-api:2.0.0-abc1234`
   -- never the mutable `:local` tag), patches only the canary Deployment's
   image, and waits for its rollout. Stable is never touched, even on
   failure:
   ```bash
   nyxgpt canary deploy
   ```
2. **Gate**: start the rollout at a small initial traffic weight, then watch
   live error-rate/p95-latency metrics (from `/api/v1/metrics`) against the
   configured thresholds -- `evaluate` automatically rolls the canary back
   if either is breached:
   ```bash
   nyxgpt canary start --weight 10
   nyxgpt canary status
   nyxgpt canary evaluate
   ```
3. **Promote**: if `evaluate` reports the canary is safe, increase its
   traffic share:
   ```bash
   nyxgpt canary promote          # adds [canary] step_percent (default 25)
   ```
   Repeat steps 2-3 until `promote` reaches 100%. At that final step,
   `promote` copies the canary's image version onto `nyxgpt-api-stable`,
   waits for stable's rollout to become healthy, then scales canary back to
   0 and stable back to `total_replicas` -- stable now runs the promoted
   version at 100% traffic, and the cycle is complete. `promote` refuses to
   shift more traffic to the canary at every step (including this final
   one) unless the canary is currently healthy, and if stable's rollout
   onto the new version fails, canary is left running untouched so you can
   retry or roll back.
4. If something is wrong at any point, cut all traffic back to stable
   immediately:
   ```bash
   nyxgpt canary rollback
   ```
   `rollback` scales the canary Deployment to 0 first (removing it from the
   Service's endpoints) before restoring stable, and is not blocked by a
   flaky stable-scale-up -- it's the emergency escape hatch.

### CLI reference

```bash
nyxgpt canary status                 # rollout progress, stable/canary health + version, live metrics
nyxgpt canary deploy                 # build a versioned image and deploy it to canary only
nyxgpt canary start [--weight N]     # start a rollout at N% canary traffic (default: 10)
nyxgpt canary evaluate               # check metrics vs thresholds; auto-rollback on regression
nyxgpt canary promote [--step N]     # add N percentage points to canary's traffic share (100% promotes)
nyxgpt canary rollback               # cut all traffic back to nyxgpt-api-stable
```

All six commands accept `--namespace` to override the `[canary] namespace`
config value (see `example.config.ini`); it defaults to `nyxgpt`.
`total_replicas`, `step_percent`, `error_rate_threshold_percent`,
`latency_p95_threshold_ms`, and `min_requests_for_evaluation` are also
configured in `[canary]`.

### Honest status, mode-aware (#3409)

`nyxgpt canary status` / `/admin/canary` report each track's health as one
of three honest states rather than a binary healthy/unhealthy that treats
"not installed" as an alarm:

- **Not deployed** -- the cluster is unreachable, the Deployment doesn't
  exist yet (run `nyxgpt ops install --kubernetes`), or it exists at 0
  desired replicas (the canary Deployment's normal idle state before a
  rollout starts). Neutral, not an alarm.
- **Unhealthy** -- the Deployment exists with `>0` desired replicas but not
  all its Pods are ready.
- **Healthy** -- fully ready, and the response includes the image version
  each track is running.

A genuine kubectl failure against a reachable cluster (e.g. an RBAC denial)
is reported as its own distinguishable **error** state, never silently
folded into "not deployed". Outside Kubernetes mode (native/terraform),
`status`/`/admin/canary` say so explicitly and name which mode provides
canary, instead of inferring "not applicable" from a failed kubectl call.

### SRE/admin dashboard

The same status/deploy/start/evaluate/promote/rollback actions are available
from the web UI at **Settings → Canary Operations** (`/admin/canary`),
backed by `GET/POST /api/v1/canary/status`, `/deploy`, `/start`,
`/evaluate`, `/promote`, and `/rollback` on the FastAPI backend.

### Metrics source

`evaluate` reads the same process-wide `ResourceMonitor` that backs
`/api/v1/metrics` (error rate over the last 1000 requests, HTTP 5xx; p95
latency) rather than a dedicated Prometheus scrape, since per-pod Prometheus
metrics haven't landed yet. This means `evaluate`'s error rate/latency
reflect whichever `nyxgpt-api` process the dashboard/CLI talks to, not a
canary-Pod-specific view.

### Canary logging & metrics

Every deploy/start/evaluate/promote/rollback decision is logged from
`src/nyxgpt/canary.py` with structured fields (via the logging module's
`extra={}`, rendered as JSON when `[logging] format = json` -- see
[configuration.md](configuration.md#logging-section)): the deploy attempt
and outcome (`canary: deploying <tag> to nyxgpt-api-canary only`, `canary:
Deployed <tag> to nyxgpt-api-canary`), rollout start (`canary: starting/
Started rollout at N%`), evaluation results (`canary: evaluate passed`,
`canary: evaluate holding, insufficient data`, `canary: evaluate detected
regression ...; rolling back`), promotion (`canary: promoting rollout from
N% to M%`, `canary: Promoted <version> to nyxgpt-api-stable ...`), and
rollback (`canary: rolling back/rolled back from N% (trigger=manual|auto)`
-- `trigger` distinguishes an operator-initiated rollback from `evaluate`'s
automatic one). Every deploy/start/promote/rollback action is also recorded
as an ops lifecycle event (`nyxgpt_ops_actions_total{command="canary-<action>"}`,
see [self-healing.md's Self-heal restarts vs. operator
actions](self-healing.md#self-heal-restarts-vs-operator-nyxgpt-ops-actions)).

These are exported as Prometheus metrics (scraped from
[`/api/v1/metrics`](api.md#get-metrics)):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `nyxgpt_canary_rollout_active` | Gauge | — | Whether a canary rollout is currently in progress (1) or idle (0) |
| `nyxgpt_canary_weight_percent` | Gauge | — | Current canary traffic weight percentage (0-100) |
| `nyxgpt_canary_evaluations_total` | Counter | `result` | Metric evaluations, by result (`pass`/`insufficient_data`/`regression`) |
| `nyxgpt_canary_events_total` | Counter | `action`, `result` | Lifecycle events (`deploy`/`start`/`promote`/`rollback`), by outcome |
| `nyxgpt_canary_track_version_info` | Gauge | `track`, `version` | 1 for the (track, version) currently observed on that track's Deployment |

The pre-provisioned Grafana **Canary Rollout** dashboard
(`docker/grafana/dashboards/canary.json`, auto-provisioned like the other
dashboards -- see [docker-compose.md's Monitoring
Dashboards](docker-compose.md#monitoring-dashboards)) shows rollout
active/idle, the live traffic split, evaluation results, lifecycle events, a
per-track version table, and a Loki-backed deploy/start/promote/rollback
timeline. The Loki saved query behind that timeline (requires the `logging`
Compose profile -- see [Log Aggregation](docker-compose.md#log-aggregation)):

```logql
{job="nyxgpt"} |= `canary:` |~ `deploying|Deployed|starting|started|promoting|Promoted|rolling back|rolled back|regression`
```

`/admin/canary` links directly to both the Grafana dashboard and Grafana
Explore with this query pre-filled (when the `monitoring`/`logging`
profiles are active).

## Scaling behavior

Neither the stable nor canary Deployment has an HPA attached -- autoscaling
would fight canary's replica-count-based traffic split (see [Canary
Deployment](#canary-deployment)). `nyxgpt-api-stable` runs a fixed
`total_replicas` (4 by default, `[canary] total_replicas`); there is no
`nyxgpt`-wrapped command for changing steady-state replica count yet, so if
you need more capacity today, raising it is a manual `kubectl` escape hatch
pending a wrapper (tracked as follow-up work), not a first-class operation --
prefer adjusting `[canary] total_replicas` and letting the next rollout apply
it where that's sufficient. Because sessions and the vector
store default to in-container paths (`/root/.nyxGPT/...`), state is **not**
shared across replicas or persisted across restarts. If you need either, add
a `PersistentVolumeClaim`, mount it at `/root/.nyxGPT`, and switch the
Deployment's access pattern accordingly (not included here, since this
deployment targets a single-user local workflow rather than multi-replica
state sharing).
