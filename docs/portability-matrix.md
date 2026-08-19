# Portability matrix & clean-machine acceptance

nyxGPT must be installable and runnable **without checking out or downloading
the code repository** (CLAUDE.md, *Repo-less Portability*, 2026-08-01).
Distribution is via published artifacts — the PyPI wheel, the remote Homebrew
tap, and the `ghcr.io` container images — never `git clone`. A source checkout
stays supported for development (`pip install -e .`), but no user-facing
install or operate flow may require one. That includes
[`nyxgpt up --dev`](ops.md#--dev-run-the-current-checkout-without-an-artifact-build)
and its Kubernetes
[equivalent](kubernetes.md#install-modes-artifact-and---dev), the opt-in mode
that builds from a checkout's working tree: it is a development and
mid-stream-testing path, never the default, and every target below still
installs and is accepted from published artifacts.

This page is the matrix of the five in-scope targets and the acceptance run
that demonstrates them. **Windows is explicitly out of scope.**

The matrix is not maintained by hand here. It lives in
[`src/nyxgpt/portability.py`](../src/nyxgpt/portability.py) as data, which
both this page and the CLI render, so a table that claims a target installs
from published artifacts cannot survive someone reintroducing a `git clone`.
Print the live version any time:

```bash
nyxgpt ops portability            # the report below, current as of your install
nyxgpt ops portability --json     # machine-readable
nyxgpt ops portability --strict   # exits non-zero while any target needs a checkout
```

There is no dashboard screen for the matrix (#3803): it describes the
product, not your machine, so there is nothing on a page to observe or act
on. This page and the CLI are the two ways to read it.

## What is checked, mechanically

Every row is checked on each run, not merely described:

| Check | What it asserts |
|---|---|
| `repo_less` | No install/operate/teardown command fetches source — no `git clone`, no `git@` remote, no source-archive download. What arrives is a published artifact. |
| `wrapped` | No command is a raw `docker`/`docker compose`/`kubectl`/`terraform`/`helm` invocation (CLAUDE.md, *Operational Command Wrapping*). Package managers (`pip`, `brew`) are allowed for the install step only — they are how an artifact arrives, not orchestrators. |
| `evidence` | Every path a row cites as evidence exists. Skipped, not failed, when there is no checkout to resolve paths against — which is the normal state on a target machine. |

A row is **acceptance-ready** when its checks pass *and* it has no open gap.

## The matrix

| Target | Published artifact | Install (clean machine) | Operate | Tear down | State |
|---|---|---|---|---|---|
| macOS native (Homebrew + launchd) | Remote tap `dkblinux98/nyxgpt` | `brew tap dkblinux98/nyxgpt`<br>`brew tap-trust dkblinux98/nyxgpt`<br>`brew install nyxgpt-api nyxgpt-web` | `nyxgpt up` | `nyxgpt down` | Install **verified in CI** (`macos-brew-smoke.yml`); operate half owner acceptance |
| Linux native (systemd `--user`) | PyPI wheel | `pip install nyxgpt` | `nyxgpt up` | `nyxgpt down` | **Verified in CI** on every release |
| Docker / Compose | `ghcr.io/dkblinux98/nyxgpt-api`, `…/nyxgpt-web` | `pip install nyxgpt` | `nyxgpt up`, `nyxgpt ops observability` | `nyxgpt down` | **Gap** — see below |
| Kubernetes | PyPI wheel + the `nyxgpt-api`/`nyxgpt-web` release tarballs | `pip install nyxgpt` | `nyxgpt ops install --kubernetes --local` | `nyxgpt ops down --kubernetes` | **Verified in CI** (`k8s-artifact-smoke.yml`) |
| AWS EC2 (private access path) | PyPI wheel, workstation and instance | `pip install nyxgpt`<br>`nyxgpt cloud credentials-setup` | `nyxgpt cloud deploy`, `nyxgpt cloud tunnel` | `nyxgpt cloud destroy --yes` | Owner acceptance — CI has no billable AWS account |
| AWS EC2 on Kubernetes (single-node k3s) | As above, plus the `nyxgpt-api`/`nyxgpt-web` release tarballs built into images on the instance | `pip install nyxgpt`<br>`nyxgpt cloud credentials-setup` | `nyxgpt cloud deploy --kubernetes`, `nyxgpt cloud tunnel`, `nyxgpt cloud canary` | `nyxgpt cloud destroy --yes` | Cluster bootstrap **verified in CI** (`k3s-cloud-smoke.yml`); the AWS half is owner acceptance, same as the row above |

### Evidence

- **Linux native** — `release-artifacts.yml`'s `artifact-install-smoke` job
  installs from the just-published PyPI wheel with **no repo checkout anywhere
  in the job**, brings the stack up, verifies the `systemd --user` units and
  endpoints answer, and tears it down again.
  [`linux-native-smoke.yml`](../.github/workflows/linux-native-smoke.yml)
  covers the same path on every push.
- **AWS EC2** — the instance installs a published release and never clones:
  `cloud_deploy.render_provision_script` and
  `cloud_provision.render_user_data` are both asserted clone-free by unit
  tests, and `release-artifacts.yml`'s `ec2-linux-user-data-smoke` job
  *executes the rendered Linux bootstrap script itself* on a fresh,
  never-logged-in account — the same state `ec2-user` is in on first boot.
  What CI cannot do is spend money: `terraform-aws-validate.yml` runs with
  dummy credentials, so the live deploy/smoke/teardown is the owner
  acceptance run below.
- **macOS native** — `release-artifacts.yml` stamps both formulas and pushes
  them to the remote tap when `HOMEBREW_TAP_REPO` is configured, and uploads
  them as a workflow artifact otherwise; either way the tarballs they install
  from are published on a companion `<version>-homebrew` release, because a
  published release is immutable (see
  [homebrew.md](homebrew.md#where-the-tarballs-are-published)). Coverage is split:
  [`macos-brew-smoke.yml`](../.github/workflows/macos-brew-smoke.yml) installs
  the formulas on a hosted `macos-15` runner — the working tree's own recipe on
  every formula change, and the published candidate from the real tap after
  every rc cut — so an install-breaking recipe fails in CI rather than on a
  clean Mac. That job also asserts the keg is **operable**, not just built:
  `nyxgpt` has to be on PATH by name after `brew install`, and
  `nyxgpt ops status` has to run on a machine with no stack, no Docker and no
  checkout (#3850 — the keg installed cleanly and answered
  `command not found`, because every check reached into the keg's venv
  instead of asking what the operator can type). The rest of the **operate**
  half — brew services / launchd reconciliation and a real `nyxgpt up` —
  stays owner-verified on the owner's workstation.
- **EC2 Mac** targets (`mac2.metal`, `mac1.metal`) are documentation-verified
  only: hosted macOS runners cover a brew install but are not EC2 instances,
  and a Dedicated Host bills a 24-hour minimum. See [cloud.md](cloud.md)'s
  target-OS support matrix.

### Open gaps

One gap remains, and it is a product gap rather than a documentation one: the
images **are** published on every release, but no wrapped command consumes them
from the registry yet.

1. **Docker / Compose** — `docker-compose.yml`'s `api` and `web` services
   carry a `build:` context (`.` and `./web`), so Compose builds them from a
   checkout instead of pulling `ghcr.io/dkblinux98/nyxgpt-api`/`-web`. The
   observability half is already repo-less: the Compose file and its templates
   ship as package data (`nyxgpt.resources`, #3621) and
   `nyxgpt ops observability` starts the monitoring/logging/tracing/errors
   profiles from public images with no checkout.

**Terraform closed its gap in #3835.** The `.tf` configuration ships as package
data (`nyxgpt.resources.terraform.local`, materialized into `~/.nyxGPT/terraform`)
instead of being read from `REPO_ROOT`, and `nyxgpt ops install --terraform
--local` pulls the published `ghcr.io/dkblinux98/nyxgpt-api`/`-web` images rather
than building the working tree. `--dev` still builds it, records that it did, and
is refused where there is no checkout. The proof is executed, not inspected: the
`terraform-artifact-smoke` job in
[`terraform-local-smoke.yml`](../.github/workflows/terraform-local-smoke.yml)
installs the wheel into a venv with no repository in reach, resolves and pulls the
real published images, deploys, and requires the stack to serve.

**Kubernetes closed its gap in #3834.** The manifests ship as package data
(`nyxgpt.resources.k8s`, synced to `~/.nyxGPT/k8s`) instead of being read from
`REPO_ROOT`, and `nyxgpt ops install --kubernetes --local` builds both images
from the published `nyxgpt-api`/`nyxgpt-web` release tarballs — the same
artifacts the Homebrew formulas install, and the ones a *release candidate*
publishes, which a container image is not. `--dev` still builds the working
tree, records that it did, and is refused where there is no checkout. The
proof is executed, not inspected:
[`k8s-artifact-smoke.yml`](../.github/workflows/k8s-artifact-smoke.yml)
installs the wheel into a venv with no repository in reach, asserts the pre-fix
build context and manifests are genuinely absent, brings the cluster up and
requires a real chat to answer.

While the Compose gap is open, `nyxgpt ops portability --strict` exits
non-zero. That is deliberate: closing it is what turns the strict gate green,
and the gate then keeps it closed. (There is no dashboard surface for this — the matrix
describes the product's portability claims, not the state of any one machine, so
the CLI and `GET /api/v1/ops/portability` are its only readers, see #3803.)

## Clean-machine acceptance run

This is the Phase 6 capstone acceptance (P6-16, #3516): from a machine that has
never seen this repository, one install and one command yield a provisioned,
deployed, monitored, self-healing nyxGPT reachable **only** over the private
access path — then teardown leaves nothing billed behind.

Run it on a clean macOS or Linux machine. Everything below is a `nyxgpt`
command; you never type `ssh`, `terraform`, `docker`, or `kubectl`.

### Prerequisites

- Python 3.11+ (Linux/macOS), or Homebrew on macOS for the native path.
- An AWS account you are willing to bill for the duration of the run, and
  permission to create a VPC, a security group, and one EC2 instance.
- **No repo checkout.** If `git rev-parse --show-toplevel` succeeds in your
  working directory, you are not testing the acceptance path.

### The sequence

| # | Command | Expected result |
|---|---|---|
| 1 | `pip install nyxgpt` | `nyxgpt --version` prints the released version; no checkout exists |
| 2 | `nyxgpt cloud credentials-setup` | AWS credentials collected and validated, routed to `~/.aws/credentials` or the OS keychain — never `config.ini` |
| 3 | `nyxgpt cloud deploy` | substrate applied, instance provisioned from published artifacts, observability profiles up, self-heal enabled, tunnel open, `/health` 200, localhost URLs printed |
| 4 | `nyxgpt cloud status` | the SSH target, the public IP and SSH-only ingress from your own CIDR are printed — every app and observability URL is a `localhost` one through the tunnel |
| 5 | `nyxgpt cloud ops status` | the instance's own `nyxgpt ops status` answers over the wrapped SSH path — container state with no hand-rolled `ssh` and no raw `docker compose` |
| 6 | `nyxgpt cloud smoke --skip-deploy --keep` | chat round-trip, RAG ingest + query, and every observability UI green |
| 7 | `nyxgpt self-heal status` | `enabled: true`, every component healthy |
| 8 | `nyxgpt cloud destroy --yes` | tunnel closed, substrate destroyed, no billed resources left |

`nyxgpt ops portability` prints this same sequence, so the terminal you are
accepting from can always tell you the next step.

### Accepting code that isn't released yet

Step 1 installs from PyPI, so by default the run exercises the last **stable**
release — not the release-branch tip carrying the acceptance failures you just
fixed. Pin a [release candidate](cloud.md#pypi-publishing-rc-and-stable)
instead — the sprint autopilot cuts one when the sprint reaches
agentic-work-complete, and `nyxgpt release publish --publish` cuts one on
demand (#3727):

| # | Command |
|---|---------|
| 1 | `pip install nyxgpt==3.0.0rc3` |
| 3 | `nyxgpt cloud deploy --version 3.0.0rc3` |

On **macOS**, where the repo-less install is `brew`, an `rc` is installed
from its own formulas instead — an rc publish stamps them into the same tap
alongside the stable ones:

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # one-time per machine (docs/homebrew.md)
brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc
```

`brew install nyxgpt-api` is unaffected and stays on the latest stable
release ([docs/homebrew.md](homebrew.md#release-candidate-formulas-rc-channel)).

Nothing else about the sequence changes: the build is a published artifact
like any other, so the run stays repo-less. Candidates are acceptance-only
and are never announced; `pip install nyxgpt` still resolves
to the stable release, because pip excludes pre-releases from an unpinned
requirement.

### Why step 6 passes `--skip-deploy`

A bare `nyxgpt cloud smoke` deploys a stack of its own, tests it, and destroys
it. That proves the smoke test works; it proves nothing about the deployment
you are accepting. `--skip-deploy` points it at the deployment step 3 made, and
`--keep` leaves that deployment up for steps 7 and 8 instead of tearing it down
early. (Without `--keep`, the smoke run destroys the deployment when it
finishes — which is the right default everywhere except inside an acceptance
run, since it guarantees no run can leave billed resources behind.)

### What to record

For each numbered step: the command, its exit status, and the operator-facing
output (URLs, the smoke summary, the `self-heal status` payload). Two extra
pieces of evidence make the acceptance auditable:

- Step 4's ingress line — the proof that nothing is publicly reachable.
- After step 8, an AWS console or `nyxgpt cloud status` check showing
  no instance, no volume, and no security group left behind.

### Non-AWS rows

The three non-AWS targets are accepted the same way, on a clean machine each:

```bash
# macOS native
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # one-time: Homebrew gates third-party taps
brew install nyxgpt-api nyxgpt-web
nyxgpt up && nyxgpt ops status && nyxgpt down

# Linux native (systemd --user)
pip install nyxgpt
nyxgpt up && nyxgpt ops status && nyxgpt down

# Kubernetes (local cluster -- provisioned by the command if there isn't one)
pip install nyxgpt
nyxgpt ops install --kubernetes --local
nyxgpt ops status && nyxgpt ops down --kubernetes

# AWS EC2 on Kubernetes (single-node k3s installed on the instance)
pip install nyxgpt
nyxgpt cloud deploy --kubernetes
nyxgpt cloud status && nyxgpt cloud canary status
nyxgpt cloud destroy --yes
```

Compose cannot be accepted from a clean machine until the gap above closes;
`nyxgpt ops portability` is the check that says whether it has.

## Related

- [cloud.md](cloud.md) — `nyxgpt cloud` in full: substrate, remote state,
  deploy, tunnel, smoke, and the target-OS support matrix
- [homebrew.md](homebrew.md) — the remote tap and how releases stamp it
- [systemd.md](systemd.md) — the Linux native path
- [ops.md](ops.md) — `nyxgpt ops`, including `portability`
- [self-healing.md](self-healing.md) — the watchdog a cloud deploy enables
- `product_management/PHASE_6_PLAN.md` — P6-16's specification
