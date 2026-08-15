# Cloud artifact smoke — `nyxgpt cloud smoke --container` (#3784)

**The question this answers: does installing nyxGPT from a published artifact
work on a bare Amazon Linux 2023 machine?**

It is not the same question `nyxgpt cloud smoke` (without `--container`)
answers. That one deploys to real AWS and verifies a *running deployment's
behaviour* — chat, RAG, observability — at the cost of an EC2 round-trip and a
bill. This one verifies the *install path* on the distro the cloud rounds
actually run on, in a local container, for free.

```bash
nyxgpt cloud smoke --container                       # install the latest published release
nyxgpt cloud smoke --container --version 3.0.0rc9    # install exactly that release
nyxgpt cloud smoke --container --status              # what the last run found
```

## Why it exists

The rc9 cloud round found five serial defects in the EC2 install path —
artifact-relative paths (#3759), docker (#3760), npm (#3761), unreported
subprocess output (#3762) and a venv on the AMI's Python 3.9 (#3782). Each one
masked the ones behind it, because the bootstrap aborts at its first failure,
so they were discovered one EC2 round-trip at a time.

`linux-native-smoke.yml` could not see any of them, and could never have: it
runs on ubuntu-latest with Python 3.11 and Node 20 installed by setup actions,
and installs nyxGPT *editable from the checkout*. That is a machine groomed to
never hit those failures — the "green by luck" condition D-006 exists to
prevent (the same shape the macOS smoke had before #3753).

## How it works

| Phase | What it does |
| --- | --- |
| `build` | Builds the AMI-parity image from `scripts/cloud/al2023-ami-parity.Dockerfile` on top of `amazonlinux:2023` |
| `boot` | Runs it privileged with a writable cgroup mount and waits for systemd — the install under test *is* a systemd install |
| `preflight` | Asserts the machine is genuinely bare (see below). Fails the run if it is not |
| `artifact` | Stages the artifact: a published PyPI release, or a locally built wheel with `--wheel` |
| `bootstrap` | Runs the **real rendered EC2 user-data script** (`nyxgpt cloud user-data --os linux`) as root, exactly as cloud-init does |
| `repo-less` | Asserts nyxGPT is answering from `site-packages`, and that no checkout exists in the container |
| `services` | Requires api (`:8000/health`), web (`:3000/`) and ollama (`:11434/`) to answer |
| `teardown` | Removes the container — always, unless `--keep` |

The bootstrap is not a copy of the install steps: it is the same text a real
instance runs, rendered from the packaged template. A drift between what CI
smokes and what an instance executes is therefore impossible.

### The preflight is the point

Before anything installs, the smoke asserts the container is a bare machine:

- `python3` is present and **older than 3.11** (AL2023 ships 3.9 — selecting a
  newer interpreter is the bootstrap's job, #3782)
- `node`, `npm` are absent (#3761)
- `docker` is absent (#3760)
- `git` is absent — so a repo checkout is not merely unused at runtime but
  impossible (CLAUDE.md's repo-less portability requirement)
- `nyxgpt` is absent

If the base image ever gains one of these, the run fails with "this run proves
nothing" instead of passing on a machine that cannot reproduce the defect.

### Fault injection: proving the smoke can fail

A job that only runs the happy path passes on every machine that fails to
reproduce the bug. `--inject` reintroduces a defect class and **inverts the
verdict** — the run passes only if the smoke fails:

| Fault | Reintroduces |
| --- | --- |
| `old-python` | #3782 — rewrites every versioned interpreter reference back to the system `python3`, so the CLI venv is built on a Python the wheel's `requires-python` refuses |
| `no-node` | #3761 — drops the Node 20 provisioning, so `ops install` reaches the web build with no `npm` |

```bash
nyxgpt cloud smoke --container --inject old-python   # exits 0 only if the smoke failed
```

Both transforms are written against the *defect*, not against a particular
line, so they keep reproducing it whatever shape the fix takes.

## What a green run does NOT cover

Stated here, printed by the command, and rendered on the dashboard panel,
because reading green as "the cloud path works" is exactly the mistake the five
serial defects hid behind:

- **EC2 provisioning** — Terraform, the AMI itself, instance/EBS lifecycle and
  security groups. `nyxgpt cloud infra plan/apply` and `terraform-local-smoke.yml`
  cover that layer.
- **cloud-init** — the bootstrap is executed directly as root, not by cloud-init
  from instance user-data, so user-data size limits, cloud-init ordering and its
  logging are not covered.
- **The private access path** — SSH, `nyxgpt cloud tunnel`'s port forwards and
  the owner-IP security-group rule. Services are probed on the container's own
  loopback.
- **Instance metadata (IMDS), instance profiles/IAM and cloud-sourced secrets.**
- **Real AWS timing and hardware** — instance boot, EBS throughput and network
  egress.
- **Deployment behaviour** — chat, RAG and the observability UIs are
  `nyxgpt cloud smoke` without `--container`, against real AWS.

### Container-mode substitutes

Two things in the AMI-parity image exist to neutralize *container* artifacts,
not target behaviour, and are commented as such in the Dockerfile:

- `systemd-networkd-wait-online` is masked. The container's interface is
  managed by Docker, not networkd, so the unit would wait out its full timeout
  and stall everything ordered after `network-online.target` (including the
  Docker engine the bootstrap starts). On an instance networkd owns the
  interface and the unit returns immediately.
- `/etc/shadow` is world-readable. On an AppArmor host (Ubuntu, and therefore
  GitHub's runners) the `unix-chkpwd` profile attaches inside the container too
  and denies it `dac_read_search`, so PAM cannot read root's shadow entry and
  every `sudo -u ec2-user` in the bootstrap fails. EC2 has no such profile.
  Every account in the throwaway image is password-locked.

The Docker engine runs *inside* the smoke container (privileged), so the
Cassandra container `ops install` creates is a child of the machine under test,
exactly as on an instance — this is the docker-in-docker leg, not a substitute
for it.

## Options

| Flag | Effect |
| --- | --- |
| `--container` | Run this smoke instead of the live AWS one |
| `--version <release>` | Install that published release (default: whatever `pip install nyxgpt` resolves to) |
| `--wheel <path>` | Install a locally built wheel instead — how CI tests a branch whose version is not on PyPI yet |
| `--image <ref>` | Base image for the AMI-parity build (default: `amazonlinux:2023`) |
| `--inject <fault>` | Reintroduce a defect class and require the smoke to fail (repeatable) |
| `--keep` | Leave the container running for inspection instead of removing it |
| `--status` | Print the last recorded run instead of starting one |
| `--json` | Print the full machine-readable record |
| `--bootstrap-timeout` / `--build-timeout` / `--health-timeout` | Budgets in seconds (defaults 2400 / 900 / 300) |

Each run is recorded to `~/.nyxGPT/cloud/artifact-smoke.json`, which is what
`--status`, the API and the dashboard panel all read — one run, one record.

## From the dashboard

**Admin → Cloud Artifact Smoke** (`/admin/cloud-smoke`) starts a run, shows the
last verdict with its defect classification and diagnostics, and lists the
coverage gaps above. It gets a real button where the AWS cloud pages get CLI
pointers (#3514) because a run here spends nothing and creates nothing outside
a disposable local container. A run takes tens of minutes, so the POST starts a
background run and the page polls the recorded result.

Endpoints: `GET`/`POST /api/v1/ops/cloud-artifact-smoke` (see [api.md](api.md)).

## In CI

`.github/workflows/cloud-artifact-smoke.yml`, path-scoped to the ops/install
layer, the user-data templates and the smoke itself:

- **`fault-injection`** — `--inject old-python` must fail the smoke. Cheap, and
  it is what makes the other job's green mean anything.
- **`artifact-install`** — the full run against a wheel built from the ref (or,
  on `workflow_dispatch` with a `version` input, against exactly what PyPI
  serves, which is the release-candidate path).

Both are blocking. See [live-verification-ci.md](live-verification-ci.md) for
what CI genuinely cannot execute.
