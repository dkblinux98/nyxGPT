# Phase 6 — Enterprise Deployment & Hardening (v3.0.0)

**Created:** 2026-07-15 · **Rewritten:** 2026-07-31 (post-Phase-5.5 scope re-review)
**Milestone:** `Phase 6 — Enterprise Deployment & Hardening (v3.0.0)` (owner-created)
**Sprints:** `Sprint 7` (2026-08-01) + `Sprint 8` (2026-08-15) — two 2-week sprints,
owner decision 2026-07-31 (`Sprint 6` belongs to Phase 5.5; no `6.x` sprint naming).
Split by effort and dependencies:
Sprint 7 = hardening gate, decision records, and cloud-independent platform work
(P6-1..P6-7, P6-10, P6-14); Sprint 8 = the dependency-blocked cloud build chain
(P6-8, P6-9, P6-11..P6-13, P6-15..P6-17), capstone P6-16 selected last.
Filed via `scripts/agents/create_phase6.sh` (workflow `file_phase6_issues.yml`).

Phase 5.5 absorbed or overturned a large part of the original plan (ledger below). This
rewrite contains only the work that remains, specified so each item can be filed directly
as an issue with the correct fields. Unless a spec block says otherwise, every issue is
filed with:

> **Status:** Backlog · **Milestone:** Phase 6 — Enterprise Deployment & Hardening (v3.0.0) ·
> **Sprint:** per the split above · **Priority:** P1 - High · **Label/Module/Effort:** per block

## Standing owner decisions (unchanged, restated)

- **Native-first, one container (2026-07-15):** the default local deployment is native on
  the host for every component, Cassandra as the only Docker container, all localhost.
  Compose and Kubernetes are retained as optional enterprise/testing paths. Shipped: this
  is exactly what `nyxgpt ops install` does today.
- **Everything through `nyxgpt` wrappers (2026-07-15, CLAUDE.md):** no raw
  `docker`/`kubectl`/`terraform` in any user instruction or flow.
- **Private-to-the-workstation access, even in the cloud (2026-07-15):** every deployment,
  local or AWS, is reachable only from the owner's workstation over a locked path (tunnel /
  WireGuard / Tailscale / owner-IP-scoped SG). Never a public endpoint — for the app *and*
  the observability tools. The concrete mechanism is decision issue **P6-4** below.
- **Repo-less portability (2026-08-01, CLAUDE.md):** the entire stack installs and
  runs without a repo checkout — published artifacts only (package + remote tap +
  container images), across macOS / Linux / Docker / k8s / AWS EC2 (no Windows).
  Delivered incrementally: P6-5 (core packaging + runtime self-containment),
  P6-14 (Linux artifact install), P6-12/P6-11 (cloud provisions from artifacts,
  never clones), P6-16 (capstone accepts from a clean, checkout-free machine).
- **Canary, not blue-green (#3409, 2026-07-29):** blue-green is retired; canary is the
  progressive-delivery strategy on the k8s substrate (api + web per #3419; ollama documented
  infeasible).

## Absorbed by Phase 5.5 (do not re-file)

| Original item | Disposition |
|---|---|
| Sandbox `/api/v1/tools/*` (audit S1) | Moot — tools/TUI feature removed entirely (#3429) |
| config.ini 0600 permissions (audit S3) | Done — ops chmods config/tfvars/secret files 0600 (#3432/#3458 era); P6-1 re-verifies coverage as an AC |
| One-command full-stack bring-up | Done in substance — `nyxgpt ops install` reconciles the full native stack (#3406/#3414); alias/polish residue is P6-5 |
| Monitoring in the default bring-up | Done — install brings up observability by default, `--skip-observability` to opt out |
| Teardown + idempotent re-deploy | Done — `nyxgpt ops down` / idempotent install; "dashboard teardown control" overturned by #3410 (Infrastructure page is status-only, owner decision) |
| GlitchTip DSN collection (part of guided secrets) | Obsolete — DSN is auto-provisioned end-to-end (#3411/#3458) |
| Local Terraform substrate | Done — `nyxgpt ops install --terraform --local`, `nyxgpt-tf-*` containers, mode-aware status/self-heal (#3410/#3428) |

---

## Issues

Sequencing: **P6-1..P6-3 are the hardening gate — no cloud-exposure work (P6-8 onward)
merges before they do.** P6-4 (access mechanism) and P6-7 (substrate) are decision issues
blocking the infra builds. P6-16 (capstone) is selected LAST.

Sprint assignment: **Sprint 7** carries P6-1..P6-7, P6-10, P6-14 (6×S, 2×M, 1×L — the
gate, both decisions, and platform work with no cloud dependency). **Sprint 8** carries
P6-8, P6-9, P6-12, P6-13, P6-11, P6-15, P6-17, P6-16 (3×XL, 3×M, 2×L — the cloud chain,
dependency-forced into the second sprint; issues are filed in that order so
lowest-number-first selection respects the dependencies).

### P6-1 · feat: refuse non-loopback API bind without auth enabled
**Label:** Feature · **Module:** security · **Effort:** S

- Problem: the API is unauthenticated by default and deploy configs can bind `0.0.0.0`;
  exposure without auth must be impossible, not just discouraged (env-sync's warning text
  exists; enforcement doesn't).
- ACs: API startup with a non-loopback `api.host` and `[auth] enabled != true` refuses to
  start with an actionable error (what to set, pointer to the wizard); loopback binds
  unaffected; deployment checklist documents auth-on as the deploy default; re-verify every
  config/secrets write path still lands 0600/0700 perms (closes the S3 residue); tests for
  the refusal matrix (host × auth) and the perms sweep.

### P6-2 · feat: security scanning in CI - bandit, pip-audit, npm audit
**Label:** Feature · **Module:** security · **Effort:** S

- ACs: push/PR workflow running bandit (Python SAST), pip-audit, and `npm audit` (web);
  fails on high-severity findings; baseline/suppression file for accepted findings with
  justification comments; documented in the developer runbook. Workflow-file note: agents
  cannot write `.github/workflows/*` — deliver proposed YAML for owner-side application
  (hand-carry pattern per #3454/#3479).

### P6-3 · feat: standalone push/PR CI gate - full pytest suite plus blocking mypy
**Label:** Feature · **Module:** testing · **Effort:** S

- Problem: pytest/mypy today run only inside the agent workflows, over `tests/unit/` only.
- ACs: an independent workflow on push/PR runs the full `tests/` suite (unit + integration
  where environment-feasible) and a blocking `mypy src/`; green on current v2.0.0 before
  merge (fix or explicitly skip-with-comment anything red); same workflow-file hand-carry
  note as P6-2.

### P6-4 · feat: decision - private access mechanism for cloud deployments
**Label:** Feature · **Module:** documentation · **Effort:** S · **Blocks:** P6-8, P6-11

- ACs: a decision record under `product_management/` comparing SSH tunnel, WireGuard,
  Tailscale, and owner-IP-scoped security groups against the private-access principle;
  picks one with rationale, spelling out how the owner reaches app + observability UIs and
  what "returns the URL" means under it; reviewed/approved by the owner on the issue before
  P6-8/P6-11 start.

### P6-5 · feat: nyxgpt up and down aliases with health-wait and URL print
**Label:** Feature · **Module:** cli · **Effort:** L *(raised from S 2026-08-01: absorbs
the core repo-less packaging work)*

- Problem: `ops install`/`down` are the shipped one-command story; the original Phase 6
  `up`/`down` naming plus bring-up UX polish remain undone. And per the repo-less
  portability requirement (2026-08-01), the whole install story currently violates the
  standing decision: `ops.py` resolves runtime data repo-relative (`REPO_ROOT`) and the
  brew tap is generated locally from a checkout.
- ACs: `nyxgpt up` = alias for the full reconcile (mode flags pass through), then waits for
  component health (reusing self-heal probes) and prints the web URL; `nyxgpt down` =
  alias for teardown; both idempotent; docs/help updated; no behavior forked from `ops`
  (thin aliases, single code path). **Repo-less core:** all runtime data (compose files,
  config templates, launchd/systemd templates, grafana/promtail/prometheus provisioning,
  helper scripts) ships inside the package (importlib.resources or ops-managed copies
  under `~/.nyxGPT`) — no `REPO_ROOT` lookups remain; install artifacts are published
  (pip-installable package via PyPI or GitHub Releases, remote brew tap with versioned
  tarballs, container images to a registry for the Compose/k8s paths); acceptance:
  `nyxgpt up` brings up the full local stack on a macOS machine that has never cloned
  the repo; a source checkout stays supported for development only.

### P6-6 · feat: guided secrets setup - masked input and per-key help, CLI + admin wizard
**Label:** Feature · **Module:** cli · **Effort:** M

- Problem: the first-run wizard exists but secrets entry lacks the guided treatment; DSN
  collection is obsolete (auto-provisioned), shrinking the original scope.
- ACs: for each secret still human-provided (`[auth] api_key` — with an offer to generate,
  `[openai] api_key`, `[github] pat`): plain-language name, what-it's-for, exactly where to
  obtain it, masked entry (`getpass`), format validation, 0600 write; idempotent
  (`--reconfigure` to force); the same guided step exists in the `/admin` wizard per the
  Definition of Done; tests for prompt/skip/validate flows. **Canonical store + sync
  (owner, 2026-08-01):** `~/.nyxGPT/config.ini` is the single canonical store for
  write-once external tokens (Slack webhook/bot token, PATs — the issuing service never
  shows them again); a wrapped `nyxgpt ops secrets-sync` pushes a declared key mapping
  one-way to GitHub Actions secrets (sealed-box via the GitHub API, values never
  printed/logged), so nothing is ever hand-edited in two places. Machine-generated
  secrets stay in `~/.nyxGPT/secrets/` (#3458 precedent); human-provided ones in
  config.ini; both 0600.

### P6-7 · feat: decision - AWS compute substrate, EC2 single-box vs EKS
**Label:** Feature · **Module:** documentation · **Effort:** S · **Blocks:** P6-8, P6-11, P6-12

- ACs: decision record with recommendation + rationale (cost, ops burden, fit with the
  canary/k8s substrate, private-access mechanism from P6-4); owner-approved on the issue.

### P6-8 · feat: Terraform AWS modules - VPC, subnets, security groups, compute
**Label:** Feature · **Module:** cli · **Effort:** XL · **Blocked by:** P6-1..P6-4, P6-7

- ACs: provider modules provisioning the P6-7 substrate; least-privilege SGs implementing
  the P6-4 access mechanism (no 0.0.0.0/0 ingress anywhere); builds on the local terraform
  layout (`nyxgpt-tf-*` naming/conventions); `terraform validate` + a plan-level test in CI;
  wrapped entirely behind `nyxgpt` commands.

### P6-9 · feat: Terraform remote state - S3 backend with DynamoDB locking
**Label:** Feature · **Module:** cli · **Effort:** M · **Blocked by:** P6-8

- ACs: S3 backend + DynamoDB lock provisioning and migration from local state; documented
  recovery story; wrapped setup (no raw `terraform init` instructions).

### P6-10 · feat: cloud secrets via SSM Parameter Store or Secrets Manager
**Label:** Feature · **Module:** security · **Effort:** M · **Blocked by:** P6-7

- ACs: API key and credentials sourced from AWS secret storage on cloud deploys — never
  baked into AMIs, user-data, tfvars, or config files; local deploys unchanged; rotation
  documented; tests with mocked AWS clients.

### P6-11 · feat: nyxgpt cloud deploy - provision AWS and deploy the full stack
**Label:** Feature · **Module:** cli · **Effort:** XL · **Blocked by:** P6-8..P6-10

- ACs: one command: apply infra, deploy the full app + observability onto the provisioned
  instance, wire the P6-4 access path, wait for health, print the (tunnel/loopback) URL;
  idempotent re-runs; `nyxgpt cloud destroy` counterpart; smoke-verifiable end to end.
  **Repo-less:** the deploy ships published artifacts/images to the instance (never
  clones), and the operator side runs from an artifact-installed `nyxgpt` CLI — the whole
  flow works from a workstation with no checkout.

### P6-12 · feat: target-OS provisioning for Linux and macOS AWS instances
**Label:** Feature · **Module:** cli · **Effort:** L · **Blocked by:** P6-7
**Pairs with:** P6-14 (shares the OS-dispatch layer)

- ACs: cloud provisioning configures the stack correctly on Linux AMIs and EC2 Mac;
  documented support matrix; CI coverage where feasible (Linux at minimum). **Repo-less:**
  instances install from published artifacts/images (P6-5) — provisioning never runs
  `git clone` on a target machine.

### P6-13 · feat: guided AWS credential collection for cloud deploy
**Label:** Feature · **Module:** cli · **Effort:** M · **Blocked by:** P6-6, P6-10

- ACs: extends P6-6's guided flow to AWS access key/secret/region/profile + secret-store
  references; masked entry with what-it-is/where-to-get-it help; AWS secrets never written
  to `config.ini` — routed to AWS profile / OS keychain / secret store; CLI + `/admin`
  cloud wizard parity.

### P6-14 · feat: Linux-native install path - systemd units with OS dispatch
**Label:** Feature · **Module:** cli · **Effort:** L

- Problem: the native install path is macOS-only (brew + launchd); Linux runs the stack
  (CI terraform smoke proves it) but has no native-first story.
- ACs: `platform.system()` dispatch in ops; systemd unit generation/management mirroring
  the launchd path (install/start/stop/restart/status/logs per service, log paths feeding
  the same `~/.nyxGPT/logs` shipping); self-heal's native probes work on Linux; doctor
  checks OS-appropriate; docs gain a Linux install section; CI exercise of the systemd path
  (container or unit-level as feasible). **Repo-less:** the Linux install path works from
  the published artifacts (P6-5) on a machine with no checkout — same self-contained
  resource resolution, no git required on the target.

### P6-15 · feat: cloud deploy lifecycle from the SRE dashboard
**Label:** Feature · **Module:** sre · **Effort:** L · **Blocked by:** P6-11

- ACs: cloud deploy status visible from `/admin` per the Definition of Done; lifecycle
  controls (deploy/teardown/rollback) reconciled with the owner's #3410 status-only
  precedent for the local Infrastructure page — an explicit owner decision on the issue
  settles whether cloud gets controls or status-plus-CLI-pointers; whatever is decided is
  fully implemented and tested.

### P6-16 · feat: Phase 6 capstone - clean machine to monitored AWS deploy in one command
**Label:** Feature · **Module:** cli · **Effort:** XL · **Sequencing: selected LAST**

- ACs: end-to-end acceptance — **clean machine (no repo checkout): install `nyxgpt` from
  published artifacts** → one command → provisioned, deployed, monitored, self-healing app
  reachable only via the private access path; operable per the P6-15 decision; documented
  end-to-end smoke test (P6-17's script) run green; teardown verified; depends on every
  other P6 issue. (Re-scoped 2026-08-01 from "clean checkout" per the repo-less
  portability requirement; the checkout path remains a dev-mode convenience, not the
  acceptance path.)

### P6-17 · feat: cloud smoke test - provision, verify chat and RAG, teardown
**Label:** Feature · **Module:** testing · **Effort:** M · **Blocked by:** P6-11

- ACs: mirrors `scripts/smoke-test.sh` against a live cloud deployment over the private
  access path (chat round-trip, RAG ingest+query, observability reachable); leaves no
  billed resources behind on success or failure; wrapped invocation.

---

## Not in this milestone (schedule separately)

- **A1** — split the `app.py` monolith into per-domain routers (pure refactor, own issue).
- **A3** — `auto-check-tasklist` read-modify-write race (workflow hygiene).
- **C3** — coverage floor for Python (`--cov-fail-under`) — partially superseded by the
  web 100% gate precedent; decide the Python floor when filing.
- Compose/k8s container-networking acceptance-failure backlog (#3177–#3185 era): re-verify
  against post-5.5 reality before re-prioritizing — several are likely already fixed by the
  mode-awareness campaign (#3409/#3410/#3428).
