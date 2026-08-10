# nyxGPT Cloud (AWS)

`nyxgpt cloud` is the CLI surface for AWS-deployed nyxGPT stacks (P6-11-class
scope). It currently covers `allow-ip` (#3630) and `user-data` (#3511,
target-OS provisioning); full provisioning and teardown (`nyxgpt cloud
deploy`/`destroy`, #3513) land separately.

Install the AWS SDK dependency with:

```bash
pip install "nyxgpt[cloud]"
```

`boto3` is kept out of the base install -- it's only needed for AWS
deployments, not the local stack every other `nyxgpt` command drives.

---

## Background: the owner-IP-scoped SSH rule

Per
[`product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md`](../product_management/DECISION_PRIVATE_ACCESS_MECHANISM.md),
an AWS-deployed nyxGPT instance is reached only over an SSH tunnel
(`nyxgpt cloud tunnel`, forthcoming): the API, web UI, and every
observability endpoint bind to `127.0.0.1` on the instance and are never
opened in the security group. The security group allows exactly one inbound
rule -- TCP port 22, scoped to the owner's current public IP, never
`0.0.0.0/0`.

The tradeoff: when the owner's IP changes (ISP renewal, travel, mobile
tethering), that rule goes stale and the instance becomes unreachable,
**including over SSH** -- there is no other way in. `nyxgpt cloud allow-ip`
exists to fix exactly this, and does so by talking only to the AWS EC2 API,
never the instance, so it works from the new IP while still locked out.

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
store AWS credentials itself. Guided credential collection is separate scope
(P6-13).

---

## Target-OS provisioning (P6-12/#3511)

`nyxgpt cloud user-data` renders the EC2 user-data bootstrap script that
installs nyxGPT on a fresh instance and brings up the native stack --
per-target-OS, from published artifacts only. This is the OS-dispatch layer
the not-yet-implemented Terraform AWS module (P6-8) and `nyxgpt cloud
deploy` (P6-11) will consume; it doesn't talk to AWS itself.

```bash
nyxgpt cloud user-data --os linux
nyxgpt cloud user-data --os macos
```

### Options

| Flag | Description |
| --- | --- |
| `--os {linux,macos}` | Required. Target instance OS family. |
| `--version <version>` | Pin the installed nyxGPT version. Linux: `pip install nyxgpt==<version>`. macOS: recorded in the script for reference only -- the Homebrew tap always tracks its current formula (see [Remote tap](homebrew.md#remote-tap)), not an arbitrary pinned release. Default: latest. |
| `--output <path>` | Write the rendered script to `path` instead of stdout. |

### What the rendered script does

**`--os linux`** (Amazon Linux 2023, Ubuntu 22.04/24.04 LTS -- see the
[support matrix](#target-os-support-matrix) below), in order:

1. **Prerequisites**, via the AMI's own package manager (`dnf`/`apt`):
   Python 3 + pip (+ `python3-venv` on Ubuntu), a Docker engine
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
(`dkblinux98/homebrew-nyxgpt`) and installs `nyxgpt-api`/`nyxgpt-web`,
seeds `~/.nyxGPT/config.ini`, and starts both via `brew services`. This
follows [the documented local remote-tap flow](homebrew.md#remote-tap)
exactly -- it deliberately does **not** call `nyxgpt ops install`, whose
macOS path builds a *local* tap from a repo checkout
(`_install_homebrew_api` in `src/nyxgpt/ops.py`) that doesn't exist on a
fresh instance.

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
minimum)"). One consequence worth an owner/manual verification pass on a
real `mac*.metal` instance: the macOS script's `brew services start` calls
depend on a launchd session for the login user, the launchd analogue of the
systemd session the Linux script sets up explicitly. EC2 Mac's default
`ec2-user` does auto-login to a GUI session at boot, so this is expected to
work, but it has not been exercised on real hardware.

## Note for the AWS Terraform module (P6-8)

`allow-ip` mutates the security group's port-22 ingress rule directly via
the AWS API, outside of Terraform. When the AWS Terraform module lands, its
security-group resource must not fight that: give the ingress rule a
`lifecycle { ignore_changes = [ingress] }` (or manage it as a separate
`aws_security_group_rule` excluded from the plan) so a routine
`terraform apply` doesn't revert an `allow-ip` refresh back to a stale IP.
The module should also write `security_group_id` and `region` to
`~/.nyxGPT/cloud/state.json` on apply, so `allow-ip` can auto-discover its
target without `--security-group-id`/`--region`. Its `user_data` should be
`nyxgpt cloud user-data --os <linux|macos>`'s rendered output for the
instance's chosen AMI family.

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
