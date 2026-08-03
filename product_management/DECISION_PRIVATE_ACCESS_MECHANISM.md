# Decision Record: Private Access Mechanism for Cloud Deployments (P6-4)

**Issue:** #3503 · **Status:** Proposed — awaiting owner review/approval on the issue
**Author:** developer-agent · **Date:** 2026-08-03
**Blocks:** P6-8 (Terraform AWS modules), P6-11 (`nyxgpt cloud deploy`)

## Problem

Standing owner decision (2026-07-15, restated in
[`PHASE_6_PLAN.md`](PHASE_6_PLAN.md#standing-owner-decisions-unchanged-restated)):
every nyxGPT deployment, local or AWS, must be reachable only from the
owner's workstation over a locked path — never a public endpoint — for the
app **and** the observability tooling (Grafana, GlitchTip, tracing UI). The
concrete mechanism was left undecided and blocks the P6-8 Terraform AWS
modules and the P6-11 `nyxgpt cloud deploy` command, both of which need to
wire a real access path rather than guess at one.

This record compares the four candidates named in the issue's acceptance
criteria and picks one.

## Constraints from existing decisions

- **Repo-less portability** (CLAUDE.md, 2026-08-01): the mechanism can't
  require anything beyond what a clean workstation already has, or a single
  standard install step — it can't depend on the nyxGPT repo being checked
  out on the client side.
- **`nyxgpt`-wrapped commands only** (CLAUDE.md, 2026-07-15): no raw
  `ssh`/`terraform`/`docker`/`kubectl` in any user-facing instruction; the
  mechanism must be something a `nyxgpt` subcommand can drive end to end.
- **Existing local precedent**: [`docs/security.md`](../docs/security.md#network-exposure)
  already recommends "SSH tunnel / VPN" as the preferred way to reach a
  loopback-bound local deployment from another machine
  (`ssh -L 8000:127.0.0.1:8000 -L 3000:127.0.0.1:3000 user@host`), and
  [`docs/docker-compose.md`](../docs/docker-compose.md) repeats the same
  guidance. Whatever P6-4 picks for AWS should not contradict that story —
  ideally it reuses it.
- **No new required third-party account**: nothing in the stack today
  requires signing up for an external coordination/relay service to operate
  nyxGPT. Introducing one changes the trust model (a third party can, in
  principle, be a path to the owner's private deployment) and adds an
  external dependency to the Definition of Done for every cloud deploy.

## Options considered

### 1. SSH tunnel (bastion-style, over the EC2 instance's own SSH)

The EC2 instance binds the API/web/observability ports to `127.0.0.1` only
(exactly like the native local deployment does today), and the owner
reaches them via local port-forwarding over SSH:
`ssh -L 8000:127.0.0.1:8000 -L 3000:127.0.0.1:3000 -L 3001:127.0.0.1:3001
ec2-user@<instance>`. The instance's security group allows inbound `22`
from the owner's current IP (or a small owner-managed allowlist) and
**nothing else** — no app or observability port is ever opened to any
network, public or private.

- **Pros:** Zero new dependencies — every provisioning path already needs
  SSH access to the instance for `nyxgpt cloud deploy` itself, so this adds
  no new client software, no account signup, no coordination server. Directly
  reuses the pattern already documented and owner-familiar from local
  deployments. Nothing is ever listening on a non-loopback address on the
  instance, so there is no exposure surface even if the security group rule
  is temporarily wrong. Fully scriptable behind a `nyxgpt` subcommand (open
  the tunnel, print the localhost URLs, tear it down on exit).
- **Cons:** The owner's IP can change (travel, hotel wifi, mobile
  tethering), which means the SG's SSH-allow rule needs to be kept current —
  addressed below by combining with option 4. A tunnel is a foreground
  process (or a background one the owner remembers to close) rather than an
  always-on network membership.

### 2. WireGuard

A WireGuard server on the EC2 instance (or a separate small instance), the
owner's workstation as a peer, app/observability ports reachable over the
WireGuard interface's private address range.

- **Pros:** Always-on private network membership rather than a per-session
  tunnel; lower per-connection latency than SSH port-forwarding; standard,
  well-audited protocol.
- **Cons:** Requires provisioning and keeping a new service alive (kernel
  module or userspace daemon, key generation and rotation, a config file
  synced to the owner's client) that doesn't exist anywhere in the stack
  today — a new operational surface with its own self-heal/monitoring story
  P6-4's constraints don't otherwise require. Client setup on the owner's
  workstation is a new one-time install (`wg-quick` or a GUI client) beyond
  what repo-less portability already assumes (an artifact-installed `nyxgpt`
  CLI + a normal SSH client). No reuse of the existing SSH-tunnel-first
  local-deployment story — this would be a second, parallel private-access
  mechanism to document and maintain alongside it.

### 3. Tailscale

A managed WireGuard-based mesh; the EC2 instance and the owner's workstation
both join the owner's "tailnet," coordinated through Tailscale's hosted
control plane (or a self-hosted Headscale replacement).

- **Pros:** Easiest day-to-day UX of the four — no manual IP-allowlisting,
  works across NATs and changing owner IPs automatically, stable MagicDNS
  names instead of the instance's changing public IP.
- **Cons:** Introduces a mandatory third-party account and a hosted
  coordination service into the trust path for reaching every nyxGPT
  deployment — a new external dependency the project has avoided everywhere
  else (repo-less portability, no forced accounts, `nyxgpt`-wrapped local
  operation). Self-hosting Headscale removes the third-party account but
  reintroduces the "new always-on service to run and heal" cost from option
  2, plus Headscale-specific operational knowledge. Client install on the
  owner's workstation is again a new one-time dependency beyond an SSH
  client. Best fit for a multi-device/multi-user mesh, which is not this
  project's shape — nyxGPT's private-access requirement is one owner
  reaching one deployment at a time.

### 4. Owner-IP-scoped security groups only (no tunnel)

The security group's ingress rules for the app/observability ports
themselves are restricted to the owner's current public IP; the ports are
opened directly (still never `0.0.0.0/0`) and reached over plain HTTP(S) to
the instance's public or private IP.

- **Pros:** Simplest possible request path — no tunnel process, direct URL
  in a browser. No new software on either side.
- **Cons:** The app and observability ports are still bound to a
  non-loopback address and directly reachable by anyone who can spoof or
  acquire the allowed IP (weaker than SSH's key-based auth) — this is IP
  allowlisting, not authentication, and CLAUDE.md's P6-1 hardening gate
  already establishes that non-loopback binds need auth, not just network
  restriction, as the enforced baseline. Same IP-churn problem as option 1's
  SSH rule, but here the blast radius of a stale/wrong rule is a directly
  reachable app port instead of just SSH. Does not reuse or extend the
  existing SSH-tunnel local-deployment story; it's a different model
  entirely (network-restricted public bind vs. never-exposed loopback bind).

## Decision

**SSH tunnel (option 1), with the security group itself owner-IP-scoped for
the SSH port (the useful part of option 4) as defense in depth.**

Concretely:

- The EC2 instance's security group denies all inbound traffic except TCP
  `22`, and that rule's source is restricted to the owner's current public
  IP (an owner-IP-scoped SG, per option 4) — not `0.0.0.0/0`.
- The API, web UI, and every observability endpoint (Grafana, GlitchTip,
  tracing UI) bind to `127.0.0.1` on the instance, identical to the P6-1
  loopback-by-default rule for the native local deployment. No app or
  observability port is ever opened in the security group, at any scope.
- The owner reaches everything by SSHing in with local port-forwards for
  each service, the same mental model already documented in
  [`docs/security.md`](../docs/security.md#network-exposure) for local
  deployments, just pointed at the EC2 host instead of `localhost`.

This wins on the constraints above: it adds no new dependency (SSH is
already required to manage the instance), needs no new always-on service to
provision, monitor, or self-heal, needs no client software beyond a
standard SSH client already assumed by repo-less portability, and directly
extends rather than duplicates the tunnel-first story the local deployment
docs already teach the owner. WireGuard and Tailscale are rejected primarily
for introducing a new operational surface (own daemon, own keys, own
health) or a mandatory third-party coordination service where the existing
SSH path already satisfies the requirement at zero incremental cost.
Owner-IP-scoped SGs alone are rejected as the *sole* mechanism because
opening the app port directly is weaker (IP spoofing/churn risk on an
unauthenticated network path) than a loopback bind reached only through an
authenticated SSH session — but the IP-scoping technique is still the right
way to lock down the one port (`22`) that does need to be open.

## How the owner reaches the app + observability UIs

```
nyxgpt cloud tunnel
```

wraps the equivalent of:

```
ssh -N \
  -L 8000:127.0.0.1:8000 \
  -L 3000:127.0.0.1:3000 \
  -L 3001:127.0.0.1:3001 \
  <deploy-user>@<instance-public-ip>
```

(port list generated from whichever observability profiles the deployment
has enabled, mirroring `nyxgpt ops install`'s existing profile detection)
and prints the resulting `http://localhost:<port>` URLs for the app and for
each enabled observability UI — the same URLs the owner already uses
locally, just tunneled to the instance instead of served natively. The
command holds the tunnel open in the foreground (or `--background` to
daemonize) and tears it down cleanly on exit or via `nyxgpt cloud
tunnel --stop`.

## What "returns the URL" means under this mechanism

P6-11's "print the (tunnel/loopback) URL" acceptance criterion resolves to:
`nyxgpt cloud deploy` provisions the instance, deploys the stack bound to
`127.0.0.1` as above, confirms health over the tunnel it opens for that
purpose, and prints the same `nyxgpt cloud tunnel` invocation (and the
`localhost` URLs it will yield) rather than any public/instance-facing URL —
there is no URL that resolves to the app without first running the tunnel
command, by design.

## Owner review

Per the issue's acceptance criteria, this decision needs owner
review/approval **on issue #3503** before P6-8 or P6-11 begin. Once
approved, this section will be updated with the approval date; until then,
downstream issues should treat this mechanism as proposed, not final.
