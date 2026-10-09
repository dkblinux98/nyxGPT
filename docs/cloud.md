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
pip install "nyxgpt[cloud]"          # a fresh install
nyxgpt ops install-extra cloud       # an install you already have
```

`boto3` is kept out of the base install -- it's only needed for AWS
deployments, not the local stack every other `nyxgpt` command drives.

The second form exists because the first one only works when `pip` and
`nyxgpt` share an environment. On a Homebrew keg they do not: `pip` is not on
`PATH`, `pip3` is a different interpreter whose packages `nyxgpt` never reads,
and the only pip that reaches the right virtualenv is a raw path into the
Cellar. `nyxgpt ops install-extra` installs into the interpreter running the
command -- the one that will do the importing -- so it is correct on a keg, a
wheel, a virtualenv and a checkout alike. Run it with no argument to list the
extras and see which environment it would install into.

A cloud instance provisions from published artifacts and never clones this
repository (the one exception, [`--dev`](#dev-mode-on-a-cloud-target), copies
your tree over SSH and still clones nothing), so the documentation on it is
the copy inside the installed
package: reach it in the tunneled web UI under **Support → Docs**, which
renders the product documentation that shipped with the deployed version. **File an Issue**
sits beside it in the same menu and files the ticket from the instance itself,
so a report never means leaving the tunneled UI. See [ui.md](ui.md#support-menu).

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
nyxgpt cloud deploy
```

One command takes you from nothing to a running, monitored stack you can
reach from your workstation. It asks which AWS account and which SSH key to
use, offering a default you take with Enter
([Which account, and which SSH key](#which-account-and-which-ssh-key-4186));
pass `--profile` / `--ssh-public-key ~/.ssh/id_ed25519.pub` to answer up
front, or `--yes` to take the defaults without being asked. Then it:

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
`--owner-ip`, `--ssh-key-name`, `--ssh-public-key`, `--instance-type`,
`--root-volume-size`) works here too and is remembered for later runs. None of
them is required: `--profile` and the two SSH flags are *asked for* when
absent, with the resolved value as the default
([Which account, and which SSH key](#which-account-and-which-ssh-key-4186)).
Plus:

| Flag | Meaning |
| --- | --- |
| `--os {auto,linux,macos}` | Which target OS's bootstrap to drive (default `auto`: `macos` for a `mac*.metal` instance type, `linux` otherwise). With no `--host`, `--os macos` prices and allocates an EC2 Mac Dedicated Host after a typed confirmation. See [EC2 Mac targets](#ec2-mac-targets) |
| `--yes` | Skip the typed confirmation before allocating a Dedicated Host, so `--os macos` stays scriptable. The cost disclosure is still printed. Also takes the resolved AWS profile and SSH key without asking (#4186) |
| `--mac-instance-type` | EC2 Mac type to allocate a host for (default `mac2.metal`, the cheapest family) |
| `--mac-az` | Availability zone for the Dedicated Host (default: the first zone EC2 says offers the family) |
| `--mac-ami-id` | Pin the macOS AMI (default: the newest `amzn-ec2-macos-*` for the type's architecture) |
| `--version` | Published release to install on the instance (default: this CLI's own version, then whatever the last deploy used). Ignored under `--dev` |
| `--dev` | Deploy **your working tree** instead of a published release — Linux targets only, see [Dev mode on a cloud target](#dev-mode-on-a-cloud-target) |
| `--kubernetes` / `--no-kubernetes` | Run the stack on a single-node k3s cluster on the instance instead of natively, applying the same `k8s/*.yaml` manifests — this is what makes `nyxgpt cloud canary` available. Linux targets only. Remembered for later runs. See [Kubernetes on the instance](#kubernetes-on-the-instance-3956) |
| `--skip-observability` | Deploy the core app only, without monitoring/logging/tracing/errors (implied by `--os macos`, which can host no containers — the *core* app, Ollama included, is installed there in full) |
| `--session-backend` | Where the instance stores chat sessions: `cassandra` (default — shared with every mode pointed at the same Cassandra) or `file` (JSON on the instance's own disk). Remembered for later runs, so a re-deploy never silently moves an instance's sessions back to files. Refused with `--kubernetes` for anything but `cassandra`, which the cluster's ConfigMap fixes. See [session-storage.md](session-storage.md) |
| `--no-tunnel` | Don't open the tunnel (and so don't health-check through it); prints the `nyxgpt cloud tunnel` command to run instead |
| `--ssh-user` | Login user on the instance (default `ec2-user`, the Amazon Linux 2023 default) |
| `--identity-file` | Private key to authenticate with (default: whatever the last deploy used, then whatever `ssh` would pick from `~/.ssh` and your agent) |
| `--host` | Target an existing box instead of the provisioned instance |
| `--health-timeout` / `--ssh-timeout` | Seconds to wait for `/health` (default 900) and for SSH. The SSH default is **derived from the target OS**: 300 for Linux, which boots in about a minute, and 2700 for an EC2 Mac, whose first boot on a freshly allocated Dedicated Host runs Apple's own setup and has measured 18 minutes. You should not need to pass `--ssh-timeout` on either |
| `--status` | Superseded by [`nyxgpt cloud status`](#nyxgpt-cloud-status--where-is-my-instance-3813); still prints the same JSON for anything already scripted against it |

### EC2 Mac targets

`nyxgpt cloud deploy --os macos` provisions an EC2 Mac the same way it
provisions a Linux instance: **nyxGPT allocates the Dedicated Host, launches
the Mac on it, renders the macOS bootstrap and pipes it to the machine over
the wrapped SSH path itself.** There is no script to copy, nowhere to paste
one, no `aws` command to run and no AWS console step (#3867, #3995).

```bash
nyxgpt cloud deploy --os macos              # prices a host, asks, allocates, deploys
nyxgpt cloud deploy --os macos --yes        # same, without the typed confirmation
nyxgpt cloud deploy --os macos --host <ip>  # a Mac you already have
```

#### Allocating the Dedicated Host

macOS runs only on EC2's Mac instance types (`mac1.metal`, `mac2*.metal`),
which require a
[Dedicated Host](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-mac-instances.html).
An allocated host bills a **24-hour minimum** whether or not an instance runs
on it, and **AWS refuses to release one before that window closes**. That is a
real, non-refundable charge, so `--os macos` with no `--host` discloses it and
asks before allocating anything:

```
`--os macos` needs an EC2 Mac, and an EC2 Mac needs a Dedicated Host.
nyxGPT can allocate one now. Read this first -- it is a real, non-refundable charge:

  Host family        mac2 (instance type mac2.metal)
  Region / AZ        us-east-1 / us-east-1a
  Rate               $0.6500/hour (USD, live from the AWS Pricing API)
  Minimum charge     $15.60 for the 24-hour minimum, charged even if you destroy in a minute
  Releasable at      2026-08-23T18:30:00 UTC

Type `allocate` to allocate the Dedicated Host, or anything else to stop:
```

- **The rate is looked up, never hardcoded.** It comes from the AWS Pricing
  API, queried for that family in that region at prompt time. The spread
  across Mac families is 2.4× (`mac2` is the cheapest, `mac2-m2pro` the
  dearest), so a constant in the source would be wrong for most operators. If
  the lookup fails, the prompt says `UNKNOWN` and names the reason — it never
  substitutes a number nothing checked. The *instance* really is $0.00/hour:
  on a Dedicated Host you pay for the host.
- **`--yes` skips the typing, not the disclosure.** The block above is still
  printed, so a scripted run leaves the numbers in its log.
- **Which region is used, and it says so.** The region comes from your nyxGPT
  cloud configuration (`--region`, then `~/.nyxGPT/cloud/infra.json`, then
  `config.ini`'s `[cloud]` reference) — *not* from your AWS CLI default, which
  is frequently a different region.
- **Which zone is queried, not assumed.** Mac capacity is per-AZ and differs
  by family. nyxGPT asks EC2 which zones offer the type and uses the first;
  `--mac-az` picks another, and the prompt lists the alternatives.
- **`--mac-instance-type`** chooses the family (default `mac2.metal`);
  `--mac-ami-id` pins the AMI, which otherwise resolves to the newest
  `amzn-ec2-macos-*` for the type's architecture.
- **The host and the Mac live in their own Terraform root module**
  (`terraform/aws/mac`), with their own VPC, their own owner-IP-scoped
  security group and their own state file. Nothing is shared with the Linux
  substrate, which is never applied for a macOS deploy — reconciling it would
  bill for an instance nothing then deploys to.
- **Re-running is safe.** A second `nyxgpt cloud deploy --os macos` reconciles
  the host you already have. It does not re-price, re-ask or allocate a second
  host (which would be a second 24-hour minimum) — **and it re-applies the Mac
  you have, not a newer one.** The AMI the instance booted and its root volume
  size are recorded at allocation and fed back on every reconcile, and the
  module additionally ignores AMI drift on an existing instance. Both matter
  because `ami` and a shrinking root volume force *replacement*, and replacing
  an EC2 Mac terminates it, takes its disk with it, and puts the host into an
  hour-long scrub that the replacement then cannot launch onto. Moving an
  existing Mac to a newer macOS is therefore deliberate: `nyxgpt cloud
  destroy`, then deploy again (or pass `--mac-ami-id` on the first allocation).

What the deploy does once the Mac is up — the same for a host it allocated and
for one you named with `--host`:

- Waits for the Mac to accept SSH, for up to **45 minutes** by default and
  saying so while it waits. An EC2 Mac's first boot on a freshly allocated
  Dedicated Host runs Apple's own setup before sshd listens, and has measured
  18 minutes on a `mac2.metal`; the Linux default of 300s cannot succeed there.
  Raise it with `--ssh-timeout <seconds>` if your family or AMI is slower — and
  note that a re-run **reconciles** the Mac you already have, so a timeout
  costs you no second host and no second 24-hour minimum.
- Runs the [EC2 Mac bootstrap](#what-the-rendered-scripts-do): Homebrew,
  the remote tap, **the formulas that carry the version you asked for**, and
  `brew services start`. Repo-less, like every other install path.
- **Installs the version `--version` names, and proves it.** A release
  candidate is published as a separately named Homebrew formula
  (`nyxgpt-api@3.0.0rc`) so that `brew install nyxgpt-api` can never resolve to
  a pre-release, so the formula names are derived from the version rather than
  fixed: `--version 3.0.0` installs `nyxgpt-api`/`nyxgpt-web`, and
  `--version 3.0.0rc14` installs `nyxgpt-api@3.0.0rc`/`nyxgpt-web@3.0.0rc`. The
  bootstrap then reads `nyxgpt --version` off the keg and **refuses to start
  the stack** if it is not the version requested, rather than bringing up a
  release you were not testing.
- **Exits non-zero if any of that fails.** `nyxgpt cloud deploy` exits 0 only
  when the deploy it recorded actually finished, so a script or CI job can read
  `$?`; an interrupted deploy exits 130. When it fails, the error quotes the
  lines around the **first** error in the bootstrap's output — not the tail,
  which on Homebrew is routinely the output of a later step that worked.
- Elevates with `sudo -n` in `ec2-macos-init`'s place, and tells the script
  which login user to install Homebrew for (your `--ssh-user`). A Mac whose
  login user needs a sudo password fails immediately with sudo's own message
  rather than hanging on a prompt.
- Defaults `--session-backend` to `file`. Nothing on that machine provisions
  a Cassandra — its bootstrap does not run `nyxgpt ops install` — so
  `cassandra` would point the API at a database that is not there. Pass
  `--session-backend cassandra` if you run one elsewhere and point
  `[rag] cassandra_hosts` at it.
- **Installs the full core stack: api, web, and the Ollama model backend with
  the configured models already pulled (#4150).** Chat, RAG and the web UI work
  on an EC2 Mac exactly as they do anywhere else, and a deploy that cannot put
  the models in place fails rather than reporting success. This bullet exists
  because the next one used to absorb it: the model backend was skipped along
  with the container tier and disclaimed under the observability caveat, and
  the owner paid a Dedicated Host's non-refundable 24-hour minimum to find a
  Mac that could not answer a chat message. **Ollama is not observability.** If
  you are reading this to work out what a Mac deploy gives up, the answer is
  the next two bullets and nothing else — and only the first of them is a
  platform constraint.
- Runs **no container tier**: no observability stack (Grafana/Loki/Tempo),
  no GlitchTip, and no `nyxgpt-cassandra` container. This one *is* a platform
  constraint rather than a revisitable scoping choice, and the constraint is
  exactly as wide as the containers: every way of running Docker on macOS
  works by running a Linux VM, and **EC2 Mac instances do not support nested
  virtualization** — so no Docker daemon can exist on that target at all.
  That is the whole of the justification, which is why it can never be
  stretched to cover a native component: Ollama installs from a Homebrew
  formula and needs no container, so it was never in scope for this caveat.
  Do not propose adding the container tier to the Mac path; point
  `--session-backend cassandra` / `[rag] cassandra_hosts` at a Cassandra
  running elsewhere instead. `nyxgpt cloud status` reports the target OS
  so this difference is visible after the scrollback is gone, and so does the
  admin Infrastructure page.
- Leaves the **self-heal watchdog off** — a default this bootstrap does not
  change, *not* a platform limit. The watchdog is a thread inside the api
  process ([self-healing.md](self-healing.md)), so it needs no container and
  nothing about an EC2 Mac prevents it; it ships disabled everywhere, and the
  only difference here is that step 5 of the Linux deploy above turns it on
  explicitly while the Mac bootstrap does not. Turn it on from the admin
  Self-Heal page, or with `nyxgpt self-heal enable` on the instance — see
  [self-healing.md](self-healing.md#turning-it-on). Filing it with the
  container tier would be the same mis-scoping #4150 was about: a toggleable
  default dressed as an impossibility.
- Opens TCP 22 to your address and nothing else. A Mac nyxGPT allocated gets
  its own security group with the same single owner-scoped SSH rule the Linux
  substrate uses, re-detected on every deploy. A Mac you supplied with
  `--host` is not nyxGPT's to configure, so `nyxgpt cloud allow-ip` does not
  apply to it and SSH reachability for *that* machine is yours to arrange.

Everything after provisioning works the same way as the Linux path: an SSH
forward to a loopback address is the only access path, and the app and web UI
still bind `127.0.0.1` on the instance. `nyxgpt cloud tunnel` forwards the
stack's ports; on a Mac there is one more thing worth reaching, and
[`nyxgpt cloud screen`](#reaching-the-macs-screen-4121) forwards that the same
way.

#### Reaching the Mac's screen (#4121)

The only reason to pay a Dedicated Host's 24-hour minimum is that the hardware
is a Mac — and a Mac has a screen. When the platform misbehaves in a way CI
structurally cannot reproduce (EC2 Mac hardware is on the short list in
[live-verification-ci.md](live-verification-ci.md)), looking at it is the test:

```bash
nyxgpt cloud screen                   # enable Screen Sharing + open the forward
nyxgpt cloud screen --status          # is the path open, and what is enabled?
nyxgpt cloud screen --status --show-password   # print the credential
nyxgpt cloud screen --local-port 5902 # forward from a different local port
nyxgpt cloud screen --stop            # close the forward
nyxgpt cloud screen --disable         # close it and turn Screen Sharing off
```

The command does three things and asks you to type none of them:

1. Enables macOS Screen Sharing on the Mac over the same wrapped SSH path
   every other remote step uses.
2. Makes it **loopback-only before it listens** (see below).
3. Forwards `localhost:5901` to the Mac's `127.0.0.1:5900`, and prints the
   `vnc://localhost:5901` address and the account to sign in as.

The local port is **5901 by default, not 5900** — and that is deliberate. On a
macOS workstation `vnc://localhost:5900` is your *own* Screen Sharing, so
Apple's client resolves the address to your machine and refuses with "you can't
control your own screen" before the forward is ever consulted. `--local-port N`
moves it; asking for a port other than the open one closes the open path and
re-opens on the one you asked for, rather than reporting the old port as though
it had satisfied the request.

**Nothing is listening on a non-loopback address, and no port is opened.** This
is [`DECISION_PRIVATE_ACCESS_MECHANISM.md`](../product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md)
applied to the screen exactly as it is applied to the app ports: the Mac's
security group stays **TCP 22 only**, and `nyxgpt cloud screen` never touches
it. A version of this that opened 5900 to your `/32` is not what you get —
that is the alternative the decision compared and rejected.

Getting there takes one step the app ports do not need. macOS's
`com.apple.screensharing` job binds 5900 on *all* interfaces and its launchd
plist is protected by System Integrity Protection, so the bind address is not
nyxGPT's to change. What nyxGPT changes instead is the Mac's own packet filter
— `pf`, which macOS already ships, so nothing is installed:

| Step | Why in this order |
| --- | --- |
| Write and load a `pf` anchor that passes port 5900 on `lo0` and **drops it everywhere else** | Activating the agent first would leave a window, however short, with a network-reachable listener |
| Read the anchor back and check the block rule is in it | `pfctl -f` exits 0 on a ruleset it only warned about, so a zero exit is not evidence |
| Write the credential and verify it with `dscl . -authonly` | A listener that is already running when the credential is written never loads it |
| **Only then** activate the Screen Sharing agent | If the rule did not load, the command fails here with nothing enabled and nothing listening |
| Restart `system/com.apple.screensharing`, and read the new listener's start time back | `kickstart -restart -agent` cycles *ARDAgent*, not the process that authenticates on 5900 — measured, the listener's pid was unchanged across one |

`--disable` turns the agent off and deliberately **leaves the `pf` rule
loaded**: it blocks a port nothing is listening on, so it costs nothing, and it
closes the window for any later run that fails between the two steps.

**The credential.** One password is generated on first use and stored in
`~/.nyxGPT/secrets/cloud-mac-vnc-password` (mode 0600), the same place every
other ops-managed secret on your machine lives. It is never prompted for, never
printed unless you ask with `--show-password`, never in any `ssh` argv or shell
history (the configuration script travels on the connection's stdin), and never
in the `--json` payload or the HTTP API. `--rotate-password` replaces it.

That one password is set as **both** the login account's password (with
`dscl . -passwd`, verified with `dscl . -authonly` in the same step) and the
Mac's legacy VNC password. Both, because the two clients authenticate against
different things: Apple's own Screen Sharing.app — the client macOS hands
`vnc://...` to — offers security types 30 and 33 first and prefers them, and
both check a real **account** password, while a third-party VNC client takes
the legacy VNC password. A VNC-only credential is therefore one Apple's client
structurally cannot use, which is how `open vnc://localhost:5901` came back
"`ec2-user` and password rejected" in #4121's acceptance round. Sign in as the
deploy's SSH user (`ec2-user` unless you changed it); the command prints the
account name next to the address.

`dscl . -passwd` and not `sysadminctl -resetPasswordFor`, which is the obvious
API and fails on an EC2 Mac with "Operation is not permitted without secure
token unlock". Setting an account password does not weaken anything here: 5900
stays loopback-only, the security group stays TCP 22 from your `/32`, and the
account is reachable only through your own authenticated SSH forward.

**Where it will refuse, and why.** Both refusals are scoped the way
`nyxgpt cloud allow-ip` is — to machines nyxGPT configured:

| Target | What happens |
| --- | --- |
| A Linux deployment | Refused: there is no screen to share. `nyxgpt cloud ops doctor` is how that box is inspected |
| A Mac you supplied with `--host` | Refused: that machine's security group is not nyxGPT's, so nyxGPT cannot know whether 5900 is exposed on it — and enabling a listener behind a firewall nobody checked is how the loopback-only guarantee gets traded away by accident |
| The Mac nyxGPT allocated | Works, with no flags: the address comes from the Dedicated Host record, and the SSH user and identity file from the deploy record |

`nyxgpt cloud status` reports whether the path is open, and so does the admin
Infrastructure page — both as *observation*, with the command named as text.
Neither opens it: per the
[Definition of Done](../CLAUDE.md#definition-of-done-owner-requirement-2026-07-08),
a UI served by (or alongside) the machine being operated is not where access to
that machine is driven from.

#### Teardown, and the deferred host release

`nyxgpt cloud destroy --yes` **terminates the Mac immediately** and defers only
the host release, because AWS will not accept one inside the 24-hour window. A
naive `terraform destroy` over the host would half-fail there — the trap that
leaves an operator billing for something they believe is gone. So:

1. The Mac instance, its VPC and its security group are destroyed now.
2. The Dedicated Host is **removed from Terraform's state, not released**, so
   the teardown cannot fail on it.
3. A **one-shot [EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html)
   schedule** is created for `allocation + 24h + 30 minutes`. It has
   `ActionAfterCompletion=DELETE`, so it removes itself after firing and
   leaves no orphan.
4. The schedule starts a **Step Functions state machine** that calls
   `ReleaseHosts` and posts the outcome to Slack.

The teardown prints the host id and its release timestamp, and a
host-release failure never blocks the rest of the destroy — the substrate, the
tunnel and the deploy record still come down, and the warning names what is
left.

**What you see in Slack.** The state machine posts to the channel in
`[monitoring] slack_channel` (see
[configuration.md](configuration.md#monitoring)) using the bot token already
in `config.ini`, carried by an EventBridge **Connection** — no AWS Chatbot, no
new Slack app and no Lambda. Success reads *"nyxGPT released EC2 Mac Dedicated
Host h-… . It has stopped billing."*; failure is a `:rotating_light:` that
says the host is **still billing** and points back at `nyxgpt cloud destroy`.

Two details that would otherwise make the report a lie, and are handled:

- **Slack returns HTTP 200 on failure.** `invalid_auth` and
  `channel_not_found` come back `200` with `"ok": false`, so the state machine
  branches on `$.ResponseBody.ok` rather than on the status code.
- **`ReleaseHosts` returns 200 on failure too.** A host still being *scrubbed*
  after its instance was terminated comes back in `Unsuccessful`, not as an
  exception — so a `Retry` block would never fire. The state machine waits and
  re-attempts on a counter instead (up to four hours), and the 30-minute
  buffer on the fire time keeps the first attempt out of the obviously-too-
  early window.

**Watching it.** `nyxgpt cloud status` shows the host until it is gone — id,
region and AZ, when it was allocated, when it becomes releasable, whether the
release is scheduled, when AWS last confirmed the host exists, and what Cost
Explorer says it has cost. It shows it *after* the deployment is destroyed too,
which is the whole point: that is the state where the host is the only thing
still costing money.

```
EC2 Mac Dedicated Host (still billing -- confirmed at AWS in nyxgpt (066835328281) at 2026-08-23T06:00:00+00:00)
  Host              h-0abc1234 (mac2.metal)
  Location          us-east-1 / us-east-1a
  Allocated         2026-08-22T18:00:00+00:00
  Releasable at     2026-08-23T18:30:00+00:00 (AWS's 24-hour minimum)
  Confirmed at AWS  2026-08-23T06:00:00+00:00
  Release           scheduled -- a one-shot AWS schedule releases it and reports the outcome to Slack
  Spend             USD 15.60 from AWS Cost Explorer through 2026-08-23 (as of 2026-08-23T06:00:00+00:00)
```

The same block appears on the admin dashboard's Infrastructure page, and on
both surfaces it is *observed*, never driven — the release is already
scheduled in AWS and there is nothing for a page to press.

**Every row is AWS's answer or is labelled as not being one (#4136, #4181).**
`nyxgpt cloud status` makes one `DescribeHosts` call and (at most hourly, since
Cost Explorer bills per request) one `GetCostAndUsage`:

- The heading says **still billing** only when the block is *usable*: AWS
  confirmed the host in this run, **under the credentials this run resolved**,
  and the record does not contradict itself. Anything else prints the reason —
  nobody asked, AWS could not be asked (and why), the last answer came from
  another account, or the record's own fields disagree — followed by a line
  saying that every row below is what this machine recorded rather than what
  AWS reports.
- **The account is part of what an answer means (#4181).** An AWS account
  reports every resource it does not own as absent, so `InvalidHostID.NotFound`
  from the wrong account is indistinguishable from a release. The account each
  confirmation was obtained in is recorded with it, and an answer from any
  other account is not treated as a confirmation. `--profile` / `--region` tell
  this command which account to ask.
- **Spend is Cost Explorer's figure**, not `rate × elapsed`. The old
  calculation could not stop counting when the charges did: it read $48.44 for
  a host AWS had billed $12.02 for and had not charged for in two days. When
  AWS has no figure yet the row says so — naming the reason AWS could not be
  asked, rather than the remedy for a different reason — and offers the local
  number explicitly as an *estimate*.
- A host AWS reports as **released** does not get a weaker row — its record
  leaves `state.json` entirely, so there is nothing left to describe.
- `INCOHERENT` rows appear when the record contradicts itself (a release
  scheduled before its host was allocated, a release window that is not
  allocation + 24h30m, a scheduled release with no schedule). Those are
  detectable with no API call, and they mean the fields came from more than one
  run, so none of them can be read together — **including** by the rows that
  would otherwise draw a conclusion. A confirmation proves the host exists; it
  does not make a block assembled from two runs describe that host, so an
  incoherent record gets no billing claim and no release verdict however
  recently AWS answered.
- **This command changes nothing (#4181).** It used to reach `terraform
  destroy` on the EC2 Mac release-schedule stack, through the reconcile that
  clears a released host — a read-only command mutating the account it was
  reporting on. Correcting the local record still happens, because that only
  ever withdraws a claim; tearing the finished schedule stack down belongs to
  `nyxgpt cloud destroy` and `nyxgpt cloud deploy`, and `cloud status` says so
  when one is left behind.

Once the fire time passes, the row says the schedule **has fired** rather than
that the host is released: nothing on your machine watched it, so claiming the
charge has stopped would be an assertion nobody checked. Slack has the real
answer. `nyxgpt cloud status`, `nyxgpt cloud deploy --os macos` and
`nyxgpt cloud destroy --yes` each ask AWS whether the host is really gone and
clear the block if it is — and keep it if the question could not be asked,
because a record deleted on the strength of expired credentials would hide a
resource that is still billing.

The one thing no CI job can run is a real `mac*.metal` instance — GitHub
Actions has no macOS EC2 runner and Apple's licensing does not permit macOS in
a container (see [live-verification-ci.md](live-verification-ci.md)). What
*is* executed: [`cloud-target-os-smoke.yml`](../.github/workflows/cloud-target-os-smoke.yml)
runs the installed `nyxgpt cloud deploy` against a real sshd and asserts the
macOS bootstrap is what arrives, elevated, and that the Linux one still
arrives for a Linux plan; [`macos-brew-smoke.yml`](../.github/workflows/macos-brew-smoke.yml)
installs the same formulas from the same remote tap on a real macOS runner;
and [`terraform-aws-validate.yml`](../.github/workflows/terraform-aws-validate.yml)
validates the Dedicated Host and deferred-release root modules on every change
to them. Neither the allocation nor the deferred release can be executed in CI
— there is no EC2 Mac hardware and no way to make AWS's 24-hour clock pass —
which is the named exception in
[live-verification-ci.md](live-verification-ci.md), not a gap.

### `nyxgpt cloud status` — where is my instance? (#3813)

The deploy ends by pointing at this command, because everything it printed
scrolls away and the public IP is not something to recover from a terminal's
scrollback:

```bash
nyxgpt cloud status            # the operator summary (default)
nyxgpt cloud status --json     # the machine-readable payload
nyxgpt cloud status --no-probe # don't health-check through an open tunnel,
                               # and don't ask AWS about the Dedicated Host
nyxgpt cloud status --profile nyxgpt   # ask a named AWS account (#4181)
```

The deploy, substrate and tunnel facts come from recorded state alone — no AWS
call, no connection to the instance — so the command is safe to run at any
time and still answers when your AWS credentials have expired. **The EC2 Mac
Dedicated Host block is the one exception (#4136):** when a host is recorded,
the command asks AWS about it (one `DescribeHosts`, plus an hourly-cached
`GetCostAndUsage`) because a host id is a claim about money, and the row is
labelled *recorded here, not confirmed at AWS* rather than reported as current
when the question cannot be asked — see *Every row is AWS's answer or is
labelled as not being one* above. The recorded Linux instance is asked about
the same way, with one `DescribeInstances` (#4181): the sentence "an instance
exists and is being billed" is the same claim whichever substrate it is made
about. `--no-probe` suppresses every network call, which is what makes it the
poll-safe form; `--profile` / `--region` say which account to ask, because an
account that does not own a resource reports it as absent. The summary carries the installed release, the
instance id and type, the region, the **AWS account** the deployment was made
in (the profile name and the account id it resolved to, #4186), the **SSH key
pair** it was given, the public IP, the security group's single
ingress rule, the enabled observability profiles, the tunnel's state, a
health verdict, the localhost URLs and, most importantly, the **connection
target**: the SSH user and identity file the deploy actually used, alongside
the host. `host` on its own is not an address you can reach.

The AWS account row is there because it was the one provisioning input no
surface ever reported: an operator with two accounts could not tell which one
held their instance without an STS call of their own. It reads
`not recorded here` — never a blank — when asked from a machine that did not
run the deploy, which is a different claim from "no profile".

On an EC2 Mac deployment the summary adds a **`Screen path`** row — open,
enabled-but-closed, or never set up — and names
[`nyxgpt cloud screen`](#reaching-the-macs-screen-4121). The row is absent on a
Linux deployment, which has no screen to report on.

For support conversations it also prints, under a `Diagnostics` heading, the
raw `ssh` invocation that `nyxgpt cloud tunnel` executes on your behalf.
That is shown so you can see what is running when a tunnel misbehaves — the
command to *run* is always the wrapped one.

Where the answer comes from follows the same rule as the substrate (see
[Which machine is answering](#which-machine-is-answering-3804)): the deploy
record on the workstation that deployed, the instance itself when the command
runs there, the cloud-deploy record in the cluster when it runs in an api Pod
of a `--kubernetes` deployment (#4138), and otherwise **unknown** — which is
not the same as nothing being deployed. The connection target is reportable only in the first case;
on the instance the SSH user and key are the workstation's, and the command
says so rather than printing a blank.

#### A deploy that did not finish (#3993)

Between DEPLOYED and UNKNOWN there are two more answers, and both are real
states an operator lands in:

| Verdict | What it means |
| --- | --- |
| `NOT COMPLETED` | A deploy started on this machine and did not finish. The summary names the phase it reached (`start`, `infra`, `ssh`, `ship`, `provision`, `tunnel`, `health`) and the error that stopped it. A failure at `start` or `infra` predates the substrate, so the summary says nothing was provisioned and does not offer `cloud destroy` — there would be nothing for it to tear down (#4007). |
| `SUBSTRATE ONLY` | An instance is provisioned — `~/.nyxGPT/cloud/state.json` on this machine names it — but no deploy has been recorded against it. |

Neither reports `deployed: true`, and both name the wrapped commands that
move it forward: re-run `nyxgpt cloud deploy` (idempotent — it reconciles from
where it stopped), or `nyxgpt cloud allow-ip` if your public IP has changed
since.

**A declined consent is neither** (#4181). Typing anything but `allocate` at
the EC2 Mac disclosure records the attempt as `declined` — its own outcome,
not a failure — so the summary does not say a deploy "did not finish" and
does not prescribe re-running it. It still says what happened, because that is
the answer to "what did that command do": *DECLINED at the `…` phase — the
disclosure was shown and not accepted, so nothing was created and nothing is
billed*. The command still exits non-zero; declining is not success.

What they say about billing depends on what was actually confirmed, never on
the verdict alone (#4007, #4181). Three answers, not two:

- **AWS confirmed the resource in this run** — the summary says so plainly,
  *it is being billed*, and offers `nyxgpt cloud destroy --yes` to stop paying
  for it.
- **Something is recorded here and nothing confirmed it** — the summary says
  exactly that, shows the recorded ids labelled *recorded here, NOT confirmed
  at AWS*, and names **the reason nothing confirmed them** (no credentials, no
  boto3, an answer from the wrong account, a record that contradicts itself)
  together with the remedy that matches that reason. It does not prescribe
  `nyxgpt cloud status` — it *is* `nyxgpt cloud status`, and telling an
  operator to re-run the command they just ran is finding 6 of #4181. This is
  the case #4181 added: the summary used to assert billing from the presence
  of an id, and did so for an operator whose AWS account held no instances at
  all.
- **Nothing was provisioned** — a `NOT COMPLETED` that stopped at `start` or
  `infra`, or a declined consent. No destroy is offered: the deploy died
  before the substrate, so there is nothing to tear down and `cloud destroy`
  would only answer "nothing to destroy".

This exists because a failed provision used to report `UNKNOWN from this
machine` — sending an operator to look for another workstation while a live,
billing EC2 instance ran and their own `state.json` named its instance id.
The record behind it, `deploy-attempt.json`, is written **before** anything is
provisioned and closed out on every exit path, so a deploy that dies in the
middle (or takes the laptop's lid with it) still leaves something to read.

A completed deployment plus a *later* failed deploy is its own state and is
reported as such: the summary stays `DEPLOYED` — the stack really is
installed — and adds a `Last deploy attempt` row naming the version and phase
that failed. Without it, an instance running the previous release looks
identical to one running the release you thought you just shipped.

### `nyxgpt cloud ops` — inspecting the instance (#3813)

What the instance is running, without a hand-rolled `ssh` and a raw
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

`nyxgpt ops status` reports whatever tier the instance has — systemd `--user`
units plus the Cassandra and observability containers on Linux, `brew
services` on an EC2 Mac, which [has no Docker daemon](#docker-on-the-instance)
to report on. The instance's own output is streamed back unchanged, so the
report names what it found rather than the shape one substrate happens to
have.

#### Finding the instance on either target OS (#4161)

No flag is needed on a macOS deployment either. `~/.nyxGPT/cloud/state.json`
holds [one block per substrate](#statejson-holds-current-state-and-nothing-else-4136),
and the two blocks use different names for the same fact: a Linux deploy's
Terraform outputs land in `public_ip`/`instance_id`/`region`, while an EC2 Mac
— which never applies that substrate — records `mac_public_ip`,
`mac_instance_id` and `mac_region`. The resolver every instance-reaching
command shares (`resolve_target`, used by `cloud ops`, `cloud tunnel`,
`cloud credentials`, `cloud canary` and `cloud smoke`) reads **both**, so:

| What is on record | Where the address and its ids come from |
| --- | --- |
| A Linux deploy | `state.json`'s bare block (`public_ip`, `instance_id`, …) |
| A macOS deploy (`deploy.json` says `os_family: macos`) | `state.json`'s `mac_` block, then `deploy.json` `host` |
| An EC2 Mac allocated by a deploy that did not finish | `state.json`'s `mac_` block |
| A box supplied with `--host` (no substrate record of it) | `deploy.json` `host` |
| `--host` on this invocation | the flag, always |

It used to read `public_ip` alone, so every one of those commands answered
`No provisioned instance found` on a Mac whose current address was sitting in
that very file under the other name — the whole wrapped-ops surface
unreachable without re-typing `--host` on each command. The values were never
stale; the reader was looking up the wrong word. (Stale values are a
different defect, fixed in #4136.)

Two rules keep that lookup from reaching the *wrong* machine:

- **Each row is taken whole.** The address and the ids come from one record or
  from none, so a host is never reported wearing another machine's instance
  id, region or security group. A `--host` box nyxGPT did not provision
  therefore shows no security group — it has none of ours. When `--host` names
  a machine that is not the one on record, the recorded ids are dropped for the
  same reason.
- **A deploy in flight names its own target OS, and that wins.** `cloud
  deploy` resolves through this resolver immediately after applying the
  substrate, when `deploy.json` still describes the *previous* deploy. A plain
  `nyxgpt cloud deploy` after a macOS one is a Linux deploy (the family comes
  from the instance type unless `--host` names the recorded box), so the Mac's
  `os_family: macos` is stale — and reading the family off it would have sent
  the Linux install over SSH onto a working EC2 Mac while the instance that run
  had just paid for sat empty. If the pinned substrate has no address to give
  (an apply whose Terraform outputs were unreadable, #3993), the deploy
  refuses rather than crossing to the other family's record.

#### If you SSH in yourself (#3993)

`nyxgpt` is on the PATH of any login shell on the instance. The Linux
bootstraps install a `/etc/profile.d/nyxgpt.sh` drop-in that prepends the CLI's
`bin` directory, and on macOS the `nyxgpt-api` keg symlinks the CLI into
Homebrew's own `bin` — so a plain `ssh` session can run `nyxgpt ops doctor`,
`nyxgpt ops logs api` and the rest directly:

| How the instance was provisioned | Where the binary lives |
| --- | --- |
| `nyxgpt cloud deploy` (SSH-driven, Linux) | `~/.nyxGPT/venv/bin/nyxgpt` |
| `nyxgpt cloud user-data --os linux` (first-boot bootstrap) | `~/.nyxGPT/opt/nyxgpt-cli/bin/nyxgpt` |
| `--os macos` | on the PATH already — the `nyxgpt-api` keg symlinks it into Homebrew's `bin` |

The wrapped commands above search that same list **on the instance** rather
than assuming any one row of it (#4150). They used to run a hardcoded
`~/.nyxGPT/venv/bin/nyxgpt`, so on an EC2 Mac — where that path does not and
should not exist — every remote inspection died with exit 127 and told the
operator to run the deploy that had just succeeded.

The wrapped inspection commands above remain the recommended route (they need
no SSH session at all); this is for the case where you are already on the box
diagnosing something, and used to get `nyxgpt: command not found` from every
command the docs told you to run.

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

On an **EC2 Mac** target there is one more thing worth forwarding, and it is
forwarded the same way: see
[Reaching the Mac's screen](#reaching-the-macs-screen-4121). It is a separate
command (`nyxgpt cloud screen`) rather than a port on this list, because it
also has to enable a service on the Mac and carries a credential of its own.

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

### Kubernetes on the instance (#3956)

```bash
nyxgpt cloud deploy --kubernetes
```

Runs the stack on a **single-node k3s cluster** on the instance instead of
natively, applying the same `k8s/*.yaml` manifests a local Kubernetes install
uses. This is the owner-approved decision in
[`DECISION_AWS_COMPUTE_SUBSTRATE.md`](../product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md)
— EC2 single-box with those manifests layered on k3s, rather than a managed
EKS control plane — and the capability it exists for is
[canary rollout](kubernetes.md#canary-deployment), which needs a cluster to
weight traffic in.

It is not a second deployment path. The deploy installs k3s, writes a
kubeconfig the login user owns, and then runs
`nyxgpt ops install --kubernetes --local` **on the instance** — the same
command you would run on a workstation, taking the same bring-your-own-cluster
branch it takes against any reachable cluster. Steps 1, 2, 5 and 6 of the
deploy above are unchanged; step 3 installs k3s in place of the host Ollama
and Node toolchain (the cluster runs Ollama itself, and the web image is built
by Docker rather than by `npm` on the host).

Step 4 has no Kubernetes form: chat sessions live in the in-cluster Cassandra
because `k8s/configmap.yaml` says so, and the Pods read that rather than the
host's `config.ini`. `--session-backend file --kubernetes` is therefore
**refused** rather than accepted and ignored — including when the `file` value
was carried forward from an earlier native deploy of the same instance, which
is exactly the case where the flag would otherwise change meaning underneath
you.

What the access model gets, beyond the single port-22 rule that is unchanged:

- k3s's API server binds the instance's **private** address, not `0.0.0.0`.
- Traefik (k3s's default ingress controller, which would bind host ports
  80/443) and `servicelb` (its `Service: LoadBalancer` implementation) are
  both disabled. Nothing in `k8s/*.yaml` asks for either.
- The cluster's Services are ClusterIP-only, so the deploy installs an
  **access bridge** — systemd `--user` services running `nyxgpt ops
  port-forward` — to hold `127.0.0.1:8000`/`127.0.0.1:3000` on the instance
  for [`nyxgpt cloud tunnel`](#reaching-it-nyxgpt-cloud-tunnel) to forward to.
  They restart automatically, which matters during a rollout: replacing a Pod
  ends a port-forward.

Canary rollout against the deployment:

```bash
nyxgpt cloud canary status
nyxgpt cloud canary start --weight 10
nyxgpt cloud canary evaluate
nyxgpt cloud canary promote --step 25
nyxgpt cloud canary rollback
```

These run the instance's own `nyxgpt canary` over the same wrapped SSH path
[`nyxgpt cloud ops`](#nyxgpt-cloud-ops--inspecting-the-instance-3813) uses,
because the cluster's API server is reachable from the instance and from
nowhere else. `--component api|web` selects the pair. There is no
`nyxgpt cloud canary deploy`: `nyxgpt canary deploy` builds an image from a
source checkout and the instance has none by construction — roll a new release
out with `nyxgpt cloud deploy --version <release>`, which is idempotent.

`canary status` reports the version each track is serving, read off the Pod's
own image tag — `artifact-<version>` for a published release, `dev-<version>`
for a `--dev` deploy (see [Image
tags](kubernetes.md#image-tags-one-namespace-per-build-path)). Before #3956 all
four build paths shared one mutable `:local` tag, so an instance running
3.0.0rc14 reported its images as `local` and there was no way to tell from the
deployment which build was serving.

**No `KUBECONFIG` to export.** Every kubectl call nyxGPT makes names the
kubeconfig kubectl's own default resolution would have used, so the wrapped
commands above, `nyxgpt cloud ops doctor` and the instance's self-heal watchdog
all find the cluster in a fresh SSH session with no environment set up. That is
not cosmetic on this substrate: `/usr/local/bin/kubectl` on a k3s node is a
symlink to the `k3s` binary, whose shim defaults `KUBECONFIG` to the root-only
`/etc/rancher/k3s/k3s.yaml` — so the user-owned `~/.kube/config` the deploy
writes was never read, and `nyxgpt cloud canary status` answered *"this process
is currently running in native mode"* on a live cluster (owner acceptance,
2026-08-26). A probe that cannot reach an API server now reports that it could
not, with kubectl's own error, rather than falling back to a confident
`native`.

The substrate is recorded with the deployment, so a later bare `nyxgpt cloud
deploy` reconciles the same Kubernetes deployment rather than installing a
native stack beside it and fighting it for ports 8000/3000. `--no-kubernetes`
moves a deployment back to the native substrate, and `--kubernetes` on a
native deployment moves it forward: each provisioning script retires the
substrate it replaces before installing its own — the k3s deploy runs the
wrapped `nyxgpt ops down` on the instance first, the native deploy stops the
access bridge and runs k3s's own uninstaller. Without that the two stacks
would both be running and every health probe would be answered by the one the
operator just asked to leave.

**A switch moves the instance; it does not migrate it.** Each substrate keeps
its own Cassandra, so chat sessions do not follow a switch: the native
Cassandra's volume survives (`ops down` preserves volumes, so switching back
finds it), but the cluster's `local-path` volumes go with `k3s-uninstall.sh`.
Move the data yourself before switching if you need it, or keep the two on
separate instances.
[`nyxgpt cloud status`](#nyxgpt-cloud-status--where-is-my-instance-3813) and
the dashboard's Infrastructure page report which one is running.

Sizing: the cluster carries the whole stack, so the node needs what a local
Kubernetes install needs — see
[Node capacity](kubernetes.md#node-capacity-what-the-stack-reserves) and pass
`--instance-type` accordingly. A node too small for the stack is refused by
the install's capacity preflight before anything is built, rather than leaving
a Pod Pending forever.

### Tearing it down

```bash
nyxgpt cloud destroy --yes
```

Closes the tunnel, then destroys the substrate. `--yes` is required: the
instance and its root volume go, and anything living only on that box —
models, Cassandra data, logs — goes with them.

That includes a `--kubernetes` deployment's cluster, in full: the k3s control
plane, its containerd image store and its `local-path` volumes all live on the
instance's root volume, so terminating the instance *is* the cluster teardown.
There is no separate cluster to remove first, and the deploy record — with the
substrate it pins — goes with it.

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
  from the operator's machine. With
  [`--kubernetes`](#kubernetes-on-the-instance-3956) the same holds one layer
  down: the manifests come from the installed package (`nyxgpt.resources.k8s`,
  synced to `~/.nyxGPT/k8s`) and the api/web images are built on the instance
  from the **published** `nyxgpt-api`/`nyxgpt-web` tarballs. This is the same
  sequence the
  `artifact-install-smoke` job in `.github/workflows/release-artifacts.yml`
  proves on a checkout-free runner for every release;
  `tests/unit/test_cloud_deploy.py` asserts the generated script contains no
  source-control fetch at all.

The instance therefore runs a *published* release, not your working tree. If
you want a version other than your CLI's, name it with `--version`.

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
  it as well — a **Build source** row, in the human form and under `--json`
  alike, and on the dashboard's cloud card — so an instance running a tree is
  never mistaken for one running a release.
- **The web UI runs Next's dev server**, as it does under `nyxgpt up --dev` —
  slower first paint, no production build.
- **It ships the tree, not the machine.** Anything your local stack has that
  the repository does not (an untracked config, a hand-installed dependency)
  is not on the instance.
- **Linux targets only.** `--dev --os macos` is refused, before the substrate
  is applied: the [EC2 Mac bootstrap](#ec2-mac-targets) installs published
  Homebrew formulas from the remote tap and has no working-tree source, so
  ignoring the flag would hand you a published release while you believed you
  were testing your tree.

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

The *services* the deploy leaves behind are the third case, and the one that
reopened #3812. `systemd --user` starts them from a manager whose credentials
predate the group change, so the API process could not query Docker at all and
the dashboard reported every observability component as undetermined —
honestly, but permanently. Every Docker call the API process makes now retries
through `sg docker` for the same reason the deploy script does — the watchdog's
Compose survey and, since #4022, the container reads behind the Infrastructure
page's Native and Terraform cards, which until then rendered the same denial as
a flat `absent` and reported a running Cassandra as gone. Both go through one
shared implementation; see [The `docker` group
hop](self-healing.md#the-docker-group-hop-making-the-probe-run-not-just-report).

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
| Inspect what the instance is running | `nyxgpt cloud ops status` |
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
| In an api **Pod** of a `--kubernetes` deployment (#4138) | The `nyxgpt-cloud-deploy` ConfigMap in the `nyxgpt` namespace | The same substrate facts, recorded there by the `nyxgpt ops install --kubernetes` the deploy ran *on* the instance. The deployment is still read first-hand — a Pod of it is answering — and the card names the record it got the instance from |
| On the workstation that provisioned it | The Terraform outputs in `~/.nyxGPT/cloud/state.json` | The substrate that machine created, plus its deploy record, tunnel state and history |
| None of those | — | **Unknown.** Not "not provisioned": nothing on that machine has checked |

This is why the panel exists in this shape. Deriving everything from Terraform
state — which lives on the operator's workstation — made a dashboard served
*from* the instance report "not provisioned" with every field blank, on a
machine Terraform had created minutes earlier (owner observation, rc12).
Terraform state and the tunnel are likewise reported as *not on this machine*
when the page is served from the instance, rather than as a local file that
does not exist there.

The third row is the same defect one substrate down (#4138, owner observation
2026-10-03): on a `--kubernetes` cloud deployment the dashboard is served by an
api **Pod**, which reaches neither IMDS (`169.254.169.254` is link-local and
not routed into the Pod network) nor the host's `~/.nyxGPT/cloud` — so both
cards read UNKNOWN while `nyxgpt cloud status` on that same instance printed
the deployment in full. The answer is the one #3988 already used for the
install mode: the install **records the facts in the cluster** and the API
serves them from there. The Pod does not probe the host and no host path is
mounted into it. Off EC2 nothing is recorded, so a local `kind` cluster's page
still says unknown; off-cluster nothing is read, so a workstation spends no
`kubectl` on a record about a machine it is not. See [The cloud deployment
underneath the cluster](kubernetes.md#the-cloud-deployment-underneath-the-cluster-4138).

The page and the CLI still call the same `nyxgpt.cloud_deploy` and
`nyxgpt.cloud_infra` functions, and the commands it displays come from the
backend's own `LIFECYCLE_COMMANDS`, so the two can never drift apart.

### Where deploy state lives

| Path | Contents |
| --- | --- |
| `~/.nyxGPT/cloud/deploy.json` | What the last successful deploy installed: version, host, instance, region, enabled profiles |
| `~/.nyxGPT/cloud/deploy-attempt.json` | The last deploy this machine *started*, whatever became of it — status (`running`/`failed`/`succeeded`), the phase it reached, target and version (#3993). Written before anything is provisioned, so a deploy that dies partway still leaves a record |
| `~/.nyxGPT/cloud/tunnel.json` | The backgrounded tunnel's pid and forwarded profiles, so `--stop`/`--status` (and the dashboard) find a tunnel another process started |
| `~/.nyxGPT/cloud/history.jsonl` | One line per deploy, teardown and [smoke run](#nyxgpt-cloud-smoke--the-end-to-end-cloud-test-p6-17-3515) — timestamp, action, outcome, version, instance, and what went wrong on a failure |

`deploy-attempt.json` is deliberately a separate file from `deploy.json`, not
an early write into it: `deploy.json` is the record of a deployment that
*exists*, and putting a half-finished attempt there would make `nyxgpt cloud
status` report DEPLOYED for a stack that was never installed. A teardown
deletes both — once the substrate is gone, "a deploy stopped at `provision`"
describes an instance that no longer does.

All of them are read-only inputs to `nyxgpt cloud status`, which answers the
deploy and tunnel questions from these files alone — no AWS call, no
connection to the instance — so it still answers when your AWS credentials
have expired. The exceptions are the two blocks that carry a claim about
money: the EC2 Mac Dedicated Host (#4136) and the recorded Linux instance
(#4181). `status` verifies each against AWS whenever one is recorded, in the
account the shared order resolves (`--profile` names it explicitly), and
reports the record as *recorded here, not confirmed at AWS* rather than as
current when the question cannot be asked. `--no-probe` turns both calls off
along with the tunnel health check, and is the form to poll. None of them
changes anything in AWS: `status` observes, and the cleanup it used to do
(`terraform destroy` on a finished release-schedule stack) belongs to
`cloud destroy` and `cloud deploy`.

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
| `did not accept SSH within 300s` (a Linux target) | Usually a stale security-group rule — run `nyxgpt cloud allow-ip` (see [Lockout recovery](#lockout-recovery)). Also possible on a very slow first boot: retry with `--ssh-timeout 600`. |
| `did not accept SSH within 2700s` (an EC2 Mac) | Almost always a first boot that is still running — that is what the 45-minute wait is for, and the message says so before it names the IP remedy. Raise it with `--ssh-timeout` and re-run: the deploy reconciles the Mac you already have, so there is no second host and no second 24-hour minimum. `nyxgpt cloud status` names the Dedicated Host, its release time and what it has cost so far. |
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
| EC2 instance | one `m5.xlarge` (4 vCPU / 16 GiB, per [`DECISION_AWS_COMPUTE_SUBSTRATE.md`](../product_management/DECISION_AWS_COMPUTE_SUBSTRATE.md)), IMDSv2 required, encrypted gp3 root volume, Elastic IP so the address survives stop/start |

There is no EKS cluster, node group, load balancer, or NAT gateway: a single
owner reaching a single private deployment needs none of them, and an ALB
would contradict the access model outright.

Resources are named with the same `nyxgpt-tf-*` convention as the local
Docker stack (`nyxgpt-tf-vpc`, `nyxgpt-tf-instance-sg`, `nyxgpt-tf-instance`,
…), overridable via `name_prefix`.

### Commands

```bash
# See what would be created; creates nothing.
nyxgpt cloud infra plan --region us-east-1

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
need the ones that change. `--ssh-public-key` (a `.pub` file to register as a
new key pair) and `--ssh-key-name` (an EC2 key pair that already exists in the
region) are mutually exclusive, and **neither is required**: with neither
given the command asks, offering the best candidate as the default — see
[Which account, and which SSH key](#which-account-and-which-ssh-key-4186).
SSH is still the only way in, so a run that has no candidate *and* cannot ask
is refused before anything is created.

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
| `~/.nyxGPT/cloud/state.json` | **what is deployed now** — the ids `allow-ip` and `cloud deploy` read |
| `~/.nyxGPT/cloud/state-archive.jsonl` | superseded records, newest last, capped. Diagnosis only; nothing reads it to make a decision |

#### `state.json` holds current state and nothing else (#4136)

The file is a set of **per-substrate blocks** in one flat namespace: the Linux
substrate owns `region`, `vpc_id`, `security_group_id`, `instance_id`,
`instance_type`, `public_ip`, `private_ip`, `ssh_key_name`; the EC2 Mac owns the
`mac_*` keys. Three rules, enforced in one place
(`src/nyxgpt/cloud_record.py`) rather than in each command:

- **A block is replaced whole, never merged into.** A write decides *every* key
  the substrate owns; one it does not mention is **dropped**. "This substrate
  has no such id" is an honest answer, and a stale id is worse than a missing
  one because every consumer treats it as current.
- **A superseded block leaves this file.** It is appended to
  `state-archive.jsonl` — a different file under a different name — so nothing
  reading `state.json` ever has to ask which run a value came from. Keys
  belonging to no substrate are archived and dropped too: nothing can refresh
  them, so they can only get staler.
- **The write is atomic** (temp file plus `os.replace`), so a killed command
  cannot leave half a record behind for the next one to read as current.

An in-place field update is the one exception, and it is gated: it must name the
resource it believes the block describes, and the write is refused if the record
now names a different one. Recording "the release is scheduled" against whatever
host happened to be in the file is how a `mac_release_scheduled_at` **78 seconds
earlier** than the `mac_allocated_at` of the host it described got written.

Why all of this: the file was merged field by field for three releases running.
A deploy that provisioned a new substrate wrote the fields it produced and left
the rest naming the previous one — a new instance stapled to a released
Dedicated Host, a combination that never existed. #3993 saw it send
`nyxgpt cloud allow-ip` at a destroyed security group while the operator was
locked out; #4136 saw it skip a priced disclosure and start an unannounced
24-hour billing minimum.

The one thing that does **not** rewrite it is an apply whose outputs could
not be read at all. `terraform output` returning nothing means "cannot
determine", not "there is nothing" — blanking every recorded id on a failed
*read* would be a worse falsehood than the stale one. That case leaves the
file untouched and prints a warning naming it, because the ids in it are then
an earlier substrate's and must not be trusted until a re-run refreshes them.

### Credentials and cost

Provisioning uses boto3-style credential resolution via Terraform's AWS
provider — the profile from `nyxgpt cloud credentials-setup` (below), or
`--profile`/`AWS_PROFILE`. **This command creates billable resources**: an
`m5.xlarge` on-demand is roughly **$140/month** (~$0.192/hr) plus EBS and the
Elastic IP; `nyxgpt cloud infra destroy --yes` removes all of it. That is
double the old `m5.large` default, and the doubling is deliberate — see
[Instance sizing](#instance-sizing).

### Instance sizing

The default is `m5.xlarge` (4 vCPU / 16 GiB). The previous `m5.large` default
(2 vCPU / 8 GiB) could not hold what a deploy puts on the box: Cassandra's
untuned JVM alone takes ~4.3 GiB, and with the web tier, Ollama and the
observability stack the instance sat at 7.2 of 7.6 GiB minutes after boot with
no swap. Ordinary use then froze it — a web route compile, a page navigation
or a document ingest each drove it into reclaim thrash, invisible to EC2
status checks and with no OOM kill to diagnose from (#3992).

**An existing deployment is not resized by upgrading nyxGPT.** Every
`nyxgpt cloud infra plan`/`apply` records the size it used in
`~/.nyxGPT/cloud/infra.json`, and a remembered value always beats the built-in
default, so a substrate provisioned on `m5.large` stays on `m5.large` until
you say otherwise. To take the new size on an existing instance, ask for it:

```bash
nyxgpt cloud infra apply --instance-type m5.xlarge
```

That stops the instance, resizes it and starts it again — the root volume and
the Elastic IP survive, but the stack is down for the restart, so run it when
a short outage is acceptable. The new value is remembered from then on.

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

`bootstrap` and `migrate` authenticate to S3 and DynamoDB, so they resolve the
AWS account through the same resolver every other `nyxgpt cloud` command uses
and are subject to the same rules as
[Which account, and which SSH key](#which-account-and-which-ssh-key-4186):
without `--profile` you are asked, with the resolved value as the default, and
`--yes` (or no terminal) takes it and prints it. The one difference is that
`backend.json`'s own saved profile and region come first, above `infra.json` —
they describe the bucket and lock table this command is about to touch.

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
   otherwise `~/.nyxGPT/cloud/state.json`'s `security_group_id`, then its
   `mac_security_group_id` (written by `nyxgpt cloud deploy` — the second is
   what a `--os macos` deploy records, and reading only the first is why
   auto-discovery found nothing at all after one, #4136).
3. Revokes every existing port-22 ingress CIDR that doesn't match the new
   IP, and authorizes the new one -- unless it's already the only allowed
   source, in which case the command is a no-op (idempotent).
4. Prints the old and new source CIDR.

### Options

| Flag | Description |
| --- | --- |
| `--ip <addr>` | Use this IP or CIDR instead of auto-detecting the caller's current public IP. A bare address (no `/`) is scoped to `/32`; an explicit CIDR is kept as passed. `0.0.0.0/0` is always refused. |
| `--security-group-id <id>` | Security group to update. Defaults to `~/.nyxGPT/cloud/state.json`'s `security_group_id`, then its `mac_security_group_id` (#4136). |
| `--region <region>` | AWS region. Defaults to `~/.nyxGPT/cloud/state.json`'s `region`, then its `mac_region` (#4136), then the last `cloud infra apply`'s, then config.ini `[cloud] region`, then boto3's normal region resolution (`AWS_REGION`/`AWS_DEFAULT_REGION`/profile config). |
| `--profile <name>` | AWS profile to authenticate with. Defaults to the profile the last `nyxgpt cloud infra apply` used, then config.ini `[cloud] profile`, then `AWS_PROFILE`, then boto3's default chain (#3993). |

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

`allow-ip` resolves its AWS **profile** the same way the substrate commands
do, and then hands the rest to boto3's normal credential resolution
(`~/.aws/credentials`, SSO, an instance role, ...). It never collects or
stores AWS credentials itself. See "Guided AWS credentials setup" below for
how to get a profile in place.

The order (#3993), implemented once in `nyxgpt.cloud_identity` and shared by
every `nyxgpt cloud` command since #4186 — which also makes step 1 a *question
with this order's answer as its default* when there is a terminal, and prints
the account either way
([Which account, and which SSH key](#which-account-and-which-ssh-key-4186)):

1. `--profile <name>`
2. the profile the last `nyxgpt cloud infra apply` recorded
   (`~/.nyxGPT/cloud/infra.json`)
3. config.ini `[cloud] profile`
4. `AWS_PROFILE`
5. boto3's own default chain

**And the `terraform` subprocess gets the same answer (#4181).** Terraform
resolves credentials itself, from its provider block plus the ambient
environment, and it has never seen `--profile`. Two things close that gap, and
both are needed:

- the resolved profile and region are put into the environment of **every**
  `terraform` invocation, for every root module (`aws`, `mac`, `mac-release`) —
  a run that resolved *no* named profile clears an inherited `AWS_PROFILE`
  rather than letting the shell outrank its own resolution; and
- a **stored tfvars file an earlier run rendered** has its credential lines
  overwritten with this run's before Terraform reads it. Reusing those values
  for *what* a destroy operates on is right; reusing them for *whose
  credentials* do it is not, and the provider's
  `profile = var.aws_profile != "" ? var.aws_profile : null` means a stale one
  outranks the environment.

Until this, a `nyxgpt cloud destroy --profile nyxgpt` whose boto3 calls reached
the right account ran `terraform destroy` on the EC2 Mac release-schedule root
as the *default* account — against a tfvars file rendered days earlier with no
`aws_profile` line at all — and died with `AccessDenied` on
`nyxgpt-tf-mac-release`.

Steps 2 and 3 are the point. Until they existed, `allow-ip` built its client
with no profile at all, so a workstation whose **default** profile names a
different account queried that account — and got:

```
nyxgpt cloud allow-ip: Failed to describe security group sg-0fed18aaeb342c218:
An error occurred (InvalidGroup.NotFound) ... does not exist
```

for a security group that was attached to the running instance the whole
time. That is the worst message this command could produce: it is the
lockout-recovery tool, so it is read by an operator who cannot SSH in to
check, and "does not exist" reads as *your infrastructure is gone*. A
not-found now always names the account and profile the lookup actually used,
so a credential-resolution mistake can never be mistaken for a destroyed
substrate:

```
nyxgpt cloud allow-ip: Failed to describe security group sg-0fed18aaeb342c218:
An error occurred (InvalidGroup.NotFound) ... does not exist -- that lookup ran
against AWS account 210987654321, profile 'default'. If the group exists in a
different account, this is a credential-resolution problem and not a destroyed
substrate: set `[cloud] profile` in ~/.nyxGPT/config.ini (or pass
`nyxgpt cloud allow-ip --profile <name>`), then re-run.
```

Cloud **secrets** resolution (`[secrets] provider = ssm|secretsmanager`,
[below](#cloud-secrets-ssm--secrets-manager)) had the same gap and takes the
same profile: `[secrets] profile` when set, otherwise `[cloud] profile`.

---

## Which account, and which SSH key (#4186)

Every `nyxgpt cloud` command needs two answers before it can do anything: the
AWS identity to authenticate as, and the SSH key that will be the only way
into the instance afterwards. Both are resolved in **one** place —
`nyxgpt.cloud_identity` — so `deploy`, `infra`, `state`, `destroy`, `status`,
`tunnel`, `allow-ip`, `ops`, `credentials` and `canary` cannot disagree about
either one.

The order is the same for both, and it is the order above: **flag →
`~/.nyxGPT/cloud/infra.json` → `config.ini [cloud]` → environment**.

One command inserts a record of its own: `nyxgpt cloud state bootstrap`/
`migrate` consult `~/.nyxGPT/cloud/backend.json`'s saved profile and region
directly below their flags and above `infra.json`, because that record
describes the bucket and lock table they are about to touch. The four steps
below it are still the shared ones, not a copy — which is the fix for state
commands having authenticated through a chain that skipped
`config.ini [cloud]` entirely, i.e. exactly where
`nyxgpt cloud credentials-setup` writes the profile.

### You are asked, with a default

What changed is what happens when a flag is absent. Instead of choosing
silently (the account) or refusing (the SSH key), the command asks — and the
default is one keypress away:

```
$ nyxgpt cloud deploy
AWS profile (or - to use boto3's default credential chain) [nyxgpt (066835328281)]:

SSH key for the instance (SSH is the only way in):
  1) EC2 key pair 'nyxgpt-smoke-key' (already in this account; fingerprint matches ~/.ssh/id_rsa.pub)  (default)
  2) register ~/.ssh/id_ed25519.pub as a new EC2 key pair
Choice [1]:
Private key to authenticate with [~/.ssh/id_rsa]:
AWS account: nyxgpt (066835328281) | region: us-east-1 | SSH key: EC2 key pair 'nyxgpt-smoke-key' … | identity: ~/.ssh/id_rsa
```

The account prompt shows the profile **and the account id it resolves to**,
because the name alone is not an answer: two sensibly-named profiles can point
at any two accounts, and a run in the wrong one is how a security group that
plainly exists comes back `InvalidGroup.NotFound`. Type a different profile
name to use it instead, or `-` to use no named profile at all and let boto3's
own credential chain decide — which the prompt says, since Enter means "keep
the one shown".

The SSH candidates are offered in this order:

1. the key the last `cloud infra apply` recorded in `infra.json`;
2. an EC2 key pair in the chosen account and region **whose fingerprint
   matches one of your local public keys** — offered by name together with the
   file it matches, and with that file's private half as the identity to log
   in with;
3. a local public key to register as a new pair.

Step 2 is the one that was missing. An account can already hold a usable pair
— and finding it otherwise means comparing fingerprints by hand, which is
exactly what the 2026-10-09 acceptance round had to do. Both formats EC2
reports are matched: the base64 SHA-256 `ssh-keygen -l` prints, and the hex
MD5 of the DER public key that an *imported* RSA pair is fingerprinted with. A
pair EC2 **created** is fingerprinted from its private half, which no public
key can reproduce, so those cannot be matched and are not offered as matches.

### Scriptable, and it never hangs

The prompts need a real terminal on both stdin and stdout. Without one — CI, a
`nyxgpt cloud ops` invocation over SSH, the admin API — the resolved defaults
are used and **printed on one line**, so a scripted run still says which
account and key it used:

```
AWS account: nyxgpt (066835328281) | region: us-east-1 | SSH key: EC2 key pair 'nyxgpt-smoke-key' | identity: ~/.ssh/id_rsa
```

Two ways to ask for that explicitly:

| | Meaning |
| --- | --- |
| `--yes` | On `cloud infra plan`/`apply` and `cloud state bootstrap`/`migrate` it means only "do not ask". On `cloud infra destroy`, `cloud deploy`, `cloud destroy` and `cloud smoke` it keeps its existing confirmation meaning *and* takes the defaults |
| `NYXGPT_CLOUD_NONINTERACTIVE=1` | The same, for every `nyxgpt cloud` command — including the read-only ones that have no `--yes`. For a wrapper script that runs with a terminal attached but no human in front of it |

With no usable default, the run fails rather than waiting, and the message
names every missing input and the flag that supplies it:

```
nyxgpt cloud infra plan: This run cannot ask (no terminal, or --yes was given) and
these inputs have no usable default:
  - an SSH key for the instance (SSH is the only way in; this account has no key pair
    matching a local public key, and this machine has no public key to register):
    pass --ssh-key-name <existing-pair> or --ssh-public-key <file.pub>
Run the command from a terminal to be prompted for them instead, or set them once
with `nyxgpt cloud credentials-setup`.
```

You are asked at most once per command, whatever it does internally: a
`cloud deploy --os macos` resolves its settings four times (pricing, the plan,
the launch, then the substrate apply) and the answer you typed is carried
across all four.

### And it stays visible afterwards

The resolver records the account id and the local SSH files alongside the rest
of `infra.json`, so the choice can be reported later from a local read, with no
credentials and no API call:

* `nyxgpt cloud status` prints an **AWS account** row (`nyxgpt (066835328281)`)
  and an **SSH key pair** row;
* `nyxgpt cloud infra status` carries `aws_profile`, `aws_account_id`,
  `aws_account_label` and `ssh_key_name`;
* the dashboard's Infrastructure page shows both on the AWS substrate card and
  on the cloud deployment card.

All three say `not recorded here` rather than leaving a blank when the question
is asked from somewhere that cannot answer it — on the instance, or from an api
Pod, where no `infra.json` exists. That is a different claim from "no profile",
and the surfaces keep them apart. They cannot word it differently, either: the
row is rendered once in Python (`aws_account_label` in both status payloads) and
displayed as-is by the CLI and by the dashboard.

---

## Target-OS provisioning (P6-12/#3511, #3867)

**To provision an instance, use [`nyxgpt cloud deploy --os`](#nyxgpt-cloud-deploy--the-one-command-path-p6-11-3513).**
It renders the target OS's bootstrap and delivers it to the machine itself,
for [Linux](#nyxgpt-cloud-deploy--the-one-command-path-p6-11-3513) and for
[EC2 Mac](#ec2-mac-targets) alike. That is the whole provisioning story; the
rest of this section documents the renderer underneath it.

`nyxgpt cloud user-data` prints that same bootstrap script instead of
delivering it. It exists for the two cases a deploy cannot serve:

- **First boot with no SSH** — an instance launched by something other than
  nyxGPT, whose `user_data` provisions it as it comes up.
- **CI** — [`cloud-artifact-smoke.yml`](cloud-artifact-smoke.md) and
  `release-artifacts.yml`'s `ec2-linux-user-data-smoke` job execute the real
  rendered script rather than a copy that could drift.

One renderer serves both: `cloud_deploy.render_provision_script` calls
`cloud_provision.render_user_data` for the macOS family, so what a deploy
sends and what this prints cannot diverge. (The Linux deploy keeps its own,
SSH-shaped script — see [What the rendered scripts do](#what-the-rendered-scripts-do).)
The substrate module (P6-8, #3509) still sets no Terraform `user_data`.

```bash
nyxgpt cloud user-data --os linux
nyxgpt cloud user-data --os macos
```

### Options

| Flag | Description |
| --- | --- |
| `--os {linux,macos}` | Required. Target instance OS family. |
| `--version <version>` | Pin the installed nyxGPT version. Linux: `pip install nyxgpt==<version>`. macOS: selects the tap formulas that carry it — `nyxgpt-api`/`nyxgpt-web` for a release, `nyxgpt-api@<line>rc`/`nyxgpt-web@<line>rc` for a candidate (see [Remote tap](homebrew.md#remote-tap)) — and the rendered script asserts on the instance that the version it installed is the one asked for. Omit it to install whatever the tap currently serves as stable, in which case there is no declared version to assert against. |
| `--session-backend {file,cassandra}` | Where the instance stores chat sessions (#3865). Default: `cassandra` on Linux, `file` on macOS -- see below. |
| `--output <path>` | Write the rendered script to `path` instead of stdout. |

**Why the session-backend default differs by target OS.** The Linux template
runs `nyxgpt ops install`, which provisions the `nyxgpt-cassandra` container
as a core service, so `cassandra` is available on the instance and is the
default -- matching the Kubernetes overlay, and giving every mode pointed at
the same Cassandra one shared session list. The EC2 Mac template deliberately
does *not* run that path -- it installs the Homebrew formulas (api, web and
ollama) and starts them, see
[What the rendered scripts do](#what-the-rendered-scripts-do) -- so nothing
provisions a Cassandra there and the default is `file`. Passing
`--session-backend cassandra` on macOS is supported for an operator who
points `[rag] cassandra_hosts` at a Cassandra they run elsewhere. Both
templates apply the choice with `nyxgpt ops session-backend`, before the
services start, so no instance ever needs a hand-edited `config.ini`. See
[session-storage.md](session-storage.md).

### What the rendered scripts do

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
`nyxgpt-api`/`nyxgpt-web`, seeds `~/.nyxGPT/config.ini`, **installs Ollama
from its Homebrew formula, starts it with `brew services` and pulls the
configured chat and embedding models with `nyxgpt ops required-models`**, then
starts api and web via `brew services`. This
follows [the documented local remote-tap flow](homebrew.md#remote-tap)
exactly, in the script itself: a fresh EC2 Mac has neither Homebrew nor
`nyxgpt` on it, so the bootstrap installs the formulas directly rather than
installing a CLI first only to have it do the same thing. (`nyxgpt ops
install` reaches the same remote tap when it runs on a machine with no
checkout -- `_install_homebrew_api` in `src/nyxgpt/ops.py`, #3759.)

**Why the model backend is installed here and not left to `ops install`
(#4150).** It used to be left to it, and the result was an EC2 Mac deploy that
exited 0 reporting the release deployed onto a machine with a healthy api, a
healthy web and nothing behind them: `GET /api/v1/models` answered 502 on a
connection refused to `127.0.0.1:11434`, and chat was structurally impossible.
The macOS bootstrap does not run `ops install` because that command reconciles
a Docker engine this target cannot have — but Ollama is not part of the
container tier, it is the component that answers every chat message, and it
went missing along with the tier it was never part of. Two things are needed,
not one: installing Ollama leaves an empty model store, which answers
`/api/v1/models` with `200 []` instead of a 502 and leaves chat just as broken.
So the bootstrap installs Ollama *and* runs
[`nyxgpt ops required-models`](ops.md#nyxgpt-ops-required-models), which pulls
the models named by `[nyxgpt] default_model` and `[rag] embedding_model` —
read from configuration, so changing the shipped default changes what a deploy
pulls with no template edit.
`tests/unit/test_cloud_user_data_template_parity.py` fails the build if the two
bootstraps ever disagree about a core component again, and
`macos-brew-smoke.yml`'s `mac-model-backend` job executes the whole sequence on
a real `macos-15` runner.

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
with a 24-hour minimum allocation -- an AWS billing/allocation constraint, not
a nyxGPT one. `nyxgpt cloud deploy --os macos` allocates that host for you
after disclosing what it costs and asking, and defers its release past the
24-hour window rather than leaving it to you
(see [EC2 Mac targets](#ec2-mac-targets)). Any other Linux distro (no systemd, e.g. Alpine) or
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

[`cloud-target-os-smoke.yml`](../.github/workflows/cloud-target-os-smoke.yml)
covers the *delivery* half for both target OSes (#3867): it runs the installed
`nyxgpt cloud deploy` against a real sshd on the runner and asserts the macOS
bootstrap is what arrives, elevated with `sudo -n` and told which login user
to install for, while a Linux plan still puts the Linux script on the same
wire. It also asserts that `--os macos` with no Mac to run on refuses before
applying anything, naming the Dedicated Host constraint. Reverting the
dispatch fails it, so a green run is not green by luck.

**EC2 Mac hardware itself has no CI coverage** -- GitHub Actions has no macOS
EC2 runner, and Apple's licensing does not permit running macOS in a
container -- so the macOS support matrix above is documentation-verified for
the *instance* half (the acceptance criteria call for CI coverage "where
feasible (Linux at minimum)"). This is about EC2 Mac specifically, not about
macOS as such:
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
| `keychain` | The OS keychain, via the optional `keyring` package — `nyxgpt ops install-extra cloud` adds it to an install that does not already have it |
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
profile = nyxgpt          # optional -- falls back to [cloud] profile, then boto3's default chain
ssm_prefix = /nyxgpt       # provider = ssm
secretsmanager_id = nyxgpt # provider = secretsmanager
```

Leaving `provider` blank (the default) is a local deploy: the three
credentials are read from `config.ini` exactly as before, unaffected.

`profile` exists for the same reason `allow-ip --profile` does (#3993): these
clients were built with no profile, so a workstation whose default profile
names a different account resolved secrets from *that* account — and the
failure is quieter here than a NotFound, because a parameter that is missing
from the wrong account is indistinguishable from an unconfigured secret.
Unset, it inherits `[cloud] profile`, so an operator who configured the cloud
reference once does not have to configure it twice.

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

`nyxgpt[cloud]` is required at runtime for either provider -- see
`src/nyxgpt/cloud_secrets.py`. On a fresh install, `pip install
"nyxgpt[cloud]"`; on an install that already exists, `nyxgpt ops install-extra
cloud`, which installs into the environment `nyxgpt` itself runs from (on a
Homebrew keg that is not the environment a bare `pip` reaches). Tests exercise both
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
