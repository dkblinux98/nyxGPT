# Terraform (local infrastructure as code, optional)

`terraform/` declares the same core stack as [`docker-compose.yml`](../docker-compose.yml)
(Ollama, Cassandra, the FastAPI backend, and the web UI) via the
[`kreuzwerker/docker`](https://registry.terraform.io/providers/kreuzwerker/docker/latest)
provider, as an alternative bring-up path for anyone who prefers `terraform
apply`/`destroy` over `docker compose up`/`down` for local infrastructure
management (drift detection, plan review, etc).

The `terraform/` root module is **local-only infrastructure as code**,
consistent with the project's local-first
[VISION.md](../product_management/VISION.md):

- No cloud provider modules (no AWS/GCP/Azure providers) in *this* module.
- No cloud networking or security groups — the stack runs on a single Docker
  bridge network on your workstation, same as Compose.
- No remote state backend in *this* module — state is a local file (see
  [State Management](#state-management) below). The AWS module has one; see
  the note below.

> **The AWS substrate is a separate root module.** `terraform/aws/` (P6-8,
> #3509) provisions a VPC, public subnet, an SSH-only owner-IP-scoped
> security group, and one EC2 instance for a cloud deployment. It is driven
> by `nyxgpt cloud infra`, never by the `nyxgpt ops --terraform` commands on
> this page, and is documented in
> [Cloud (AWS)](cloud.md#nyxgpt-cloud-infra--provisioning-the-aws-substrate-p6-8-3509).
> That module *does* have a remote state option — an S3 backend with
> DynamoDB locking, set up by `nyxgpt cloud state migrate` (P6-9, #3510) and
> documented under
> [Remote state](cloud.md#remote-state-s3--dynamodb-locking-p6-9-3510).
> Everything below is about the local Docker stack, which stays on local
> state.

> **Scope note:** issue #2690 originally asked for cloud provider modules,
> cloud networking/security groups, and remote state management. The owner
> explicitly descoped that to local-only IaC on 2026-07-07 as out of step
> with `product_management/VISION.md`'s local-first constraint, then reversed and re-scoped the
> issue on 2026-07-09 with the local-only requirements implemented here
> (docker provider, no cloud networking/security groups, local state) —
> see the issue's comment thread for the full rationale.
>
> **Superseded in part (2026-08-10, #3509):** the "cloud provider modules are
> intentionally not planned" part of that scoping no longer holds. Phase 6's
> standing owner decision (2026-07-15) added an AWS deployment target, and
> P6-8 delivered `terraform/aws/` for it. The *local* module on this page is
> still docker-provider-only, exactly as scoped above.

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
materializes the Terraform configuration into `~/.nyxGPT/terraform/` from the
copy shipped inside the installed package, bootstraps `terraform.tfvars` from
the example (a random `auth_api_key` is generated unless you pass `--api-key`
or answer the interactive prompt), pulls the published `api`/`web` container
images, and runs `init` → `plan` → `apply`, then reports each container's
health plus the `api_url`/`web_url`/`ollama_url` outputs.

### Install modes: artifact (default) and `--dev`

The deployment has the same two install modes as the native install
([ops.md](ops.md#install-modes)), and records which one it is running:

| | what the `api`/`web` containers run | needs a checkout |
| --- | --- | --- |
| default (**artifact**) | the published `ghcr.io/dkblinux98/nyxgpt-api` / `nyxgpt-web` images | no |
| `--dev` | images built from the current checkout's working tree | yes |

```bash
nyxgpt ops install --terraform --local          # published images
nyxgpt ops install --terraform --local --dev    # this checkout's working tree
```

The artifact path is what makes this deployment runnable on a machine that
has never cloned the repository: the `.tf` files come from the installed
package, the images from the registry, and the api container's mounts from
`~/.nyxGPT`. `--dev` needs a checkout by definition and is refused (with the
path it looked at) when nyxgpt is running from an installed package.

`--dev` builds `nyxgpt-api:local`/`nyxgpt-web:local`, skipping the build and
reusing the current image when the app source (`src/nyxgpt/` +
`pyproject.toml` for api, `web/` for web) hasn't changed since it was last
built — the same reinstall-if-needed behavior the Homebrew path uses.

Images are published per release
(`.github/workflows/release-artifacts.yml`), so a version with no published
image of its own — a release candidate, or a development version — falls
back to the newest published one. That is version skew, so it is reported as
its own line in the install output and named in `ops status`; set
`NYXGPT_TF_API_IMAGE` / `NYXGPT_TF_WEB_IMAGE` to deploy specific images
instead (they may be any ref the local Docker daemon holds or can pull —
the container equivalent of `NYXGPT_ARTIFACT_DIR` for the native tarballs).

Which mode a deployment is in is recorded in
`~/.nyxGPT/install-mode-terraform.json` — its own file, separate from the
native services' marker — and reported by `nyxgpt ops status`, `nyxgpt ops
doctor` and the SRE dashboard's Infrastructure page:

```
Install mode (native api/web): artifact (published/vendored build -- the repo-less default)
Install mode (terraform): dev (images built from the working tree at /Users/you/nyxGPT)
```

A deployment that is *running* with no marker is reported as **not
recorded** — not as the artifact default. Every Terraform deployment made
before this marker existed was built from a working tree (that path had no
other mode), so calling an unrecorded live stack "artifact" would state the
opposite of what it is running. `ops status` prints the deployment as `not
recorded (...)` and tags its `api`/`web` `[unrecorded]`, `ops doctor` prints
it with the way to record it (an unknown build is not a fault, so it does
not fail the check), and the Infrastructure page badges it `IMAGES NOT
RECORDED`.
Redeploying with `nyxgpt up --terraform --local` (or `--dev`) records it. A
machine with the marker and *nothing deployed* keeps the artifact default:
there is no live stack for it to misdescribe.

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
not something you're expected to type by hand. It describes the working
directory `nyxgpt ops` uses, `~/.nyxGPT/terraform/`; the same files live in
`terraform/` in a checkout, and are the source the packaged copy is built
from.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5.0
  (installed automatically by `nyxgpt ops install --terraform --local` above)
- Docker, with the daemon running

## 1. Configure variables

`nyxgpt ops install --terraform --local` generates `docker/config.docker.ini`
for you (deriving it from your native `~/.nyxGPT/config.ini`). If you drive
Terraform by hand instead (below), generate it first with `nyxgpt ops env-sync`
— it's a git-ignored, per-machine file that `main.tf` bind-mounts into the `api`
container, and a missing source would make Docker create an empty directory in
its place:

```bash
nyxgpt ops env-sync   # derives docker/config.docker.ini from ~/.nyxGPT/config.ini

cd ~/.nyxGPT/terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and set:

- `auth_api_key` — a real value for the API's `[auth]` section (see
  [security.md](security.md)); required because the web container reaches
  the API over the Docker network rather than `localhost`

`terraform.tfvars` is gitignored — never commit real credentials.

The variables that select an install mode are deliberately *not* in that
file, because they change per run — `nyxgpt ops` passes them on the command
line, and so should you:

- `api_image` / `web_image` — the image refs the `api`/`web` containers run
- `build_from_source` — `true` builds them from `repo_path` instead (dev
  mode); `false`, the default, uses `api_image`/`web_image` as-is
- `repo_path` — absolute path to a nyxGPT checkout, used as the Docker build
  context when `build_from_source` is set (same role as the `context:` keys
  in `docker-compose.yml`); unused otherwise, and empty by default

## 2. State management

Terraform writes state next to the configuration it applies, so the state
for this stack is `~/.nyxGPT/terraform/terraform.tfstate` — alongside
nyxGPT's other local state, and outside any checkout.

The state file contains resource IDs, not secrets, but it is still
sensitive (it does include the values of any variables you pass, including
`auth_api_key`) — do not commit it.

If you deployed this stack before the working directory moved out of the
checkout, `nyxgpt ops install --terraform --local` copies your existing
`terraform/terraform.tfstate` **and** `terraform/terraform.tfvars` into the
new directory on its next run — so the same deployment keeps being managed
rather than a second one created alongside it, and it keeps the
`auth_api_key` you are already using instead of being handed a fresh random
one.

## 3. Apply

```bash
terraform plan -var=api_image=ghcr.io/dkblinux98/nyxgpt-api:latest \
               -var=web_image=ghcr.io/dkblinux98/nyxgpt-web:latest
terraform apply -var=api_image=ghcr.io/dkblinux98/nyxgpt-api:latest \
                -var=web_image=ghcr.io/dkblinux98/nyxgpt-web:latest
```

(add `-var=build_from_source=true -var=repo_path=/path/to/checkout` to build
the images from a working tree instead — the dev-mode equivalent of `docker
compose up --build`.)

This starts all four containers on a dedicated `nyxgpt-terraform` bridge
network. Models are not pulled by this raw-Terraform path: `nyxgpt ops install
--terraform --local` (above) runs a `required models` step after `apply` that
pulls the configured chat and embedding models into the `ollama` container and
fails the install if it cannot (#3824). Blobs persist in
`~/.nyxGPT/volumes/ollama` -- the same host directory `docker-compose.yml`
uses, so a model already pulled there is not re-downloaded here, and vice
versa; see [docker-compose.md#volumes](docker-compose.md#volumes).

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
| [Kubernetes](kubernetes.md) | Local cluster (kind/minikube/k3s), canary rollout of the API |
| [Cloud (AWS)](cloud.md) | You want the stack on an EC2 instance in your own AWS account, reachable only over an SSH tunnel (`nyxgpt cloud infra`) |

This deployment is watched by the same [self-heal watchdog](self-healing.md)
as every other deployment path -- see [self-healing.md#terraform-mode](self-healing.md#terraform-mode)
for how it checks/heals the `nyxgpt-tf-*` containers and the Docker socket
mount that makes that possible.

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
