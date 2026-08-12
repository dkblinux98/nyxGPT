# Portability matrix & clean-machine acceptance

nyxGPT must be installable and runnable **without checking out or downloading
the code repository** (CLAUDE.md, *Repo-less Portability*, 2026-08-01).
Distribution is via published artifacts — the PyPI wheel, the remote Homebrew
tap, and the `ghcr.io` container images — never `git clone`. A source checkout
stays supported for development (`pip install -e .`), but no user-facing
install or operate flow may require one.

This page is the matrix of the five in-scope targets and the acceptance run
that demonstrates them. **Windows is explicitly out of scope.**

The matrix is not maintained by hand here. It lives in
[`src/nyxgpt/portability.py`](../src/nyxgpt/portability.py) as data, and both
this page and the SRE dashboard render it, so a table that claims a target
installs from published artifacts cannot survive someone reintroducing a
`git clone`. Print the live version any time:

```bash
nyxgpt ops portability            # the report below, current as of your install
nyxgpt ops portability --json     # machine-readable
nyxgpt ops portability --strict   # exits non-zero while any target needs a checkout
```

The same report is on the SRE dashboard at **Admin → Portability and
Acceptance** (`/admin/portability`), read-only: the matrix describes the
product, not your machine, so there is nothing on that page to act on.

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
| macOS native (Homebrew + launchd) | Remote tap `dkblinux98/homebrew-nyxgpt` | `brew tap dkblinux98/homebrew-nyxgpt`<br>`brew install nyxgpt-api nyxgpt-web` | `nyxgpt up` | `nyxgpt down` | Owner acceptance — no Apple Silicon CI runner exists |
| Linux native (systemd `--user`) | PyPI wheel | `pip install nyxgpt` | `nyxgpt up` | `nyxgpt down` | **Verified in CI** on every release |
| Docker / Compose | `ghcr.io/dkblinux98/nyxgpt-api`, `…/nyxgpt-web` | `pip install nyxgpt` | `nyxgpt up`, `nyxgpt ops observability` | `nyxgpt down` | **Gap** — see below |
| Kubernetes | the same two images | `pip install nyxgpt` | `nyxgpt ops install --kubernetes --local` | `nyxgpt ops down --kubernetes` | **Gap** — see below |
| AWS EC2 (private access path) | PyPI wheel, workstation and instance | `pip install nyxgpt`<br>`nyxgpt cloud credentials-setup` | `nyxgpt cloud deploy`, `nyxgpt cloud tunnel` | `nyxgpt cloud destroy --yes` | Owner acceptance — CI has no billable AWS account |

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
  them to the remote tap when `HOMEBREW_TAP_REPO` is configured (and always
  attaches them to the GitHub Release otherwise; see
  [homebrew.md](homebrew.md#remote-tap)). The brew/launchd reconciliation
  itself is owner-verified — GitHub Actions has no Apple Silicon runner.
- **EC2 Mac** targets (`mac2.metal`, `mac1.metal`) are documentation-verified
  only, for the same reason plus the 24-hour minimum Dedicated Host
  allocation. See [cloud.md](cloud.md)'s target-OS support matrix.

### Open gaps

Both remaining gaps are the same shape, and both are product gaps rather than
documentation ones: the images **are** published on every release, but no
wrapped command consumes them from the registry yet.

1. **Docker / Compose** — `docker-compose.yml`'s `api` and `web` services
   carry a `build:` context (`.` and `./web`), so Compose builds them from a
   checkout instead of pulling `ghcr.io/dkblinux98/nyxgpt-api`/`-web`. The
   observability half is already repo-less: the Compose file and its templates
   ship as package data (`nyxgpt.resources`, #3621) and
   `nyxgpt ops observability` starts the monitoring/logging/tracing/errors
   profiles from public images with no checkout.
2. **Kubernetes** — `k8s/*.yaml` is not package data (`ops.K8S_DIR` resolves
   under `REPO_ROOT`, allowlisted in
   [`tests/unit/test_repo_root_allowlist.py`](../tests/unit/test_repo_root_allowlist.py)),
   so the kustomization an install applies only exists in a checkout; and
   `nyxgpt ops install --kubernetes --local` builds `nyxgpt-api:local` /
   `nyxgpt-web:local` from the checkout rather than loading the published
   images. The path is otherwise fully wrapped, and provisions its own `kind`
   cluster.

While either gap is open, `nyxgpt ops portability --strict` exits non-zero and
the dashboard says the capstone portability criterion is not met. That is
deliberate: closing them is what turns the strict gate green, and the gate then
keeps them closed.

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
| 4 | `nyxgpt cloud deploy --status` | `access_model` reports SSH-only ingress from your own CIDR — every app and observability URL is a `localhost` one through the tunnel |
| 5 | `nyxgpt cloud smoke --skip-deploy --keep` | chat round-trip, RAG ingest + query, and every observability UI green |
| 6 | `nyxgpt self-heal status` | `enabled: true`, every component healthy |
| 7 | `nyxgpt cloud destroy --yes` | tunnel closed, substrate destroyed, no billed resources left |

`nyxgpt ops portability` prints this same sequence, so the terminal you are
accepting from can always tell you the next step.

### Accepting code that isn't released yet

Step 1 installs from PyPI, so by default the run exercises the last **stable**
release — not the release-branch tip carrying the acceptance failures you just
fixed. Pin a [nightly dev build or a release candidate](cloud.md#pypi-publishing-dev-rc-and-stable)
instead — the nightly publishes the tip automatically, and
`nyxgpt release publish --publish` cuts an RC on demand (#3727):

| # | Command |
|---|---------|
| 1 | `pip install nyxgpt==3.0.0rc3` |
| 3 | `nyxgpt cloud deploy --version 3.0.0rc3` |

On **macOS**, where the repo-less install is `brew`, an `rc` is installed
from its own formulas instead — an rc publish stamps them into the same tap
alongside the stable ones:

```bash
brew tap dkblinux98/nyxgpt && brew install nyxgpt-api-rc nyxgpt-web-rc
```

`brew install nyxgpt-api` is unaffected and stays on the latest stable
release ([docs/homebrew.md](homebrew.md#release-candidate-formulas-rc-channel)).

Nothing else about the sequence changes: the build is a published artifact
like any other, so the run stays repo-less. Dev and rc builds are
acceptance-only and are never announced; `pip install nyxgpt` still resolves
to the stable release, because pip excludes pre-releases from an unpinned
requirement.

### Why step 5 passes `--skip-deploy`

A bare `nyxgpt cloud smoke` deploys a stack of its own, tests it, and destroys
it. That proves the smoke test works; it proves nothing about the deployment
you are accepting. `--skip-deploy` points it at the deployment step 3 made, and
`--keep` leaves that deployment up for steps 6 and 7 instead of tearing it down
early. (Without `--keep`, the smoke run destroys the deployment when it
finishes — which is the right default everywhere except inside an acceptance
run, since it guarantees no run can leave billed resources behind.)

### What to record

For each numbered step: the command, its exit status, and the operator-facing
output (URLs, the smoke summary, the `self-heal status` payload). Two extra
pieces of evidence make the acceptance auditable:

- Step 4's `access_model` — the proof that nothing is publicly reachable.
- After step 7, an AWS console or `nyxgpt cloud deploy --status` check showing
  no instance, no volume, and no security group left behind.

### Non-AWS rows

The three non-AWS targets are accepted the same way, on a clean machine each:

```bash
# macOS native
brew tap dkblinux98/homebrew-nyxgpt && brew install nyxgpt-api nyxgpt-web
nyxgpt up && nyxgpt ops status && nyxgpt down

# Linux native (systemd --user)
pip install nyxgpt
nyxgpt up && nyxgpt ops status && nyxgpt down
```

Compose and Kubernetes cannot be accepted from a clean machine until the two
gaps above close; `nyxgpt ops portability` is the check that says whether they
have.

## Related

- [cloud.md](cloud.md) — `nyxgpt cloud` in full: substrate, remote state,
  deploy, tunnel, smoke, and the target-OS support matrix
- [homebrew.md](homebrew.md) — the remote tap and how releases stamp it
- [systemd.md](systemd.md) — the Linux native path
- [ops.md](ops.md) — `nyxgpt ops`, including `portability`
- [self-healing.md](self-healing.md) — the watchdog a cloud deploy enables
- `product_management/PHASE_6_PLAN.md` — P6-16's specification
