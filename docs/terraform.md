# Terraform (local infrastructure as code, optional)

`terraform/` declares the same core stack as [`docker-compose.yml`](../docker-compose.yml)
(Ollama, Cassandra, the FastAPI backend, and the web UI) via the
[`kreuzwerker/docker`](https://registry.terraform.io/providers/kreuzwerker/docker/latest)
provider, as an alternative bring-up path for anyone who prefers `terraform
apply`/`destroy` over `docker compose up`/`down` for local infrastructure
management (drift detection, plan review, etc).

This is **local-only infrastructure as code**, consistent with the project's
local-first [VISION.md](../VISION.md):

- No cloud provider modules (no AWS/GCP/Azure providers).
- No cloud networking or security groups — the stack runs on a single Docker
  bridge network on your workstation, same as Compose.
- No remote state backend — state is a local file (see
  [State Management](#state-management) below).

Scope: this covers the core stack only (`ollama`, `cassandra`, `api`, `web`).
The opt-in Compose profiles (`monitoring`, `logging`, `tracing`, `errors`) are
not modeled here — use
[`docker compose --profile <name> up`](docker-compose.md) for those, either
alongside this stack (they share the `nyxgpt` service names via
`docker/*.yml` configs) or standalone.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5.0
- Docker, with the daemon running

## 1. Configure variables

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and set:

- `repo_path` — absolute path to your nyxGPT checkout (used as the Docker
  build context for the `api`/`web` images, same role as the `context:` keys
  in `docker-compose.yml`)
- `auth_api_key` — a real value for the API's `[auth]` section (see
  [security.md](security.md)); required because the web container reaches
  the API over the Docker network rather than `localhost`

`terraform.tfvars` is gitignored — never commit real credentials.

## 2. State management

By default Terraform writes state to `terraform/terraform.tfstate`. To keep
it alongside nyxGPT's other local state instead, point the local backend at
`~/.nyxGPT/terraform/` at init time (`~` does not expand inside a `backend`
block, so this is set via `-backend-config` rather than a variable):

```bash
mkdir -p ~/.nyxGPT/terraform
terraform init -backend-config="path=$HOME/.nyxGPT/terraform/terraform.tfstate"
```

The state file contains resource IDs, not secrets, but it is still
sensitive (it does include the values of any variables you pass, including
`auth_api_key`) — do not commit it. If you skip the `-backend-config` flag,
`terraform.tfstate` is created in `terraform/` and is gitignored there too.

## 3. Apply

```bash
terraform plan
terraform apply
```

This builds the `api`/`web` images from `repo_path` (equivalent to `docker
compose up --build`) and starts all four containers on a dedicated
`nyxgpt-terraform` bridge network. First boot still needs a model pulled into
the `ollama` container once (state persists in the `nyxgpt_tf_ollama_data`
volume across `terraform apply`/`destroy` cycles as long as the volume isn't
deleted):

```bash
docker exec nyxgpt-tf-ollama ollama pull qwen2.5:0.5b
```

Then verify using the outputs Terraform prints (`api_url`, `web_url`,
`ollama_url`):

```bash
curl -H "X-API-Key: <auth_api_key from terraform.tfvars>" http://localhost:8000/health
```

Open the web UI at the printed `web_url` (default
[http://localhost:3000](http://localhost:3000)).

## 4. Destroy

```bash
terraform destroy
```

Removes the containers, network, and named volumes (`nyxgpt_tf_ollama_data`,
`nyxgpt_tf_cassandra_data`, `nyxgpt_tf_nyxgpt_data`) — this discards pulled
models and any chat/RAG data stored by this stack, same as `docker compose
down -v`. Omit the volumes from state first (`terraform state rm
docker_volume.ollama_data`, etc.) if you want to keep pulled models across a
destroy/apply cycle.

## Relationship to the other deployment paths

| Path | Use when |
|------|----------|
| [`nyxgpt ops`](ops.md) (Homebrew) | Native macOS install, no containers |
| [Docker Compose](docker-compose.md) | One-command bring-up, including opt-in observability profiles |
| **Terraform** (this doc) | You want plan/apply/destroy semantics and drift detection for the core stack |
| [Kubernetes](kubernetes.md) | Local cluster (kind/minikube/k3s), blue/green and canary rollout of the API |

Only run one of Compose or Terraform at a time against the same host ports —
both default to `8000`/`3000`/`11434`/`9042`.
