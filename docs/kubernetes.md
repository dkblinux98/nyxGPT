# Kubernetes Deployment (local clusters)

nyxGPT can be deployed to a **local** Kubernetes cluster (kind, minikube, k3s,
Docker Desktop's built-in cluster, etc.) as an alternative to the Homebrew /
`nyxgpt ops` workflow described in [ops.md](ops.md). This is aimed at running
nyxGPT on your own workstation, in line with the project's local-first
[VISION.md](../VISION.md) — it is not a guide for deploying to a cloud
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
over between them with zero downtime and roll back instantly.

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

This creates the `nyxgpt` namespace, the ConfigMap, Secret, the
`nyxgpt-api-blue` and `nyxgpt-api-green` Deployments (each with its own
HorizontalPodAutoscaler), and the `nyxgpt-api` Service. Both colors run at
once (1 replica each by default) so the inactive color can be health-checked
before it ever receives traffic; the Service starts pointed at `blue`.

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
