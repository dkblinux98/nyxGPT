# Terraform (local infrastructure as code, optional)

`terraform/` declares the same core stack as [`docker-compose.yml`](../docker-compose.yml)
(Ollama, Cassandra, the FastAPI backend, and the web UI) via the
[`kreuzwerker/docker`](https://registry.terraform.io/providers/kreuzwerker/docker/latest)
provider, as an alternative bring-up path for anyone who prefers `terraform
apply`/`destroy` over `docker compose up`/`down` for local infrastructure
management (drift detection, plan review, etc).

This is **local-only infrastructure as code**, consistent with the project's
local-first [VISION.md](../product_management/VISION.md):

- No cloud provider modules (no AWS/GCP/Azure providers).
- No cloud networking or security groups — the stack runs on a single Docker
  bridge network on your workstation, same as Compose.
- No remote state backend — state is a local file (see
  [State Management](#state-management) below).

> **Scope note:** issue #2690 originally asked for cloud provider modules,
> cloud networking/security groups, and remote state management. The owner
> explicitly descoped that to local-only IaC on 2026-07-07 as out of step
> with `product_management/VISION.md`'s local-first constraint, then reversed and re-scoped the
> issue on 2026-07-09 with the local-only requirements implemented here
> (docker provider, no cloud networking/security groups, local state) —
> see the issue's comment thread for the full rationale. Cloud provider
> modules are intentionally not planned; there is no follow-up issue for
> them because the project has no cloud infrastructure to provision.

Scope: this covers the core stack only (`ollama`, `cassandra`, `api`, `web`).
The opt-in Compose profiles (`monitoring`, `logging`, `tracing`, `errors`) are
not modeled here — use
[`docker compose --profile <name> up`](docker-compose.md) for those, either
alongside this stack (they share the `nyxgpt` service names via
`docker/*.yml` configs) or standalone.

## One-command bring-up (`nyxgpt ops`)

```bash
nyxgpt ops install --terraform --local
```

Per the project's [Operational Command Wrapping](../CLAUDE.md) rule, this is
the supported way to run this deployment — no raw `brew`/`terraform`
commands required. It wraps the whole flow described below into one step:
migrates any pre-#3346 named-volume data into `~/.nyxGPT/volumes/` (see
[docker-compose.md#volumes](docker-compose.md#volumes); a no-op if you have
none), installs Terraform via the official HashiCorp tap if it isn't already
on PATH (`brew install terraform` no longer works on its own — HashiCorp
pulled the formula from homebrew-core after the 2023 BUSL relicense),
bootstraps `terraform.tfvars` from the example (a random `auth_api_key` is
generated unless you pass `--api-key` or answer the interactive prompt), and
runs `init` → `plan` → `apply`, then reports each container's health plus the
`api_url`/`web_url`/`ollama_url` outputs.

`--local` is required and explicit — it's the only locality implemented
today, and is the precursor to a future cloud deployment target. `--cloud`
is accepted by the CLI surface but rejected with a "not yet implemented"
message rather than silently doing the wrong thing.

The command refuses to start if the native/Compose stack already owns the
same host ports (8000/3000/11434/9042) — run `nyxgpt ops down` (or stop the
conflicting components) first. `nyxgpt ops status`/`doctor` show this
stack's container states alongside native/Compose.

Tear down with:

```bash
nyxgpt ops down --terraform
```

which runs `terraform destroy`, wrapped the same way.

The rest of this document walks through what those two commands do — useful
if you want to run the steps individually (e.g. to review `terraform plan`
output before applying) or troubleshoot a failure. It is reference material,
not something you're expected to type by hand.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5.0
  (installed automatically by `nyxgpt ops install --terraform --local` above)
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
the `ollama` container once (state persists in `~/.nyxGPT/volumes/ollama` --
the same host directory `docker-compose.yml` uses, so a model already pulled
there doesn't need re-downloading here, and vice versa; see
[docker-compose.md#volumes](docker-compose.md#volumes)):

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

(`nyxgpt ops down --terraform` wraps this — see [above](#one-command-bring-up-nyxgpt-ops).)

Removes the containers and network only. Unlike before #3346, this does
**not** discard pulled models or chat/RAG data: `ollama`/`cassandra`/`api`
bind-mount `~/.nyxGPT/volumes/{ollama,cassandra,nyxgpt-data}` on the host
(see [docker-compose.md#volumes](docker-compose.md#volumes)) rather than
Terraform-managed `docker_volume` resources, so there's nothing for
`terraform destroy` to remove — that data is shared with `docker-compose.yml`
and (Cassandra only) the native `nyxgpt ops install` Cassandra container, and
survives a destroy/apply cycle (or switching deployment modes entirely) by
design. To actually delete it, remove the host directories yourself or use
`nyxgpt ops down --volumes --yes-really` (Compose scope) — see
[`nyxgpt ops down`](ops.md#nyxgpt-ops-down).

## Relationship to the other deployment paths

| Path | Use when |
|------|----------|
| [`nyxgpt ops`](ops.md) (Homebrew) | Native macOS install, no containers |
| [Docker Compose](docker-compose.md) | One-command bring-up, including opt-in observability profiles |
| **Terraform** (this doc) | You want plan/apply/destroy semantics and drift detection for the core stack |
| [Kubernetes](kubernetes.md) | Local cluster (kind/minikube/k3s), blue/green and canary rollout of the API |

Only run one of Compose or Terraform at a time against the same host ports —
both default to `8000`/`3000`/`11434`/`9042`. This also matters for data
integrity now, not just ports: `ollama`/`cassandra`/`api` bind-mount the same
`~/.nyxGPT/volumes/{ollama,cassandra,nyxgpt-data}` host directories as
`docker-compose.yml` (see [docker-compose.md#volumes](docker-compose.md#volumes)),
so two Cassandra processes writing to `~/.nyxGPT/volumes/cassandra`
concurrently (e.g. if you've customized `cassandra_port` past the default
collision) would corrupt it. `nyxgpt ops install --terraform --local` and the
native `nyxgpt ops install` Cassandra container both refuse to start against
an already-running instance of the other for this reason.

## Image versions

`docker_image.ollama` and `docker_image.cassandra` in `main.tf` are pinned to
specific versions (no `:latest`), kept identical to the `ollama`/`cassandra`
services in `docker-compose.yml` and (Cassandra only) `CASSANDRA_IMAGE` in
`src/nyxgpt/ops.py` — see
[docker-compose.md#image-pinning](docker-compose.md#image-pinning) for the
full pinning policy and how to bump a version across all three definitions.
