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
see [ops.md](ops.md) / `nyxgpt ops` for running the full local stack, or the
Docker Compose setup for one-command bring-up of every component.

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

This creates the `nyxgpt` namespace and the ConfigMap, Secret, Deployment,
Service, and HorizontalPodAutoscaler.

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

## Scaling behavior

The HPA (`k8s/hpa.yaml`) scales `nyxgpt-api` between 1 and 3 replicas,
targeting 70% average CPU utilization. Because sessions and the vector store
default to in-container paths (`/root/.nyxGPT/...`), state is **not** shared
across replicas or persisted across restarts. If you need either, add a
`PersistentVolumeClaim`, mount it at `/root/.nyxGPT`, and switch the
Deployment's access pattern accordingly (not included here, since this
deployment targets a single-user local workflow rather than multi-replica
state sharing).
