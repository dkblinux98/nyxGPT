# Phase 6 — Cloud Deployment & Unified Orchestration (v3.0.0)

**Created:** 2026-07-15
**Owner decision (2026-07-15):** add a Phase 6 milestone and a v3.0.0 release to deliver
cloud-native AWS deployment plus the unified one-command, OS-aware deploy story. Phase X is
unrelated to local-first and does not constrain this work; cloud-native provisioning is now
explicitly in scope for v3.

## Deployment model decision — native-first, one container (owner, 2026-07-15)

**The DEFAULT local deployment is native-on-the-host for every component — API, web, Ollama,
Prometheus, Grafana, Jaeger, GlitchTip, Loki — with Cassandra as the *only* Docker container.
All components communicate over `localhost`.** The Docker Compose stack (#2689) and Kubernetes
(#2688) are **retained, not retired** — they are optional deployments for **future enterprise
use**, plus an optional **local deployment for testing only**. Native-first is what an ordinary
single-user install gets by default.

- **Native-first is the default and primary path.** `nyxgpt ops install` becomes the native
  deployment manager: it installs and configures *every* component as a native Homebrew service,
  using `homebrew/nyxgpt-api.rb` and `homebrew/nyxgpt-web.rb` as the pattern (nyxgpt-tap
  formulae, or upstream formulae configured for nyxGPT), and manages the single Cassandra Docker
  container. No user runs raw `brew`, `docker`, `docker compose`, or `kubectl` (ops-wrapper
  principle).
- **Compose (#2689) and k8s (#2688) remain supported** as optional enterprise / local-testing
  deployments. Their container-networking acceptance failures (#3177, #3178, #3179, #3182,
  #3184, #3185) are **not moot** — they still need fixing for those optional paths — but they
  are **not blockers for the default native deployment**, so they re-prioritize below the
  native-deployment work rather than closing.
- **One wrapped entry point, mode by flag.** `nyxgpt ops install` (no flag) = native default.
  `nyxgpt ops install --k8s` = Kubernetes, resolving to a **local minikube** cluster for
  testing or an **AWS/EKS-style** cluster for enterprise deployment. (A Compose flag can follow
  the same pattern.) The user never runs raw `minikube`/`kubectl`/`eksctl`/`docker compose` —
  every mode is selected through the `nyxgpt` wrapper.
- **Blue-green (#2691) and canary (#2692)** stay on the retained k8s substrate; not dropped.
- Native-first makes the *default* private-to-the-workstation posture natural (native services
  bind `localhost`), and reduces the split-brain (#16) by making native the primary model
  (Compose/k8s are an explicit opt-in, not an accidental parallel stack).
- The AWS deploy (Sprint 6.2/6.3) is the enterprise/cloud path and may use the containerized
  model; the default local experience stays native.

## Theme

One command, after a clean `git checkout`, brings up the **entire** nyxGPT stack —
API, web UI, Cassandra, host-wired Ollama, **and** the full monitoring stack
(Prometheus, Grafana, Loki, Jaeger) — with the deploy path **detecting macOS vs Linux**
and doing the right thing on each. That same capability extends to the cloud: one CLI
command provisions AWS infrastructure (Terraform) and deploys the full app onto a
Linux or macOS AWS instance, monitored and self-healing, operable from the SRE/admin
dashboard per the Definition of Done.

## Why a hardening sprint comes first

Two audit findings become **critical** the moment the API is exposed on a public cloud
endpoint rather than localhost:

- **S1** — `/api/v1/tools/{cat,ls,grep}` allow **arbitrary file read** (no path sandbox).
  On a public AWS endpoint this is remote arbitrary file disclosure of the host,
  including the API key in `~/.nyxGPT/config.ini`.
- **S2** — the API is **unauthenticated by default**, and the deploy config binds `0.0.0.0`.

These must land **before** any cloud exposure. Sprint 6.0 is therefore a gate, not optional.

---

## Sprint 6.0 — Deployment hardening (prerequisite gate)

Must merge before any public cloud exposure work in 6.2+.

1. **fix: Sandbox `/api/v1/tools/{cat,ls,grep}` to prevent arbitrary file read**
   Constrain resolved paths to an allowlisted workspace root, mirroring the existing
   guard on `/api/v1/logs/view/{filename}`. Reject any path whose `resolve()` escapes root.
   *Label: Acceptance Failure · Module: API · Effort: S*

2. **feat: Require auth when the API binds to a non-loopback host**
   If `api.host` is not loopback, refuse to start (or hard-warn) unless `[auth] enabled=true`.
   Document auth-on as the deploy default in the deployment checklist.
   *Label: Feature · Module: API · Effort: S*

3. **fix: Persist `config.ini` with 0600 permissions to protect the API key**
   `os.chmod(cfg_path, 0o600)` after every write; parent dir `0o700`.
   *Label: Acceptance Failure · Module: API · Effort: XS*

4. **feat: Add security scanning to CI (bandit, pip-audit, npm audit)**
   New push/PR-triggered job. Fail on high-severity dependency CVEs and Python SAST findings.
   *Label: Feature · Module: Agent System · Effort: S*

5. **feat: Add an independent push/PR Python test + mypy gate in CI**
   A standalone workflow that runs the full `tests/` suite and a blocking `mypy` on push/PR,
   independent of the agent pipeline (closes the C1/C2 gap: today pytest only runs inside
   the dev/review agent workflows, over `tests/unit/` only).
   *Label: Feature · Module: Agent System · Effort: S*

## Sprint 6.1 — Unified local deploy + OS detection

Closes the capstone (#3160) deploy-pillar gap: a real single command, monitoring included,
OS-aware.

6. **feat: `nyxgpt up` — one command brings up the full stack after checkout**
   Single entrypoint orchestrating the Compose stack (all core services), waiting for health,
   printing the web URL. Idempotent. Dashboard surface for status per DoD.
   *Label: Feature · Module: CLI · Effort: L*

7. **feat: Include Prometheus/Grafana/Loki/Jaeger in the default bring-up**
   Monitoring comes up as part of `nyxgpt up` (today they are opt-in Compose profiles that a
   bare `docker compose up` skips). `--no-monitoring` to opt out.
   *Label: Feature · Module: Observability · Effort: M*

8. **feat: macOS/Linux detection with platform-appropriate native install**
   Add a Linux path (systemd units) alongside the existing macOS-only launchd/launchctl path
   in `ops.py`; dispatch on `platform.system()`. Today the native install path is Mac-only.
   *Label: Feature · Module: CLI · Effort: L*

9. **feat: Single-command teardown + idempotent re-deploy for the unified stack**
   `nyxgpt down` / re-run `nyxgpt up` cleanly. Dashboard teardown control.
   *Label: Feature · Module: CLI · Effort: M*

9a. **feat: Guided credentials/secrets setup during deploy (CLI + `/admin` wizard)**
   When `nyxgpt up` runs and a required secret is unset, collect it through a **guided,
   self-documenting** flow that, for each key, (a) names it in plain language, (b) explains
   what it's for, (c) says exactly where to obtain or create it (URL + steps), (d) prompts
   with **masked input** (`getpass`), (e) validates format where possible, and (f) writes to
   `~/.nyxGPT/config.ini` with **0600** perms (implements audit S3). Extends the existing
   `run_wizard` (`src/nyxgpt/wizard.py`) and the `/admin` setup wizard
   (`web/src/app/admin/page.tsx`) — the same guided step must exist on the **web** surface
   per the Definition of Done, not CLI-only.
   Secrets in scope: `[auth] api_key` (protect the instance — offer to generate one),
   `[error_tracking] dsn` (GlitchTip project settings → DSN),
   `[openai] api_key` (platform.openai.com/api-keys, only if OpenAI is enabled),
   `[github] pat` (github.com/settings/tokens, only if agent ops are used).
   Idempotent: never re-prompts for a secret already present; `--reconfigure` to force.
   *Label: Feature · Module: CLI · Effort: L*

## Sprint 6.2 — AWS IaC foundation

10. **feat: Architecture decision — AWS compute substrate (EC2 single-box vs EKS)**
    Decision issue with a recommendation and rationale; picks the target for the modules below.
    *Label: Feature · Module: Agent System · Effort: S*

11. **feat: Terraform AWS modules — VPC, subnets, security groups, compute**
    Cloud provider modules provisioning the chosen substrate (per #10). Least-privilege SGs.
    *Label: Feature · Module: CLI · Effort: XL*

12. **feat: Terraform remote state (S3 backend + DynamoDB lock)**
    *Label: Feature · Module: CLI · Effort: M*

13. **feat: Cloud secrets management (SSM Parameter Store / Secrets Manager) — no plaintext keys**
    API key and any credentials sourced from AWS secret storage, never baked into images/config.
    *Label: Feature · Module: API · Effort: M*

## Sprint 6.3 — One-command cloud deploy

14. **feat: `nyxgpt cloud deploy` — provision AWS + deploy the full app after checkout**
    Single CLI command: `terraform apply` the infra, then bring up the full stack on the
    provisioned Linux/macOS AWS instance, wired to the monitoring stack, returning the URL.
    *Label: Feature · Module: CLI · Effort: XL*

15. **feat: Target OS detection/provisioning for Linux vs macOS AWS instances**
    Provision and configure correctly whether the target AMI is Linux or macOS (EC2 mac).
    *Label: Feature · Module: CLI · Effort: L*

15a. **feat: Guided cloud-credential collection for AWS deploy**
    Extends the #9a guided-secrets flow to the cloud path: AWS access key / secret /
    region / profile, and the target secret-store references (SSM/Secrets Manager per #13),
    each with what-it-is + where-to-get-it help and masked entry. Never writes AWS secrets
    to `config.ini` in plaintext — routes them to the OS keychain / AWS profile / secret
    store. CLI + `/admin` cloud-deploy wizard.
    *Label: Feature · Module: CLI · Effort: L*

16. **feat: Cloud deploy status / teardown / rollback from the SRE dashboard**
    DoD surface: the whole cloud deploy lifecycle operable from `/admin`, not just the CLI.
    *Label: Feature · Module: Web UI · Effort: L*

17. **feat: Cloud smoke test — provision → verify chat/RAG over public endpoint → teardown**
    Mirrors `scripts/smoke-test.sh` but against a live AWS deployment.
    *Label: Feature · Module: Testing · Effort: M*

## Capstone (implement LAST)

18. **feat: Phase 6 capstone — deploy nyxGPT to AWS from a clean checkout in one command,
    monitored and self-healing**
    End-to-end acceptance: clean checkout → one command → provisioned, deployed, monitored,
    self-healing app reachable over the internet, entirely operable from the SRE dashboard;
    documented end-to-end smoke test; teardown. Depends on all of #6.0–#6.3.
    *Label: Feature · Module: CLI · Effort: XL · Sequencing: LAST*

---

## Cross-cutting principle — everything through `nyxgpt` wrappers (owner, 2026-07-15)

No operation may require a raw `docker`/`docker compose`/`kubectl`/`terraform` command from
the user; all of it is exposed through `nyxgpt` commands (`nyxgpt up`/`down`/`ops …`) and the
dashboard. This is now codified in CLAUDE.md ("Operational Command Wrapping"). It elevates the
`nyxgpt up`/`down`/`ops` wrappers (#6, #9, #8) from convenience to hard requirement, and it
constrains the fixes for the deploy-layer acceptance failures: self-heal (#3179), smoke test
(#3180), and blue-green/canary (#3184) must heal/operate via wrappers, and the UI strings +
the five docs that currently show raw `docker compose`/`kubectl` must be converted.

## Cross-cutting principle — private-to-the-workstation access, even in the cloud (owner, 2026-07-15)

**Every nyxGPT deployment — local *and* remote (AWS Linux/macOS) — is reachable only from the
owner's own workstation, never publicly exposed.** A cloud deploy provisions private
infrastructure the owner reaches through a locked path (e.g. SSH tunnel / WireGuard / Tailscale,
or a security group scoped to the owner's current IP with services bound to loopback behind the
tunnel) — not a public endpoint. This applies to the app *and* the local observability tools
(Grafana/Prometheus/Jaeger/GlitchTip), which are reached over localhost (or the tunnel), never a
public URL. This is a non-negotiable security posture consistent with VISION.md (local-first,
privacy-respecting) and directly constrains the AWS security-group/networking work (#11), the
`nyxgpt cloud deploy` command (#14), and the "returns the URL" behavior (the URL is a
tunnel/loopback address, not a public one). The specific access mechanism is an architecture
decision to make explicitly (like #10), not to assume.

## Sequencing rules

- Sprint 6.0 blocks 6.2/6.3 (no public exposure before the API is sandboxed + auth-gated).
- #18 (capstone) is selected LAST, only after #1–#17 are closed — same gate pattern as #3160.
- #10 (compute-substrate decision) blocks #11, #14, #15.

## Not in this milestone (recommend separate scheduling)

- **A1** — split the 4,616-line `app.py` monolith into per-domain routers. Pure refactor,
  no user-facing change; schedule as its own tech-debt issue, not gated on v3.
- **A3** — `auto-check-tasklist` read-modify-write race (workflow tooling hygiene).
- **C3** — coverage floor (`--cov-fail-under`).
