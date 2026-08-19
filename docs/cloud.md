# nyxGPT Cloud (AWS)

`nyxgpt cloud` is the CLI surface for AWS-deployed nyxGPT stacks. It covers
`deploy`/`destroy`/`tunnel` -- the one-command story from nothing to a
running, monitored stack (#3513) -- `infra`, the substrate underneath it
(#3509), `state`, that substrate's Terraform state (#3510), `user-data`, the
target-OS provisioning bootstrap those instances boot with (#3511), and
`allow-ip`, SSH lockout recovery (#3630).

**In a hurry?** [`nyxgpt cloud deploy`](#nyxgpt-cloud-deploy--the-one-command-path-p6-11-3513)
is the only command most operators need; everything below it is the
lower-level machinery it drives.

Install the AWS SDK dependency with:

```bash
pip install "nyxgpt[cloud]"
```

`boto3` is kept out of the base install -- it's only needed for AWS
deployments, not the local stack every other `nyxgpt` command drives.

A cloud instance provisions from published artifacts and never clones this
repository (the one exception, [`--dev`](#dev-mode-on-a-cloud-target), copies
your tree over SSH and still clones nothing), so the documentation on it is
the copy inside the installed
package: reach it in the tunneled web UI under **Support → Docs**, which
renders the product documentation that shipped with the deployed version. **File an Issue**
sits beside it in the same menu. See [ui.md](ui.md#support-menu).

---

## Background: the owner-IP-scoped SSH rule

Per
[`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`](../product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md),
an AWS-deployed nyxGPT instance is reached only over an SSH tunnel
(`nyxgpt cloud tunnel`): the API, web UI, and every observability endpoint
bind to `127.0.0.1` on the instance and are never opened in the security
group. The security group allows exactly one inbound rule -- TCP port 22,
scoped to the owner's current public IP, never `0.0.0.0/0`.

The tradeoff: when the owner's IP changes (ISP renewal, travel, mobile
tethering), that rule goes stale and the instance becomes unreachable,
**including over SSH** -- there is no other way in. `nyxgpt cloud allow-ip`
exists to fix exactly this, and does so by talking only to the AWS EC2 API,
never the instance, so it works from the new IP while still locked out.

---

## `nyxgpt cloud deploy` — the one-command path (P6-11, #3513)

```bash
nyxgpt cloud deploy --ssh-public-key ~/.ssh/id_ed25519.pub
```

One command takes you from nothing to a running, monitored stack you can
reach from your workstation. It:

1. **Applies the substrate** — the same reconcile `nyxgpt cloud infra apply`
   performs, so a re-run converges instead of creating a second deployment.
2. **Wires the access path** — the apply re-detects your current public IP
   every run, so the security group's single port-22 rule already points at
   wherever you are; the deploy reports that CIDR and then waits for the
   freshly booted instance to accept SSH.
3. **Provisions the instance from published artifacts** — installs the OS
   packages, a Python that satisfies nyxGPT's `requires-python` (the AMI's
   own `python3` is not assumed to: Amazon Linux 2023's is 3.9, below the
   floor — see [Python on the instance](#python-on-the-instance)), Node 20
   (from NodeSource, the toolchain `ops install` builds and
   runs the web bundle with), the Docker engine, Ollama, and a **published**
   `nyxgpt` release (or, under
   [`--dev`](#dev-mode-on-a-cloud-target), your working tree), then runs
   `nyxgpt ops install` on the box **exactly
   once**: one install pass per deploy, no retry pass and no follow-up
   `ops observability` run (the install already reconciles those profiles
   unless `--skip-observability` is passed). See
   [Repo-less by construction](#repo-less-by-construction) below and
   [Docker on the instance](#docker-on-the-instance).
4. **Selects the session storage backend** — `cassandra` by default (#3865),
   applied with `nyxgpt ops session-backend` on the instance before
   `ops install`, so the deployment's chats land in the `nyxgpt.chat_sessions`
   table every other mode pointed at the same Cassandra reads, rather than as
   JSON on the instance's own disk. Pass `--session-backend file` for a
   deliberately single-instance deployment. See
   [session-storage.md](session-storage.md).
5. **Enables self-healing** — a cloud instance is unattended by definition,
   so the deploy turns the watchdog on explicitly once the stack is up (it
   ships disabled). See [self-healing.md](self-healing.md#turning-it-on).
6. **Opens the tunnel and waits for health** — starts the SSH tunnel in the
   background, polls `http://localhost:8000/health` through it, and prints
   the localhost URLs, which are live the moment the command returns.

Every flag `nyxgpt cloud infra apply` accepts (`--region`, `--profile`,
`--owner-ip`, `--ssh-key-name`, `--instance-type`, `--root-volume-size`)
works here too and is remembered for later runs, plus:

| Flag | Meaning |
| --- | --- |
| `--version` | Published release to install on the instance (default: this CLI's own version, then whatever the last deploy used). Ignored under `--dev` |
| `--dev` | Deploy **your working tree** instead of a published release — see [Dev mode on a cloud target](#dev-mode-on-a-cloud-target) |
| `--skip-observability` | Deploy the core app only, without monitoring/logging/tracing/errors |
| `--session-backend` | Where the instance stores chat sessions: `cassandra` (default — shared with every mode pointed at the same Cassandra) or `file` (JSON on the instance's own disk). Remembered for later runs, so a re-deploy never silently moves an instance's sessions back to files. See [session-storage.md](session-storage.md) |
| `--no-tunnel` | Don't open the tunnel (and so don't health-check through it); prints the `nyxgpt cloud tunnel` command to run instead |
| `--ssh-user` | Login user on the instance (default `ec2-user`, the Amazon Linux 2023 default) |
| `--identity-file` | Private key to authenticate with (default: whatever the last deploy used, then whatever `ssh` would pick from `~/.ssh` and your agent) |
| `--host` | Target an existing box instead of the provisioned instance |
| `--health-timeout` / `--ssh-timeout` | Seconds to wait for `/health` (default 900) and for SSH (default 300) |
| `--status` | Superseded by [`nyxgpt cloud status`](#nyxgpt-cloud-status--where-is-my-instance-3813); still prints the same JSON for anything already scripted against it |

### `nyxgpt cloud status` — where is my instance? (#3813)

The deploy ends by pointing at this command, because everything it printed
scrolls away and the public IP is not something to recover from a terminal's
scrollback:

```bash
nyxgpt cloud status            # the operator summary (default)
nyxgpt cloud status --json     # the machine-readable payload
nyxgpt cloud status --no-probe # don't health-check through an open tunnel
```

It answers from recorded state alone — no AWS call, no connection to the
instance — so it is safe to run at any time and still answers when your AWS
credentials have expired. The summary carries the installed release, the
instance id and type, the region, the public IP, the security group's single
ingress rule, the enabled observability profiles, the tunnel's state, a
health verdict, the localhost URLs and, most importantly, the **connection
target**: the SSH user and identity file the deploy actually used, alongside
the host. `host` on its own is not an address you can reach.

For support conversations it also prints, under a `Diagnostics` heading, the
raw `ssh` invocation that `nyxgpt cloud tunnel` executes on your behalf.
That is shown so you can see what is running when a tunnel misbehaves — the
command to *run* is always the wrapped one.

Where the answer comes from follows the same three-way rule as the substrate
(see [Which machine is answering](#which-machine-is-answering-3804)): the
deploy record on the workstation that deployed, the instance itself when the
command runs there, and otherwise **unknown** — which is not the same as
nothing being deployed. The connection target is reportable only in the first
case; on the instance the SSH user and key are the workstation's, and the
command says so rather than printing a blank.

### `nyxgpt cloud ops` — inspecting the instance (#3813)

Container state on the instance, without a hand-rolled `ssh` and a raw
`docker compose ps`:

```bash
nyxgpt cloud ops status     # the instance's own `nyxgpt ops status` (default)
nyxgpt cloud ops doctor     # the instance's own `nyxgpt ops doctor`
nyxgpt cloud ops self-heal  # the instance's own `nyxgpt self-heal status`
nyxgpt cloud ops session-backend  # which session store the instance is on (#3865)
```

Each one runs the named wrapped command *on the instance* over the same SSH
access path [`nyxgpt cloud credentials`](#nyxgpt-cloud-credentials) uses, and
streams the instance's own output back unchanged. The list is deliberately
read-only: changing what runs on the instance is `nyxgpt cloud deploy`, which
is idempotent and records what it did.

The inspection's own exit code is passed through — `nyxgpt cloud ops doctor`
exiting non-zero because the stack is unhealthy is a reportable answer, not a
failure of the command. A failure to *reach* the instance is reported as one,
with `nyxgpt cloud allow-ip` named as the usual fix.

The SSH user and identity file the deploy recorded are reused automatically,
so a deployment made with a non-default key does not need `--identity-file`
re-typed on every inspection; `--ssh-user`, `--identity-file` and `--host`
still override.

### Reaching it: `nyxgpt cloud tunnel`

```bash
nyxgpt cloud tunnel              # hold the tunnel open in the foreground
nyxgpt cloud tunnel --background # leave it running and return
nyxgpt cloud tunnel --status     # is one open, and what does it forward?
nyxgpt cloud tunnel --stop       # close a backgrounded tunnel
```

The tunnel forwards the core services plus a UI for each observability
profile the deploy enabled:

| Service | URL while the tunnel is open |
| --- | --- |
| API | `http://localhost:8000` |
| Web UI | `http://localhost:3000` |
| Grafana (`monitoring`) | `http://localhost:3001` |
| Prometheus (`monitoring`) | `http://localhost:9090` |
| Jaeger (`tracing`) | `http://localhost:16686` |
| GlitchTip (`errors`) | `http://localhost:8080` |

There is **no instance-facing URL** — by design. Nothing on the box listens
on a non-loopback address, and no application port is open in the security
group, so the tunnel is the only path in. If a local port is already taken
(a local stack on 8000/3000, say), the tunnel refuses to open and says so;
`nyxgpt ops down` frees them.

### `nyxgpt cloud credentials`

Logging into Grafana and GlitchTip: the observability UIs above ask for an
admin login, and both passwords are
ops-managed secrets generated on the instance — Grafana's by `nyxgpt ops
install`, GlitchTip's by `nyxgpt ops glitchtip-init`. Read them from your
workstation with:

```bash
nyxgpt cloud credentials                      # both services
nyxgpt cloud credentials --service grafana    # just one
nyxgpt cloud credentials --json               # machine-readable
```

This runs the instance's own
[`nyxgpt ops credentials`](ops.md#nyxgpt-ops-credentials) over the same
wrapped SSH access path every other deploy step uses — there is never a
reason to `ssh` to the box and `cat` a secret file yourself (#3718). The
URLs it prints are the instance's loopback URLs, reachable once `nyxgpt
cloud tunnel` is open.

A service whose password hasn't been provisioned yet prints as `(not
provisioned)` with the command that provisions it, and the command exits 2.
Credentials are never returned by the HTTP API (#3458/#3466); this path is
CLI-side only.

### Tearing it down

```bash
nyxgpt cloud destroy --yes
```

Closes the tunnel, then destroys the substrate. `--yes` is required: the
instance and its root volume go, and anything living only on that box —
models, Cassandra data, logs — goes with them.

### Repo-less by construction

Per CLAUDE.md's repo-less portability requirement (2026-08-01), neither side
of this flow touches a checkout:

- **Operator side** — everything the CLI needs (the Terraform configuration,
  the provisioning script) ships inside the installed package, so the whole
  deploy runs from an artifact-installed `nyxgpt` on a workstation that has
  never cloned this repository.
- **Instance side** — the box installs `nyxgpt==<version>` from PyPI into a
  venv under `~/.nyxGPT`, seeds `config.ini` from the installed package, and
  runs `nyxgpt ops install`. It never runs `git clone`, and nothing is copied
  from the operator's machine. This is the same sequence the
  `artifact-install-smoke` job in `.github/workflows/release-artifacts.yml`
  proves on a checkout-free runner for every release;
  `tests/unit/test_cloud_deploy.py` asserts the generated script contains no
  source-control fetch at all.

[`--dev`](#dev-mode-on-a-cloud-target) is the one deliberate exception, and it
is opt-in and checkout-only for exactly this reason: a plain
`nyxgpt cloud deploy` still needs no repository on either side, and the
instance still clones nothing under `--dev` either — your tree crosses the
deploy's own SSH connection.

### Dev mode on a cloud target

`nyxgpt up --dev` runs the stack from the checkout in front of you instead of
building artifacts ([ops.md](ops.md#--dev-run-the-current-checkout-without-an-artifact-build)).
`nyxgpt cloud deploy --dev` is the same idea aimed at the EC2 instance:

```bash
nyxgpt cloud deploy --dev        # deploy the tree you are standing in
nyxgpt cloud deploy              # back to a published release
```

What it does, in the order the deploy reports it:

1. **Refuses immediately if you are not in a checkout.** The check is
   `nyxgpt.ops.dev_checkout_root()` — the same one `nyxgpt up --dev` uses, so
   the two can never disagree about what a source tree is — and it runs before
   AWS is touched, so a mistaken `--dev` costs nothing.
2. **Ships your working tree**, after the substrate is applied and the
   instance is answering SSH. The file list comes from git
   (`--cached --others --exclude-standard`): everything tracked plus everything
   new that is not ignored, so **uncommitted edits go too** — that is the point
   of dev mode. `node_modules`, `.venv` and `.next` are excluded by the
   repository's own ignore rules, and `.git` is never sent: the instance gets
   a source tree, not a repository it could pull or push from. It lands in
   `~/.nyxGPT/src` and **replaces** whatever the last `--dev` deploy left
   there, so a file you deleted locally leaves the instance too.
3. **Installs it editable** (`pip install -e ~/.nyxGPT/src`) instead of
   `pip install nyxgpt==<version>`, and runs `nyxgpt ops install --dev` on the
   box. Everything else about the deploy is identical — same bootstrap, same
   Node 20, same Docker, same session backend, same self-heal enable.

Things worth knowing before you use it:

- **It is not an acceptance path.** Same rule as the local dev mode: it exists
  for development and mid-stream testing. What you accept is a published
  release, installed the artifact way.
- **`--dev` is never inherited.** Every other choice a deploy records (the SSH
  user, the identity file, the session backend, the version) carries over to
  the next run; this one does not. A plain `nyxgpt cloud deploy` always means
  "install a published release", so re-running it after a `--dev` deploy puts
  the release back rather than silently re-shipping whatever is checked out.
- **The version you see is your tree's.** A working tree usually declares a
  release that does not exist yet, so the deploy summary says plainly that it
  built from your tree and names the directory. `nyxgpt cloud status` reports
  it as well, so an instance running a tree is never mistaken for one running
  a release.
- **The web UI runs Next's dev server**, as it does under `nyxgpt up --dev` —
  slower first paint, no production build.
- **It ships the tree, not the machine.** Anything your local stack has that
  the repository does not (an untracked config, a hand-installed dependency)
  is not on the instance.

Terraform and Kubernetes modes are a *local* install-mode choice
(`nyxgpt ops install --terraform/--kubernetes --local`), and `--dev` composes
with both there — see [terraform.md](terraform.md#install-modes-artifact-default-and---dev)
and [kubernetes.md](kubernetes.md#install-modes-artifact-and---dev). Neither is
a `nyxgpt cloud deploy` mode: the cloud path deploys the native stack to one
EC2 instance.

Verified by execution, not inspection: `.github/workflows/cloud-dev-deploy-smoke.yml`
ships this repository's working tree to a bare Amazon Linux 2023 container over
real SSH and requires the box to import `nyxgpt` from the shipped tree, an
uncommitted sentinel included.

The instance therefore runs a *published* release, not your working tree. If
you want a version other than your CLI's, name it with `--version`.

### Python on the instance

nyxGPT's `requires-python` is `>=3.11`, and the AMI's own `python3` need not
satisfy it — on Amazon Linux 2023 it is 3.9. Nothing on the instance assumes
otherwise:

- **Provisioning** installs an explicit `python3.13`/`python3.12`/`python3.11`
  package (newest that its package manager has), then *resolves* the
  interpreter by asking each candidate its own version rather than trusting
  its name. If nothing on the box qualifies, the deploy stops there and says
  so, naming the version it found.
- **`nyxgpt ops install`** picks the interpreter for each service venv the
  same way: the interpreter ops itself is running under first (it is
  provably able to run nyxGPT), then an explicitly-versioned `python3.X` from
  PATH, and bare `python3` last and only if it qualifies. A candidate whose
  `venv`/`ensurepip` is missing — Debian splits those into `python3.X-venv` —
  falls through to the next one.

Before that, the venv was built with bare `python3`, so on Amazon Linux 2023
`ops install` produced a Python 3.9 venv and pip refused the artifact into it
(`requires a different Python: 3.9.16 not in '>=3.11'`) minutes into a
deploy. What that failure looks like now, and what to do about it, is in
[troubleshooting.md](troubleshooting.md#no-python--311-available-to-create-the-nyxgpt-api-venv).

### Docker on the instance

The provisioning script installs only the Docker *engine* from the AMI's
package manager and leaves the Compose plugin to `nyxgpt ops install`, which
knows how to get one on distros that package none — Amazon Linux 2023 among
them. See [Privileged install steps](systemd.md#privileged-install-steps) for
what install does there and what it falls back to.

Group membership is the other half. `usermod -aG docker` cannot reach the SSH
session the deploy is already running in, so every nyxGPT command the script
invokes runs under `sg docker`, which grants the group immediately and without
a re-login; the script resolves once, up front, whether `sg` works, rather than
retrying a failed command without the group and turning an unrelated failure
into a `permission denied ... /var/run/docker.sock` cascade. `ops install` has
its own belt-and-braces hop for the same problem — `sg docker` there too,
falling back to `sudo -n --preserve-env` — and it verifies the hop preserves
`HOME` before using it, so Compose bind mounts keep resolving under
`/home/ec2-user` rather than `/root`
([Docker group membership](systemd.md#privileged-install-steps)).

### From the dashboard: information only (#3804)

The admin dashboard's **[Infrastructure Status](ui.md)** page
(`/admin/infrastructure`) carries an **AWS** section that **reports the cloud
substrate and the deployment on it, and does nothing else.** It shows the
substrate (region, instance, type, public IP, VPC, subnet, security group,
key pair, open ports), the installed release, the enabled observability
profiles, the connection target (the SSH user@host and identity file, plus
the raw ssh the wrapped tunnel executes, as diagnostics), whether the access
tunnel is open, a health answer, the localhost URL list, the Terraform state
backend, and the deploy history — and it points
at the wrapped commands below for everything that changes state:

| To do this | Run |
| --- | --- |
| Deploy or redeploy the stack | `nyxgpt cloud deploy` |
| Tear the whole deployment down | `nyxgpt cloud destroy --yes` |
| Run the end-to-end cloud test (deploys, verifies, tears down) | `nyxgpt cloud smoke` |
| Test the artifact install path locally, without AWS | `nyxgpt cloud smoke --container` |
| Show the same state from a terminal | `nyxgpt cloud status` |
| Inspect the containers running on the instance | `nyxgpt cloud ops status` |
| Diagnose the instance | `nyxgpt cloud ops doctor` |
| Read the observability logins | `nyxgpt cloud credentials` |
| Open or close the access tunnel | `nyxgpt cloud tunnel` / `nyxgpt cloud tunnel --stop` |
| Preview a substrate change without creating anything | `nyxgpt cloud infra plan` |
| Move Terraform state, or recover a version | `nyxgpt cloud state migrate` / `versions` / `restore` / `unlock` |
| Re-allow SSH after your public IP changes | `nyxgpt cloud allow-ip` |

There was a separate **AWS Cloud Infrastructure** screen
(`/admin/cloud-infrastructure`). The owner removed it, and every remaining
control on it, on 2026-08-16 (#3804). #3514 had already removed Apply, Deploy
and Destroy on the grounds that cloud lifecycle actions are rare,
consequential and irreversible; what the acceptance round found is that the
argument does not stop at those three:

- **The self-hosting paradox.** Every acting control changes the substrate the
  UI itself runs on. Migrating Terraform state or applying a substrate change
  from a page served by that instance pulls the rug out from under it, and if
  the operation half-completes the surface that would report it is gone.
- **No usable escape hatch.** Driving it safely needs a *second* nyxGPT
  controlling the first, which is not practical: two local instances collide
  on `:8000`/`:3000`, and a Kubernetes-hosted one collides with the native
  install on the same host ports. The control surface is unusable where it
  would be safe and unsafe where it is usable.

Reading has neither problem — observing does not remove the observer — which
is why the information folded into the local Infrastructure page and the
controls did not come with it.

### Which machine is answering (#3804)

The AWS section's facts come from whichever source can actually see the
substrate from where the dashboard is running, and it names the source it
used:

| Where the dashboard runs | Source | What it reports |
| --- | --- | --- |
| On the EC2 instance | Instance metadata (IMDSv2, `169.254.169.254`) | The running machine's own region, instance id and type, public IP, VPC, subnet, security groups and key pair. The deployment is read first-hand — the stack answering the request *is* the deployment |
| On the workstation that provisioned it | The Terraform outputs in `~/.nyxGPT/cloud/state.json` | The substrate that machine created, plus its deploy record, tunnel state and history |
| Neither | — | **Unknown.** Not "not provisioned": nothing on that machine has checked |

This is why the panel exists in this shape. Deriving everything from Terraform
state — which lives on the operator's workstation — made a dashboard served
*from* the instance report "not provisioned" with every field blank, on a
machine Terraform had created minutes earlier (owner observation, rc12).
Terraform state and the tunnel are likewise reported as *not on this machine*
when the page is served from the instance, rather than as a local file that
does not exist there.

The page and the CLI still call the same `nyxgpt.cloud_deploy` and
`nyxgpt.cloud_infra` functions, and the commands it displays come from the
backend's own `LIFECYCLE_COMMANDS`, so the two can never drift apart.

### Where deploy state lives

| Path | Contents |
| --- | --- |
| `~/.nyxGPT/cloud/deploy.json` | What the last successful deploy installed: version, host, instance, region, enabled profiles |
| `~/.nyxGPT/cloud/tunnel.json` | The backgrounded tunnel's pid and forwarded profiles, so `--stop`/`--status` (and the dashboard) find a tunnel another process started |
| `~/.nyxGPT/cloud/history.jsonl` | One line per deploy, teardown and [smoke run](#nyxgpt-cloud-smoke--the-end-to-end-cloud-test-p6-17-3515) — timestamp, action, outcome, version, instance, and what went wrong on a failure |

All three are read-only inputs to `nyxgpt cloud status`, which
answers without calling AWS or touching the instance — safe to poll, and it
still answers when your AWS credentials have expired.

The history is appended by `deploy` and `destroy` themselves rather than by
whichever surface invoked them, so a deploy run from a terminal shows up on
the dashboard exactly like any other. A deploy that installed the stack but
never went healthy is recorded as `failed` before the error is raised —
that is precisely the event the history exists to preserve. A teardown whose
substrate destroy fails is recorded the same way, and leaves `deploy.json` in
place: nothing has proved the deployment is gone.

### Troubleshooting

| Symptom | What it means |
| --- | --- |
| `did not accept SSH within 300s` | Usually a stale security-group rule — run `nyxgpt cloud allow-ip` (see [Lockout recovery](#lockout-recovery)). Also possible on a very slow first boot: retry with `--ssh-timeout 600`. |
| `Provisioning the instance failed` | Every failed step of the remote install is listed under it, in full and untruncated (the provisioning run's stdout and stderr are streamed together as they happen, so what you watched is what the summary quotes). When the run failed before any step reported — a package-manager or shell error — the last lines of its output are quoted instead, always on whole-line boundaries. Re-running `nyxgpt cloud deploy` is safe — provisioning is idempotent. |
| `node/npm could not be installed` | Neither NodeSource nor the distro's own packages produced a Node toolchain, so `ops install` could not build the web bundle. Usually a blocked egress to `nodesource.com`; the instance needs outbound HTTPS. |
| `never returned 200 within 900s` | The stack installed but isn't healthy. The tunnel is left open; `nyxgpt cloud status` and `nyxgpt cloud ops doctor` (the instance's own doctor, over the wrapped SSH path) say more. |
| `Could not open the SSH tunnel` | A local port is already bound, most often by a local nyxGPT stack. |

---

## `nyxgpt cloud smoke` — the end-to-end cloud test (P6-17, #3515)

The cloud counterpart of `scripts/smoke-test.sh`. One command provisions a
deployment, proves it actually works over the private access path, and then
destroys it again:

```bash
nyxgpt cloud smoke
```

| Phase | What it proves |
| --- | --- |
| `deploy` | `nyxgpt cloud deploy` succeeds: substrate applied, published release installed, tunnel opened |
| `access` | The API answers `/health` on `http://localhost:8000` — i.e. the SSH tunnel *is* a working access path |
| `model` | The instance's configured default model is present, pulling it if it is not |
| `chat` | A real chat round-trip through `/api/v1/chat` returns a non-empty reply |
| `rag` | A document containing a unique marker is ingested, then a query for it returns that marker |
| `observability` | Every UI the deploy's enabled profiles forward — Grafana, Prometheus, Jaeger, GlitchTip — answers through the tunnel |
| `teardown` | `nyxgpt cloud destroy` ran and the substrate is gone |

Exit code is `0` only when every phase passed **and** the teardown succeeded.

### It always tears down

This is the point of the command, so it is worth being explicit: the teardown
runs on **every** exit path — a failed verification, an unexpected error, a
deploy that died half-applied, or a Ctrl-C. A run that leaves AWS resources
behind is reported as a failure even if every check passed, and the message
tells you to run `nyxgpt cloud destroy --yes`. The only thing that skips the
teardown is `--keep`, which prints a warning that the deployment is still
billing.

Because the test both creates and destroys, `--skip-deploy` (verify the
deployment that already exists) additionally requires `--yes` — otherwise the
run would destroy a deployment it did not create.

### Options

| Flag | Effect |
| --- | --- |
| `--version <release>` | Deploy and test a specific published release (default: this CLI's version) |
| `--skip-observability` | Core app only — skips the observability stack and its reachability check |
| `--skip-deploy` | Verify the existing deployment instead of deploying one (requires `--yes`, or `--keep`) |
| `--keep` | Leave the deployment running afterwards. **It keeps billing** until `nyxgpt cloud destroy --yes` |
| `--api-key <key>` | API key for the deployed stack (default: `$NYXGPT_AUTH_API_KEY`, then the key the instance itself is configured with) |
| `--json` | Print the full machine-readable record of the run instead of a summary |
| `--model-timeout` / `--chat-timeout` / `--rag-timeout` / `--observability-timeout` / `--health-timeout` / `--ssh-timeout` | Per-phase budgets in seconds (defaults 1800 / 300 / 120 / 300 / 900 / 300) |

Every run is appended to `~/.nyxGPT/cloud/history.jsonl` as a `smoke` entry, so
it appears in the dashboard's deploy history alongside deploys and teardowns.
The Infrastructure page's AWS section lists the command as a pointer (it is a
lifecycle action, so it is not a dashboard button — see
[From the dashboard](#from-the-dashboard-information-only-3804)).

It is a wrapped CLI command rather than a script in this repository on purpose:
per CLAUDE.md's repo-less portability requirement it has to run on a machine
that has never cloned the repo, which is exactly the machine P6-16 accepts the
cloud path from.

Inside that acceptance run the invocation is
`nyxgpt cloud smoke --skip-deploy --keep`, so it verifies the deployment being
accepted rather than deploying a second, throwaway one — see
[portability-matrix.md](portability-matrix.md#clean-machine-acceptance-run) for
the whole sequence, and `nyxgpt ops portability` to print it from the machine
you are accepting from.

---

## `nyxgpt cloud smoke --container` — the artifact install path, without AWS (#3784)

The same command, pointed at a bare **Amazon Linux 2023 container** instead of
a real deployment. It answers the other half of the question: not "does the
deployed stack behave?" but "does installing from a published artifact work on
the distro the instances run?" — on a machine with the AMI's Python 3.9, no
node, no docker and no git, executing the same rendered user-data bootstrap a
real instance runs.

```bash
nyxgpt cloud smoke --container --version <rc>   # or omit --version for the latest published release
```

It costs nothing, needs no AWS credentials, and is what CI runs on changes to
the ops/install layer. It does **not** exercise Terraform, cloud-init, SSH or
the instance lifecycle — see
[cloud-artifact-smoke.md](cloud-artifact-smoke.md) for the full command,
its fault-injection mode, and the complete list of what a green run does not
cover.

---

## `nyxgpt cloud infra` — provisioning the AWS substrate (P6-8, #3509)

`nyxgpt cloud infra` provisions the infrastructure an AWS deployment runs on,
and nothing else — installing the nyxGPT stack onto the instance is a
separate step (#3513). It is the only supported way to drive
`terraform/aws/`: per CLAUDE.md no user flow runs raw `terraform`, so the
command owns Terraform's whole lifecycle (installing the binary, generating
tfvars, pinning state, recording outputs).

### What it creates

Shape fixed by two approved decision records, not by configuration:

| Resource | Detail |
| --- | --- |
| VPC | `10.42.0.0/16` by default, DNS support + hostnames on |
| Public subnet(s) | one `10.42.1.0/24` subnet by default, plus an internet gateway and default route — the instance needs a routable address for SSH |
| Security group | **exactly one inbound rule: TCP 22 from your IP.** Egress is open outbound (package/artifact/image/model downloads) |
| EC2 instance | one `m5.large` (per [`DECISION_AWS_COMPUTE_SUBSTRATE.md`](../product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md)), IMDSv2 required, encrypted gp3 root volume, Elastic IP so the address survives stop/start |

There is no EKS cluster, node group, load balancer, or NAT gateway: a single
owner reaching a single private deployment needs none of them, and an ALB
would contradict the access model outright.

Resources are named with the same `nyxgpt-tf-*` convention as the local
Docker stack (`nyxgpt-tf-vpc`, `nyxgpt-tf-instance-sg`, `nyxgpt-tf-instance`,
…), overridable via `name_prefix`.

### Commands

```bash
# See what would be created; creates nothing.
nyxgpt cloud infra plan --region us-east-1 --ssh-public-key ~/.ssh/id_ed25519.pub

# Provision it (idempotent -- a re-run reconciles rather than duplicates).
nyxgpt cloud infra apply

# What's provisioned, and how it's reachable.
nyxgpt cloud infra status

# The access-model checks CI runs, offline: no AWS account, creates nothing.
nyxgpt cloud infra test

# Tear it down (deletes the instance and its root volume).
nyxgpt cloud infra destroy --yes
```

Every flag is remembered in `~/.nyxGPT/cloud/infra.json`, so later runs only
need the ones that change. Exactly one of `--ssh-public-key` (a `.pub` file
to register as a new key pair) or `--ssh-key-name` (an EC2 key pair that
already exists in the region) is required — SSH is the only way in, so an
instance with no key is refused before anything is created.

None of these is on the dashboard. The Infrastructure page's AWS section
reports what they produced and names the commands themselves — see
[From the dashboard](#from-the-dashboard-information-only-3804).

### The SSH source CIDR

`--owner-ip` sets the one CIDR allowed to reach port 22. Omitted, it is
auto-detected as this machine's current public IP, scoped to `/32`.
`0.0.0.0/0` is refused three times over — by the CLI, by the root module's
variable validation, and by the security module's precondition — and
anything broader than a `/16` is refused as a fat-fingered CIDR.

Once the group exists, use [`nyxgpt cloud allow-ip`](#nyxgpt-cloud-allow-ip)
to move that rule; see "How `allow-ip` and the Terraform module coexist"
below for why a re-apply is not the way to change it.

### Where state and configuration live

| Path | What |
| --- | --- |
| `~/.nyxGPT/cloud/terraform/` | the Terraform configuration, materialized from the installed package (works with no repo checkout) |
| `~/.nyxGPT/cloud/terraform.tfstate` | Terraform state *before* you migrate it, deliberately outside the config directory an upgrade re-syncs. See [Remote state](#remote-state-s3--dynamodb-locking-p6-9-3510) |
| `~/.nyxGPT/cloud/terraform.tfvars` | generated from your flags, mode 0600 |
| `~/.nyxGPT/cloud/infra.json` | remembered settings, mode 0600 |
| `~/.nyxGPT/cloud/backend.json` | where remote state lives, once migrated, mode 0600 |
| `~/.nyxGPT/cloud/state.json` | the ids `allow-ip` (and later `cloud deploy`) read |

### Credentials and cost

Provisioning uses boto3-style credential resolution via Terraform's AWS
provider — the profile from `nyxgpt cloud credentials-setup` (below), or
`--profile`/`AWS_PROFILE`. **This command creates billable resources**: an
`m5.large` on-demand is roughly $70/month plus EBS and the Elastic IP;
`nyxgpt cloud infra destroy --yes` removes all of it.

---

## Remote state (S3 + DynamoDB locking, P6-9, #3510)

A fresh install keeps the substrate's Terraform state in one local file,
`~/.nyxGPT/cloud/terraform.tfstate`. That is correct for one operator on one
machine and wrong for everything else:

- A second operator, or a CI runner, has no way to see the first one's state.
  Terraform would believe nothing exists and try to create a second VPC,
  security group, and instance.
- Two concurrent applies can interleave writes and leave a state file that
  describes neither run's result — with no warning at the time.

`nyxgpt cloud state` moves that state into an S3 bucket with a DynamoDB lock
table: shared, versioned, encrypted, and mutually exclusive.

### Migrating

```bash
# Where state lives right now, and how (or whether) it is locked.
nyxgpt cloud state status

# Create the bucket + lock table and move existing state into them.
nyxgpt cloud state migrate
```

`migrate` is safe to re-run and needs no flags in the common case. It:

1. Creates the state bucket (default `nyxgpt-tfstate-<account-id>-<region>` —
   S3 bucket names are globally unique, hence the account id) with
   **versioning**, **default AES256 encryption**, and **all public access
   blocked**. An existing bucket is adopted, and those three settings are
   re-applied to it rather than assumed.
2. Creates the DynamoDB lock table (default `nyxgpt-tfstate-locks`,
   on-demand billing) and waits for it to go active.
3. Rewrites the backend and re-initializes with `-migrate-state
   -force-copy`, which copies the existing local state up to S3.

Override any of it with `--bucket`, `--table`, `--key`, `--region`,
`--profile`; the values are remembered in `~/.nyxGPT/cloud/backend.json`.
Use `nyxgpt cloud state bootstrap` to create the AWS resources *without*
switching the backend — useful when the person with permission to create
buckets isn't the person who runs the migration.

After migrating, `nyxgpt cloud infra plan/apply/destroy` work exactly as
before. The difference is that a second concurrent apply now blocks on the
lock instead of racing.

These are CLI operations only. The Infrastructure page's AWS section reports
where state lives and how it is locked, and nothing there rewrites it (#3804)
— see [From the dashboard](#from-the-dashboard-information-only-3804).

### Recovery

Four things go wrong with remote state. Each has a wrapped command.

**A run was killed mid-apply and left the lock held.** Every later run fails
with the lock's id. Release exactly that lock:

```bash
nyxgpt cloud state unlock --lock-id <id-from-the-error>
```

Only do this when no apply is actually running. Breaking a lock a live run
still owns is how two runs end up writing the same state — which is the
problem locking exists to prevent. The lock id is required for that reason:
there is no "release whatever is held".

**State was written wrong and has to be rolled back.** Bucket versioning is
enabled at creation precisely for this: every write keeps its predecessor,
and each version is a complete state file as it stood after one apply.

```bash
# Inventory, newest first.
nyxgpt cloud state versions

# Make one of them current.
nyxgpt cloud state restore --version-id <id>
nyxgpt cloud infra plan   # what Terraform now believes differs from AWS
```

`restore` downloads the version and pushes it back *through Terraform*
rather than copying it over the object in S3. The backend keeps a checksum
of the state in DynamoDB; an out-of-band overwrite leaves that checksum
describing the version it replaced, and every later command then fails an
integrity check. The restore is itself reversible — the version it replaces
stays in the bucket as its own version.

**You want a copy before doing something risky.**

```bash
nyxgpt cloud state backup                       # ~/.nyxGPT/cloud/terraform.tfstate.backup
nyxgpt cloud state backup --output ./before.tfstate
```

This reads through Terraform, so it works identically before and after
migration. The file is written mode 0600 — state carries every resource id
and the values of any variables passed in.

**The backend itself is the problem** — the account is locked out, a bucket
policy was changed, the region is down. Move state back to the local file
and keep operating:

```bash
nyxgpt cloud state local
```

The bucket and lock table are deliberately left in place; this changes where
Terraform reads state, not what exists in AWS. Re-run `nyxgpt cloud state
migrate` to go back.

If a migration fails part-way, `backend.json` is rolled back so it keeps
describing where the state actually is, and the next command re-initializes
against that backend. You should not have to repair anything by hand — but
`nyxgpt cloud state status --verify` will confirm the bucket, the lock
table, and that versioning is genuinely on.

### Permissions

Beyond what provisioning already needs, migrating requires
`sts:GetCallerIdentity` (to derive the default bucket name),
`s3:CreateBucket`/`PutBucketVersioning`/`PutEncryptionConfiguration`/
`PutBucketPublicAccessBlock` once at bootstrap, `s3:GetObject`/`PutObject`/
`ListBucket`/`ListBucketVersions` on the state object thereafter, and
`dynamodb:CreateTable`/`DescribeTable` plus `GetItem`/`PutItem`/`DeleteItem`
on the lock table.

---

## `nyxgpt cloud allow-ip`

Refreshes the security group's port-22 ingress rule to the caller's current
public IP.

```bash
nyxgpt cloud allow-ip
```

What it does:

1. Detects the caller's current public IP (via `https://checkip.amazonaws.com`,
   AWS's own IP-echo endpoint -- no third-party dependency).
2. Resolves the target security group: `--security-group-id` if given,
   otherwise `~/.nyxGPT/cloud/state.json`'s `security_group_id` (written by
   `nyxgpt cloud deploy`).
3. Revokes every existing port-22 ingress CIDR that doesn't match the new
   IP, and authorizes the new one -- unless it's already the only allowed
   source, in which case the command is a no-op (idempotent).
4. Prints the old and new source CIDR.

### Options

| Flag | Description |
| --- | --- |
| `--ip <addr>` | Use this IP or CIDR instead of auto-detecting the caller's current public IP. A bare address (no `/`) is scoped to `/32`; an explicit CIDR is kept as passed. `0.0.0.0/0` is always refused. |
| `--security-group-id <id>` | Security group to update. Defaults to `~/.nyxGPT/cloud/state.json`'s `security_group_id`. |
| `--region <region>` | AWS region. Defaults to `~/.nyxGPT/cloud/state.json`'s `region`, then boto3's normal region resolution (`AWS_REGION`/`AWS_DEFAULT_REGION`/profile config). |

### Example

```bash
$ nyxgpt cloud allow-ip
Security group sg-0123456789abcdef0: SSH ingress rule updated.
  old: 198.51.100.7/32
  new: 203.0.113.42/32

$ nyxgpt cloud allow-ip
Security group sg-0123456789abcdef0: SSH already allowed from 203.0.113.42/32 -- no change.
```

### Credentials

`allow-ip` uses boto3's normal credential resolution (environment variables,
`~/.aws/credentials`, an instance/SSO profile, ...) -- it does not collect or
store AWS credentials itself. See "Guided AWS credentials setup" below for
how to get a profile in place.

---

## Target-OS provisioning (P6-12/#3511)

`nyxgpt cloud user-data` renders the EC2 user-data bootstrap script that
installs nyxGPT on a fresh instance and brings up the native stack --
per-target-OS, from published artifacts only. It doesn't talk to AWS
itself: it prints a script, which is what an instance's `user_data`
consumes so the machine provisions itself on first boot.

**Relationship to `nyxgpt cloud deploy`.** `deploy` (P6-11, #3513) reaches
an already-running instance over SSH and provisions it there
(`render_provision_script` in `src/nyxgpt/cloud_deploy.py`); the substrate
module (P6-8, #3509) currently sets no `user_data` at all. The two
bootstraps therefore overlap -- both `pip install` a published release and
run `nyxgpt ops install`, never a clone -- but they answer different
questions: `user-data` is the first-boot, no-SSH-required path and the only
one that covers **EC2 Mac**, which `deploy`'s Linux-only SSH script does
not. Collapsing them onto one renderer is follow-up work, not something
either issue scoped.

```bash
nyxgpt cloud user-data --os linux
nyxgpt cloud user-data --os macos
```

### Options

| Flag | Description |
| --- | --- |
| `--os {linux,macos}` | Required. Target instance OS family. |
| `--version <version>` | Pin the installed nyxGPT version. Linux: `pip install nyxgpt==<version>`. macOS: recorded in the script for reference only -- the Homebrew tap always tracks its current formula (see [Remote tap](homebrew.md#remote-tap)), not an arbitrary pinned release. Default: latest. |
| `--session-backend {file,cassandra}` | Where the instance stores chat sessions (#3865). Default: `cassandra` on Linux, `file` on macOS -- see below. |
| `--output <path>` | Write the rendered script to `path` instead of stdout. |

**Why the session-backend default differs by target OS.** The Linux template
runs `nyxgpt ops install`, which provisions the `nyxgpt-cassandra` container
as a core service, so `cassandra` is available on the instance and is the
default -- matching the Kubernetes overlay, and giving every mode pointed at
the same Cassandra one shared session list. The EC2 Mac template deliberately
does *not* run that path (it installs the two Homebrew formulas and starts
them -- see [What the rendered script does](#what-the-rendered-script-does)),
so nothing provisions a Cassandra there and the default is `file`. Passing
`--session-backend cassandra` on macOS is supported for an operator who
points `[rag] cassandra_hosts` at a Cassandra they run elsewhere. Both
templates apply the choice with `nyxgpt ops session-backend`, before the
services start, so no instance ever needs a hand-edited `config.ini`. See
[session-storage.md](session-storage.md).

### What the rendered script does

**`--os linux`** (Amazon Linux 2023, Ubuntu 22.04/24.04 LTS -- see the
[support matrix](#target-os-support-matrix) below), in order:

1. **Prerequisites**, via the AMI's own package manager (`dnf`/`apt`):
   Python 3 + pip (+ `python3-venv` on Ubuntu) *plus an explicitly-versioned
   Python that meets nyxGPT's `>=3.11` floor* — the AMI's `python3` is 3.9 on
   Amazon Linux 2023 and 3.10 on Ubuntu 22.04, and the CLI venv is built from
   the resolved interpreter, never from a `python3` assumed new enough (see
   [Python on the instance](#python-on-the-instance)) — a Docker engine
   (`docker`/`docker.io`), and Node 20 from NodeSource. All three are
   required by `nyxgpt ops install` and none ship on a stock AL2023 or
   Canonical Ubuntu AMI: it builds a Python venv for the API, runs `npm
   ci`/`npm run build` for the web bundle, and creates the
   `nyxgpt-cassandra` container (`_ensure_cassandra_container` in
   `src/nyxgpt/ops.py`) -- the one Docker-managed piece of an otherwise
   native install. The distro Node packages are too old (Ubuntu 22.04 ships
   Node 12, AL2023 ships Node 18), hence NodeSource.
2. **Docker enablement**: `systemctl enable --now docker`, plus
   `usermod -aG docker` for the target user, since `ops install` shells out
   to `docker` as that user and never as root.
3. **nyxGPT itself**, from PyPI, under the AMI's default login user
   (`ec2-user`/`ubuntu`, never root), into a dedicated venv at
   `~/.nyxGPT/opt/nyxgpt-cli`. A venv rather than `pip install --user`
   because Ubuntu 24.04 LTS marks its system Python
   [PEP 668](https://peps.python.org/pep-0668/) externally-managed, which
   makes a `--user` install a hard error.
4. **Ollama**, via its official installer.
5. **A usable systemd --user session**: `loginctl enable-linger` (so units
   survive with no interactive login), then a bounded wait for
   systemd-logind to create `/run/user/<uid>` and its per-user D-Bus bus.
   Every subsequent `sudo -u` call forwards `XDG_RUNTIME_DIR` and
   `DBUS_SESSION_BUS_ADDRESS` -- `sudo -u` starts a bare process with none
   of a login session's environment, so without them `systemctl --user`
   inside `ops install` has no service manager to talk to and every unit
   fails to start.
6. **Preflight assertions** that `systemctl --user` and `docker` are both
   reachable *as the target user*, so a broken instance fails with a message
   naming the cause instead of a pile of unit-start errors.
7. Seeds `~/.nyxGPT/config.ini` from the packaged `example.config.ini` and
   runs `nyxgpt ops install --skip-observability` -- the same native
   (systemd --user) path #3508 added and `scripts/systemd-native-smoke.sh`
   exercises in CI.

**`--os macos`** (EC2 Mac -- see the [support matrix](#target-os-support-matrix)
below): installs Homebrew if missing, `brew tap`s the remote tap
(`dkblinux98/nyxgpt`, the `dkblinux98/homebrew-nyxgpt` repository),
`brew tap-trust`s it so the non-interactive install does not stop at
Homebrew's third-party tap gate (#3752), installs
`nyxgpt-api`/`nyxgpt-web`, seeds `~/.nyxGPT/config.ini`, and starts both
via `brew services`. This
follows [the documented local remote-tap flow](homebrew.md#remote-tap)
exactly, in the script itself: a fresh EC2 Mac has neither Homebrew nor
`nyxgpt` on it, so the bootstrap installs the formulas directly rather than
installing a CLI first only to have it do the same thing. (`nyxgpt ops
install` reaches the same remote tap when it runs on a machine with no
checkout -- `_install_homebrew_api` in `src/nyxgpt/ops.py`, #3759.)

**Repo-less (CLAUDE.md, 2026-08-01):** neither script ever runs `git
clone` -- the PyPI package and the remote Homebrew tap are the only
sources of the application, so both work on an instance with no repo
checkout.

### Target-OS support matrix

| Target | Family / instance type | OS version | Install path |
| --- | --- | --- | --- |
| Linux AMI | Amazon Linux 2023 (x86_64, arm64) | current | PyPI + systemd --user (#3508) |
| Linux AMI | Ubuntu 22.04 / 24.04 LTS (x86_64, arm64) | current | PyPI + systemd --user (#3508) |
| EC2 Mac | `mac2.metal` / `mac2-m2.metal` / `mac2-m2pro.metal` (Apple Silicon) | Sonoma 14, Sequoia 15 | Remote Homebrew tap + launchd |
| EC2 Mac | `mac1.metal` (Intel) | Ventura 13, Sonoma 14 | Remote Homebrew tap + launchd |

EC2 Mac instances require a
[Dedicated Host](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-mac-instances.html)
with a 24-hour minimum allocation -- an AWS billing/allocation constraint,
not a nyxGPT one. Any other Linux distro (no systemd, e.g. Alpine) or
Windows AMI is out of scope, per the native-install OS dispatch
(`_unsupported_os_result` in `src/nyxgpt/ops.py`) and CLAUDE.md's
Repo-less Portability section (Windows explicitly out of scope for
portability targets).

`LINUX_AMI_SUPPORT_MATRIX`/`MACOS_EC2_SUPPORT_MATRIX` in
`src/nyxgpt/cloud_provision.py` are this table's source of truth.

### CI coverage

`.github/workflows/release-artifacts.yml`'s `ec2-linux-user-data-smoke` job
renders the Linux script with `nyxgpt cloud user-data` from the
just-published PyPI artifact (no repo checkout) and runs it end-to-end on
`ubuntu-latest`, verifying the same install → verify → down cycle as
`artifact-install-smoke`.

Crucially, it targets a **purpose-created account that has never logged
in** (`nyxgpt-ec2`), not the runner's own `runner` account. `runner` has an
active logind session and is already in the `docker` group, so bootstrapping
into it would pass even if the script forgot to install Docker or to forward
`XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS` -- exactly the failure modes an
EC2 instance's first boot hits. The job asserts both preconditions (no
`/run/user/<uid>`, no Docker access) *before* running the bootstrap, so it
cannot silently drift back into masking them, and it verifies units and the
`nyxgpt-cassandra` container as the target user afterwards.

EC2 Mac has no CI coverage -- GitHub Actions has no macOS EC2 runner, and
Apple's licensing does not permit running macOS in a container -- so the
macOS support matrix above is documentation-verified, not CI-verified (the
acceptance criteria call for CI coverage "where feasible (Linux at
minimum)"). This is about EC2 Mac specifically, not about macOS as such:
plain Homebrew installs *are* CI-verified on hosted macOS runners by
[`macos-brew-smoke.yml`](../.github/workflows/macos-brew-smoke.yml), which is
what covers `brew install nyxgpt-api` on a real Mac. One consequence worth an owner/manual verification pass on a
real `mac*.metal` instance: the macOS script's `brew services start` calls
depend on a launchd session for the login user, the launchd analogue of the
systemd session the Linux script sets up explicitly. EC2 Mac's default
`ec2-user` does auto-login to a GUI session at boot, so this is expected to
work, but it has not been exercised on real hardware.

---

## Guided AWS credentials setup (P6-13, #3512)

Every `nyxgpt cloud` command (and `[secrets] provider = ssm`/`secretsmanager`
above) ultimately calls boto3, which needs AWS credentials available
somewhere. `nyxgpt cloud credentials-setup` walks through getting a profile
in place, with the same masked-entry, what-it-is/where-to-get-it treatment as
the guided secrets flow (#3505) -- but the AWS access key ID/secret access key
collected here are **never written to `config.ini`**. They're routed
instead to one of:

| Destination | Where the key pair goes |
| --- | --- |
| `profile` (default) | `~/.aws/credentials`, under the chosen profile name -- exactly what `aws configure --profile <name>` would produce |
| `keychain` | The OS keychain, via the optional `keyring` package (`pip install nyxgpt[cloud]`) |
| `ambient` | Nowhere -- credentials are already available some other way (an existing profile, an EC2 instance role, an SSO session, environment variables) and nothing is written |

Only the non-secret *reference* -- profile name, region, and which
destination was chosen -- is written to `config.ini`'s `[cloud]` section, so
`cloud.py`/`cloud_secrets.py` can find it again:

```ini
[cloud]
profile = nyxgpt
region = us-east-1
credentials_source = profile
```

```bash
$ nyxgpt cloud credentials-setup
============================================================
nyxGPT Guided AWS Credentials Setup
============================================================
AWS profile name [nyxgpt]:
AWS region [us-east-1]:

How should nyxGPT get AWS credentials?
  1) Enter an access key pair -- written to ~/.aws/credentials
  2) Enter an access key pair -- stored in the OS keychain instead of a file
  3) Already configured elsewhere (existing profile, instance role, SSO, env vars)
Choice [1]: 1
AWS access key ID:
AWS secret access key:

Saved -- profile='nyxgpt' region='us-east-1' destination='profile'.
Access key written to /home/you/.aws/credentials under [nyxgpt].
```

The same flow optionally walks through the `[secrets]` provider reference
above (provider/region/ssm_prefix/secretsmanager_id) in one pass, so a
cloud-deploy setup doesn't need a separate detour through the general
Configuration Wizard -- those fields aren't secret values themselves (the
actual application secrets stay in SSM/Secrets Manager), just which store to
use.

**This flow is terminal-only (#3805).** The `/admin/aws-credentials` screen
that used to offer the same entry was removed: an access key pair typed into
a browser crosses an HTTP request and the page's process on its way to disk,
and over the SSH access tunnel it would cross that path too -- while the CLI
takes masked input and writes straight to `~/.aws/credentials` or the OS
keychain. AWS credentials are also needed *before* there is a deploy to
observe, so the screen was too late to be useful. `GET
/api/v1/config/aws-credentials` remains as the read-only status tooling can
query (masked, never cleartext); no HTTP path writes a credential.

---

## Cloud secrets (SSM / Secrets Manager)

On a cloud (AWS) deploy, `[auth] api_key`, `[openai] api_key`, and
`[github] pat` must never be baked into an AMI, user-data script, tfvars
file, or `config.ini` itself (P6-10, #3507). Set `[secrets] provider` in
`config.ini` and nyxGPT resolves those three credentials from AWS at read
time instead:

```ini
[secrets]
provider = ssm            # or "secretsmanager"
region = us-east-1        # optional -- falls back to boto3's normal region resolution
ssm_prefix = /nyxgpt       # provider = ssm
secretsmanager_id = nyxgpt # provider = secretsmanager
```

Leaving `provider` blank (the default) is a local deploy: the three
credentials are read from `config.ini` exactly as before, unaffected.

### SSM Parameter Store layout (`provider = ssm`)

One `SecureString` parameter per credential, under `ssm_prefix`:

| Parameter | Value |
|---|---|
| `{ssm_prefix}/auth_api_key` | The shared secret checked by `[auth] enabled` middleware |
| `{ssm_prefix}/openai_api_key` | OpenAI API key |
| `{ssm_prefix}/github_pat` | GitHub Personal Access Token |

```bash
aws ssm put-parameter --name /nyxgpt/auth_api_key --type SecureString --value "..."
aws ssm put-parameter --name /nyxgpt/openai_api_key --type SecureString --value "..."
aws ssm put-parameter --name /nyxgpt/github_pat --type SecureString --value "..."
```

Only credentials actually used need to be set -- a missing/unreadable
parameter resolves to an empty value for that credential (see "Failure
behavior" below), not an error that blocks the others.

### Secrets Manager layout (`provider = secretsmanager`)

One secret, at `secretsmanager_id`, holding a single JSON object with all
three keys:

```bash
aws secretsmanager create-secret --name nyxgpt --secret-string '{
  "auth_api_key": "...",
  "openai_api_key": "...",
  "github_pat": "..."
}'
```

Secrets Manager bills per secret rather than per value, so one secret with
several keys is the natural fit here (unlike SSM, which is priced and
structured per parameter).

### IAM permissions

The instance role needs read access to whichever provider is configured:

- `provider = ssm`: `ssm:GetParameter` on the `ssm_prefix` path, plus
  `kms:Decrypt` on the key used to encrypt the `SecureString` parameters
  (the default `alias/aws/ssm` key, unless a customer-managed key is used).
- `provider = secretsmanager`: `secretsmanager:GetSecretValue` on
  `secretsmanager_id`.

No other AWS permissions are required for secret resolution.

### Rotation

Rotate a credential by updating its value in AWS -- `aws ssm put-parameter
... --overwrite` or `aws secretsmanager update-secret ...` -- nothing on
the instance needs to change:

- Resolved values are cached in-process for 5 minutes, so a rotation takes
  effect on its own within that window without a restart.
- To force it immediately, restart the API: `nyxgpt ops restart api`.

**The `/admin/access` dashboard's "rotate API key" button is disabled when a
cloud secrets provider is configured.** `get_auth_api_key` always prefers the
AWS-resolved value over `config.ini`, so a rotation written to `config.ini`
by that endpoint would be inert -- the middleware would keep enforcing the
old cloud-stored key while the dashboard reported the new one as active.
`POST /admin/access` rejects `{"rotate": true}` with `400` in that case;
rotate via the AWS CLI/console as above instead.

### Failure behavior

If a provider is configured but AWS resolution fails (missing parameter,
denied IAM permission, boto3 not installed, etc.), that credential
resolves to `""` -- it is never silently satisfied by falling back to a
`config.ini` value cloud deploys don't populate anyway. For `[auth]
api_key` this fails *closed*: with auth enabled and an empty expected key,
no provided key can ever match, so the API rejects every request rather
than accepting none. For `[openai] api_key` / `[github] pat`, that
integration simply doesn't work until the underlying AWS issue is fixed;
check the nyxGPT process logs for a `Cloud secret resolution failed for
...` warning naming the failing key, the provider and the exception class
(e.g. `CloudSecretsError`). The provider's own message is deliberately not
in that warning -- nothing constrains an SDK error string to be free of the
secret payload it was handling -- so raise the log level to `DEBUG` when you
need it (`[logging] level = DEBUG`, then `nyxgpt ops restart api`).

A sustained failure (outage, bad IAM, wrong prefix) is remembered for only
30 seconds (vs. the 5-minute success cache), so resolution is retried
periodically rather than requiring a restart once the underlying issue is
fixed.

### Testing

`nyxgpt[cloud]` (`pip install "nyxgpt[cloud]"`) is required at runtime for
either provider -- see `src/nyxgpt/cloud_secrets.py`. Tests exercise both
providers against a mocked boto3 client (no live AWS dependency); see
`tests/unit/test_cloud_secrets.py`.

---

## PyPI publishing: rc and stable

Every install path above pulls nyxGPT **from PyPI** -- `pip install nyxgpt`
on a clean machine, `pip install nyxgpt==<version>` in the rendered
[user-data bootstrap](#target-os-provisioning-p6-12-3511), the same pin in
`nyxgpt cloud deploy`'s remote provisioning script. That is the whole point
of the repo-less requirement, and it has one consequence: acceptance testing
can only ever exercise code that has been *published*. Fix an acceptance
failure, merge it to the release branch, and the clean-machine run still
installs the last stable release, without the fix.

**One pipeline** closes that gap (#3727):
`.github/workflows/release-publish-pypi.yml` builds and publishes the
release-branch tip on two channels (#3735). There is no second release
workflow and no second script -- the ceremony delegates to this one too --
and **nothing is published on a schedule**: every publish is a deliberate
dispatch, by the owner or by the sprint autopilot.

| Channel | Version | Trigger | Who runs it |
| --- | --- | --- | --- |
| `rc` | `3.0.0rcN` | the sprint autopilot parking at agentic-work-complete, a manual dispatch, or `nyxgpt release publish --publish` | automatic + owner |
| `stable` | `3.0.0` | `scripts/release_ceremony.sh` Phase 2 | owner, ceremony only |

PEP 440 orders them `3.0.0rcN < 3.0.0`, so a candidate can never shadow the
release.

An rc build is **acceptance-only**. It is never announced and never a
release, and no ceremony step (master merge, release tag, stable Homebrew
formulas, GitHub Release, sign-off) runs for it.

The `rc` channel is the one with a step past PyPI: because macOS installs
with `brew`, not `pip`, an rc also cuts a GitHub **prerelease** carrying the
service tarballs and stamps `nyxgpt-api@3.0.0rc` / `nyxgpt-web@3.0.0rc` into
the Homebrew tap -- see
[Accepting a candidate on macOS](#accepting-a-candidate-on-macos) below.

### Cutting a release candidate

```bash
# What would be published, and whether the guardrails allow it here.
nyxgpt release publish

# Cut it: dispatches release-publish-pypi.yml on the release branch. The RC
# number is the next unused one, read from PyPI.
nyxgpt release publish --publish

# Or a specific number, when a run failed after upload and you need to skip one.
nyxgpt release publish --publish --number 4
```

`nyxgpt release rc` is kept as shorthand for `--channel rc`.

The command reports the release line, which RCs PyPI already serves, the
next version, the tap formulas that candidate installs as, and -- if it
cannot be cut from where you are -- exactly why. Publishing carries the
owner's credentials, so it is a terminal command and a dispatch-only
workflow, never a button.

The workflow builds an sdist and a wheel from the tip with
`pyproject.toml`'s version rewritten to the resolved version (build-time
only -- it is never committed), runs `twine check` and a clean-venv smoke
install that asserts the artifact reports the version it claims, publishes,
and then polls pypi.org until it serves it. Dispatch it with
`dry_run: true` to do everything except the upload.

### Candidates cut themselves at agentic-work-complete

Most rounds need no command at all (#3729). The owner's cadence is: wait for
the sprint to reach **agentic work complete**, run a full acceptance round,
file failures and improvements, repeat -- and the moment the sprint autopilot
detects that state is exactly the moment a candidate should exist on every
platform. So the park transition into `awaiting_acceptance`
(`_autopilot_publish_rc` in `scripts/agents/lib/gh_project.sh`) dispatches
this pipeline with `channel=rc`, and the park note on the release tracking
issue names the version to install:

> 📦 **Release candidate for this acceptance round:** `3.0.0rc2` is
> publishing now from `v3.0.0` -- PyPI plus this line's Homebrew candidate
> formulas, in one run.

Three things bound it:

- **Only that state.** A sprint with work still in flight has nothing to
  accept, and a sprint already promoted to *For Release* has had its round.
  The decision is #3709's park state -- there is no second state machine.
- **Only `rc`.** The channel is a constant, re-checked at the dispatch. A
  release needs the ceremony's tag and confirmation token, which this path
  does not have and cannot obtain.
- **No duplicates.** The rc channel carries a tip guard: an rc dispatch
  whose release-branch tip has not moved since the last published candidate
  resolves to `SKIP`. Repeat observations of the
  same parked state therefore publish nothing, and the park note names the
  existing candidate instead. (An explicit `number`, and a `dry_run`, opt
  out of the guard -- both are deliberate acts.)

The guard reads this workflow's own run history for the last successful rc,
which is why the `run-name:` at the top of the file is load-bearing: both
channels arrive on the same `workflow_dispatch` event, so the rendered title
("publish rc from v3.0.0") is the only record of which channel a finished
run built.

Reading history is also why the pipeline runs under a `concurrency` group
(`pypi-publish`, `cancel-in-progress: false`). The guard can only answer for
candidates already in that history, so two runs overlapping in the window
before either has published both read "this tip has no candidate" and both
publish -- which is how `3.0.0rc7` and `3.0.0rc8` were cut from one tip and
rc7 burned dead. The group makes a second cut *wait* rather than race: it
resolves its version only once the first run is finished and visible. It is
never cancelled, because a run killed mid-upload can leave PyPI serving a
version no successful run records -- the one state the guard cannot see.
Nothing is lost by waiting: the queued run re-evaluates the guard when it
starts and resolves to `SKIP` if the first run published this tip's
candidate.

The two layers deliberately read the history differently:

* **inside the pipeline**, the guard counts *finished successes only*. The
  concurrency group already guarantees the winning cut is finished and
  visible by the time a queued run looks, so any other unfinished run in the
  listing is queued *behind* the caller and has published nothing. Deferring
  to that one would be backwards and mutual -- the running cut would skip for
  the queued one and the queued one for the first's skip-success, leaving a
  tip with no candidate looking permanently claimed;
* **the autopilot's preflight** decides whether to dispatch at all, so it
  never queues behind the group and nothing else stops it firing alongside a
  cut in progress. It is the stricter reader: a run that is still *in flight*
  already owns its tip (`run_claims_tip(..., include_in_flight=True)`).

Neither layer counts a *failed* run: its number went unused, so a retry on
the same tip stays free to publish.

### Accepting a candidate on macOS

macOS installs nyxGPT with `brew`, not `pip`, so a PyPI-only candidate would
leave the whole macOS path acceptance-testable only one release behind. An
`rc` publish therefore also (#3727):

1. cuts a GitHub **prerelease** for the RC version -- marked prerelease and
   explicitly not "latest" -- with the `nyxgpt-api`/`nyxgpt-web` source
   tarballs as assets, and
2. pushes stamped `nyxgpt-api@<release>rc` / `nyxgpt-web@<release>rc`
   formulas to the remote tap, built from the same `homebrew/tap/*.rb.tmpl`
   templates the stable formulas come from. The name carries the release
   line the candidate belongs to (#3735), so a machine on `@3.0.0rc` never
   silently crosses to the next line's candidates.

**A candidate release is published complete or not at all.** Releases in this
repository are immutable: once published, one can never gain or change an
asset. The tarballs are therefore attached in the same `gh release create`
call that publishes the prerelease -- never uploaded afterwards, which is what
`HTTP 422: Cannot upload assets to an immutable release` used to cost a
candidate cycle (#3747) -- and the job reads the release back and refuses to
stamp the formulas unless both tarballs are on it. A leftover candidate
release that is missing one is deleted (or, if the platform refuses the
delete, banner-marked "superseded" in its notes) by
`scripts/supersede_incomplete_rc_releases.sh`, which the job runs before it
cuts a new candidate.

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # one-time per machine (docs/homebrew.md)
brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc

nyxgpt up
```

`nyxgpt up`, not `brew services start` -- this block used to end with the
per-service commands, which is the sequence the owner ran in #3854 and which
produced an acceptance candidate with no Ollama, no Cassandra and no
observability. `brew services start` starts the one service it names; only
`nyxgpt up` reaches `ops.install()`, which is what installs Ollama, creates
the `nyxgpt-cassandra` container and brings the observability profiles up. It
starts the Homebrew services too, so restart-at-login still works.

**The stable formulas are never touched.** Homebrew has no pre-release
semantics, so `brew install nyxgpt-api` staying on the latest stable release
depends on an rc publish not producing a `nyxgpt-api.rb` at all -- which is
exactly what it does, asserted in the job and in
`tests/unit/test_build_homebrew_artifacts.py`. The `stable` channel never
reaches the tap job at all -- the stable formulas are the ceremony's. Full
detail, including how to switch a machine between channels (the candidate
formulas `conflicts_with` the stable ones), is in
[docs/homebrew.md](homebrew.md#release-candidate-formulas-rc-channel).

### Pointing an acceptance run at a specific build

The provisioning templates already pin exactly, so a candidate needs no
special handling -- pass it wherever a version goes:

```bash
pip install nyxgpt==3.0.0rc3
nyxgpt cloud user-data --os linux --version 3.0.0rc3
nyxgpt cloud deploy --version 3.0.0rc3
```

The exact `==` pin is what makes this work at all: pip excludes
pre-releases from an unpinned requirement, so `pip install nyxgpt` keeps
resolving to the latest **stable** release for every ordinary user, no
matter how many candidates exist. (`pip install --pre nyxgpt` is the
other way to opt in, if you want the newest pre-release without naming it.)

### The release ceremony delegates here

`scripts/release_ceremony.sh` Phase 2 no longer builds or uploads anything
itself. It dispatches this same workflow with `channel=stable`, waits for
the run, and then verifies pypi.org serves the release -- one publish
mechanism, two entry points. The ceremony keeps only what is
ceremony-exclusive: the master fast-forward, the tag, the GitHub Release,
the Homebrew tap, the project close-out and the human stop points.

Because of that, the ceremony needs **no PyPI credential at all**; the
`--skip-pypi` flag still skips the phase.

### Guardrails

| Guardrail | How it is enforced |
| --- | --- |
| Dispatch trigger only | The workflow has no `schedule`, `push`, `tag` or `release` trigger -- nothing is published without being asked for, and nothing is published on a timer (#3735) |
| Release branches only | The version step runs `python -m nyxgpt.release_candidate`, which exits non-zero for any ref that is not `v<X.Y.Z>` matching `pyproject.toml`'s declared version |
| A candidate is never a stable version | What an rc uploads is always `<release>rcN` -- a pre-release, which default installs skip |
| An rc never clobbers the stable brew formulas | The rc tap job writes `nyxgpt-api@<release>rc.rb`/`nyxgpt-web@<release>rc.rb` only; it asserts no stable formula was produced and refuses to push if one would change, so `brew install nyxgpt-api` stays on the latest stable release |
| A candidate never crosses release lines | The formula name carries the line (`nyxgpt-api@3.0.0rc`), so the next line's candidates are a different formula -- installing them is a deliberate act, and the ceremony retires a shipped line's candidates by name |
| An rc's GitHub release is never "latest" | It is created with `--prerelease --latest=false` and verified afterwards -- which also keeps `release-artifacts.yml` (trigger: `released`, not `prereleased`) out of the rc path |
| A candidate release always carries both tarballs | Releases here are immutable, so the assets are attached in the `gh release create` call itself and the release is read back before the formulas are stamped; an existing release missing one is retired rather than uploaded to (#3747) |
| The ceremony's formulas are never written by a candidate | `homebrew-tap-rc` is gated on `channel == 'rc'`, so the `stable` channel cannot reach it by construction, not by convention |
| Stable is ceremony-only | The stable channel additionally requires the release tag at the built commit (Phase 1 creates it) *and* the ceremony's confirmation token, so dispatching `channel=stable` by hand publishes nothing -- and the sprint autopilot's dispatch path hard-codes `rc` and refuses any other channel before it dispatches |
| The autopilot never cuts a duplicate candidate | An rc dispatch on a release-branch tip that has not moved since the last published candidate resolves to `SKIP`, so re-observing the same parked state publishes nothing |
| Two cuts never race | The pipeline runs under the `pypi-publish` concurrency group with `cancel-in-progress: false`, so a second dispatch queues behind the running one and re-evaluates the tip guard against a history that already contains it, instead of both reading "no candidate yet" and both publishing (#3771) |
| No version reuse | The next number comes from what PyPI already serves, and PyPI rejects a re-upload anyway |

The branch check and the version arithmetic live in
`src/nyxgpt/release_candidate.py` (unit-tested), not in the workflow's YAML,
so CI, the ceremony and the CLI cannot drift apart about what a channel
publishes.

### Owner setup (one-time)

Publishing authenticates with **PyPI Trusted Publishing (OIDC)**. No PyPI
token is stored in the repo, in Actions, or in `config.ini`.

On pypi.org, project `nyxgpt` → *Publishing* → add a GitHub publisher:

| Field | Value |
| --- | --- |
| Owner | `dkblinux98` |
| Repository | `nyxGPT` |
| Workflow name | `release-publish-pypi.yml` |
| Environment | *(blank)* |

The job's `id-token: write` permission mints the OIDC token. The workflow
filename above is part of the publisher's identity -- if it is ever renamed,
update the publisher first or every publish will be rejected.

`nyxgpt release publish --publish` additionally needs `[github] pat`,
`repo_owner` and `repo_name` in `config.ini` -- the same values
`nyxgpt ops secrets-sync` already uses -- because it dispatches the workflow
through the GitHub API.

---

## How `allow-ip` and the Terraform module coexist

`allow-ip` mutates the security group's port-22 ingress rule directly via the
AWS API, outside of Terraform -- it has to, because it is the lockout-recovery
path and the owner cannot reach the instance to do anything else. The
substrate module (below) is built so a routine apply doesn't fight it:

- The security group's `ingress` is declared inline with
  `lifecycle { ignore_changes = [ingress] }`
  (`terraform/aws/modules/security/main.tf`), so a later
  `nyxgpt cloud infra apply` leaves an `allow-ip` refresh in place instead of
  reverting it to whatever CIDR was in tfvars. **After the group exists,
  `nyxgpt cloud allow-ip` -- not a re-apply -- is how the SSH source
  changes.** Egress remains Terraform-managed and is reconciled normally.
- `nyxgpt cloud infra apply` writes `security_group_id` and `region` (plus the
  instance/VPC ids) to `~/.nyxGPT/cloud/state.json`, so `allow-ip`
  auto-discovers its target with no `--security-group-id`/`--region`.
- The module sets no `user_data` today: `nyxgpt cloud deploy` provisions the
  instance over SSH after apply. If a first-boot bootstrap is ever wanted
  there (it is the only path that works for **EC2 Mac**), the `user_data`
  should be
  [`nyxgpt cloud user-data --os <linux|macos>`](#target-os-provisioning-p6-12-3511)'s
  rendered output for the chosen AMI family rather than a script templated
  inside the module.

## Lockout recovery

If you're locked out of an AWS-deployed instance because the SSH rule no
longer matches your current IP:

1. **First resort:** run `nyxgpt cloud allow-ip` from the machine with the
   new IP. It only needs AWS API credentials, not access to the instance
   itself, so it works even though SSH is currently refused.
2. **Fallback (no local AWS credentials available):** update the security
   group's port-22 ingress rule directly from the AWS Console (EC2 →
   Security Groups → the deployment's group → Edit inbound rules), or the
   AWS CLI (`aws ec2 authorize-security-group-ingress` /
   `revoke-security-group-ingress`) from any machine with credentials for
   the account. Scope the new rule to your current public IP only -- never
   `0.0.0.0/0`.

Once the rule is refreshed, `nyxgpt cloud tunnel` (or a direct
`ssh -L ...`, see
[`docs/security.md#network-security`](security.md#network-security)) reaches
the instance again.
