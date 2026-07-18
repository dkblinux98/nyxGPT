#!/usr/bin/env bash
# Create the Phase 6 / v3.0.0 milestone and all its issues.
#
# WHY THIS SCRIPT EXISTS / WHERE TO RUN IT:
#   GitHub writes from the remote Claude Code session authenticate as the repo
#   owner and cannot create milestones (proxy returns 403), so this was prepared
#   there but must be RUN FROM A LOCAL session, where `gh` is authenticated as
#   myGPT-scrummaster-agent (per the session-start hook). Running it locally also
#   means every issue is authored by the scrummaster agent, per the identity policy.
#
# PREREQUISITE — SPRINTS (one-time, GitHub Projects UI):
#   Sprint is a ProjectV2 *iteration* field. Neither this script nor
#   create_issue.sh can CREATE iterations (create_issue.sh only assigns to
#   existing ones). Before running with SPRINTS_READY=1, add these four 2-week
#   iterations to the project's "Sprint" field in the Projects UI:
#     Sprint 6.0  2026-07-20 -> 2026-08-02
#     Sprint 6.1  2026-08-03 -> 2026-08-16
#     Sprint 6.2  2026-08-17 -> 2026-08-30
#     Sprint 6.3  2026-08-31 -> 2026-09-13
#   (Titles must match SPRINT_* below exactly.)
#
# USAGE:
#   ./scripts/agents/create_phase6.sh            # create milestone + issues (no sprint assignment)
#   SPRINTS_READY=1 ./scripts/agents/create_phase6.sh   # also assign issues to sprints
#   DRY_RUN=1 ./scripts/agents/create_phase6.sh   # print create_issue.sh calls, create nothing
#
# Idempotency: re-running creates DUPLICATE issues. Run once. The milestone
# creation step is guarded (skips if a "Phase 6" milestone already exists).

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

MILESTONE_TITLE="Phase 6: Cloud & Unified Deployment"
MILESTONE_DESC="v3.0.0 — cloud-native AWS deploy + unified one-command, OS-aware deployment. See product_management/PHASE_6_PLAN.md."
MILESTONE_DUE="2026-09-13T23:59:59Z"

SPRINT_60="Sprint 6.0"
SPRINT_61="Sprint 6.1"
SPRINT_62="Sprint 6.2"
SPRINT_63="Sprint 6.3"

DRY_RUN="${DRY_RUN:-0}"
SPRINTS_READY="${SPRINTS_READY:-0}"

# --- 1. Create the milestone (guarded) ---
existing="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=all" \
  --jq '.[] | select(.title | startswith("Phase 6")) | .title' 2>/dev/null | head -1 || true)"
if [[ -n "$existing" ]]; then
  echo "[phase6] Milestone already exists: '$existing' — skipping creation."
elif [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] would create milestone: '$MILESTONE_TITLE' (due $MILESTONE_DUE)"
else
  gh api -X POST "repos/${REPO_OWNER}/${REPO_NAME}/milestones" \
    -f title="$MILESTONE_TITLE" -f state="open" \
    -f description="$MILESTONE_DESC" -f due_on="$MILESTONE_DUE" >/dev/null
  echo "[phase6] Created milestone: '$MILESTONE_TITLE'"
fi

# --- helper: create one issue with common hygiene ---
mk() {
  local title="$1" label="$2" module="$3" effort="$4" sprint="$5" body="$6"
  local args=(
    --title "$title"
    --label "$label"
    --module "$module"
    --effort "$effort"
    --priority P1
    --milestone "$MILESTONE_TITLE"
    --phase "$MILESTONE_TITLE"
    --body "$body"
  )
  [[ "$SPRINTS_READY" == "1" && -n "$sprint" ]] && args+=(--sprint "$sprint")
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[dry-run] create_issue.sh --title %q --label %q --module %q --effort %q --sprint %q\n' \
      "$title" "$label" "$module" "$effort" "${sprint:-<none>}"
  else
    "$DIR/create_issue.sh" "${args[@]}"
    sleep 2
  fi
}

body() {
  # body <problem> <criterion1>;<criterion2>;...  -> formatted issue body
  local problem="$1"; shift
  local out="## Problem / Motivation\n\n${problem}\n\n## Acceptance Criteria\n"
  local IFS=';'
  for c in $1; do out+="- [ ] ${c}\n"; done
  out+="\n## Related\n\n- Part of Phase 6 / v3.0.0 (see product_management/PHASE_6_PLAN.md)"
  printf '%b' "$out"
}

# ================= Sprint 6.0 — Hardening gate =================
mk "fix: Sandbox /api/v1/tools/{cat,ls,grep} to prevent arbitrary file read" \
  "Acceptance Failure" "API" "S" "$SPRINT_60" \
  "$(body "The tools endpoints resolve a caller-supplied path with no sandbox, allowing arbitrary file read (incl. ~/.nyxGPT/config.ini). Auth is off by default and the k8s config binds 0.0.0.0. Audit finding S1." \
  "Constrain resolved paths to an allowlisted workspace root (mirror the /api/v1/logs/view guard);Reject paths whose resolve() escapes the root;Add regression tests for traversal attempts")"

mk "feat: Require auth when the API binds to a non-loopback host" \
  "Feature" "API" "S" "$SPRINT_60" \
  "$(body "API auth is disabled by default and only /api/v1 is ever protected; the deploy config binds 0.0.0.0. Audit finding S2." \
  "Refuse to start (or hard-warn) when api.host is non-loopback and [auth] enabled is false;Document auth-on as the deploy default in the deployment checklist")"

mk "fix: Persist config.ini with 0600 permissions to protect the API key" \
  "Acceptance Failure" "API" "XS" "$SPRINT_60" \
  "$(body "config.ini is written with default permissions but stores the API key in cleartext. Audit finding S3." \
  "chmod config.ini to 0600 after every write;Create the parent dir 0700;Test the resulting mode")"

mk "feat: Add security scanning to CI (bandit, pip-audit, npm audit)" \
  "Feature" "Agent System" "S" "$SPRINT_60" \
  "$(body "No SAST or dependency-CVE scanning exists in CI. Audit finding S4." \
  "New push/PR-triggered job runs bandit, pip-audit, and npm audit;Fails on high-severity findings;Documented in testing.md")"

mk "feat: Add an independent push/PR Python test + mypy gate in CI" \
  "Feature" "Agent System" "S" "$SPRINT_60" \
  "$(body "pytest runs only inside the agent workflows (tests/unit only); a direct push merges with no Python test signal, and mypy is non-blocking. Audit findings C1/C2." \
  "Standalone workflow runs the full tests/ suite on push/PR;mypy runs blocking on a defined module set;Green on v3 branch")"

# ================= Sprint 6.1 — Unified local deploy =================
mk "feat: nyxgpt up — one command brings up the full stack after checkout" \
  "Feature" "CLI" "L" "$SPRINT_61" \
  "$(body "There is no single command that brings up the whole stack; today it is four separate hand-invoked paths (Compose, k8s, Terraform, ops install). Audit finding F1." \
  "A single documented command brings up API, web UI, Cassandra, wired to host Ollama;Waits for health and prints the web URL;Idempotent re-run;Status visible in the SRE dashboard (DoD)")"

mk "feat: Include Prometheus/Grafana/Loki/Jaeger in the default bring-up" \
  "Feature" "Observability" "M" "$SPRINT_61" \
  "$(body "Monitoring services are opt-in Compose profiles, so a bare bring-up skips them; 'one command brings up prometheus + grafana' is currently false. Audit finding F1." \
  "nyxgpt up starts the monitoring stack by default;--no-monitoring opts out;Dashboards reachable after bring-up")"

mk "feat: macOS/Linux detection with platform-appropriate native install" \
  "Feature" "CLI" "L" "$SPRINT_61" \
  "$(body "The native install path (ops.py) is macOS-only (launchctl/launchd/brew); there is no Linux/systemd path and no platform dispatch. Audit finding F1." \
  "platform.system() dispatch selects the correct native backend;Linux path uses systemd units;macOS path unchanged;Both verified")"

mk "feat: Single-command teardown + idempotent re-deploy for the unified stack" \
  "Feature" "CLI" "M" "$SPRINT_61" \
  "$(body "There is no clean teardown/redeploy for the unified stack." \
  "nyxgpt down tears everything down cleanly;Re-running nyxgpt up is idempotent;Teardown control in the SRE dashboard")"

mk "feat: Guided credentials/secrets setup during deploy (CLI + /admin wizard)" \
  "Feature" "CLI" "L" "$SPRINT_61" \
  "$(body "Deploy needs secrets written to ~/.nyxGPT/config.ini but nothing collects them with guidance; existing wizard prompts are bare. Owner requirement 2026-07-15." \
  "For each required secret ([auth] api_key, [error_tracking] dsn, [openai] api_key, [github] pat): name it plainly, explain its purpose, and say where to obtain/create it (URL + steps);Masked input (getpass) with format validation;Offer to generate [auth] api_key;Write config.ini at 0600;Never re-prompt for a secret already set (--reconfigure to force);Implemented on BOTH the CLI (run_wizard) and the /admin web wizard (DoD)")"

# ================= Sprint 6.2 — AWS IaC foundation =================
mk "feat: Architecture decision — AWS compute substrate (EC2 vs EKS)" \
  "Feature" "Agent System" "S" "$SPRINT_62" \
  "$(body "The cloud deploy needs a chosen compute substrate before modules are written." \
  "Recommendation with rationale (EC2 single-box vs EKS) for local-first-parity cloud deploy;Owner sign-off;Picks the target for the IaC modules")"

mk "feat: Terraform AWS modules — VPC, subnets, security groups, compute" \
  "Feature" "CLI" "XL" "$SPRINT_62" \
  "$(body "Cloud-native AWS provisioning (owner decision 2026-07-15, Phase 6). Provision the chosen substrate with least-privilege networking." \
  "Terraform modules provision VPC, subnets, security groups, and compute per the decision issue;Least-privilege security groups;terraform plan/apply/destroy documented")"

mk "feat: Terraform remote state (S3 backend + DynamoDB lock)" \
  "Feature" "CLI" "M" "$SPRINT_62" \
  "$(body "Cloud IaC needs shared, locked remote state." \
  "S3 backend configured for Terraform state;DynamoDB table for state locking;Documented bootstrap")"

mk "feat: Cloud secrets management (SSM / Secrets Manager) — no plaintext keys" \
  "Feature" "API" "M" "$SPRINT_62" \
  "$(body "Cloud deploys must not bake secrets into images/config in cleartext." \
  "API key and credentials sourced from AWS SSM Parameter Store or Secrets Manager;No plaintext secrets in images or config.ini on the cloud instance;Documented")"

# ================= Sprint 6.3 — One-command cloud deploy =================
mk "feat: nyxgpt cloud deploy — provision AWS + deploy the full app after checkout" \
  "Feature" "CLI" "XL" "$SPRINT_63" \
  "$(body "The headline v3 capability: one command provisions AWS and deploys the full app after a clean checkout." \
  "Single CLI command applies the infra then brings up the full stack on the provisioned instance;Wired to the monitoring stack;Returns the public URL;Idempotent re-deploy and teardown")"

mk "feat: Guided cloud-credential collection for AWS deploy" \
  "Feature" "CLI" "L" "$SPRINT_63" \
  "$(body "Cloud deploy needs AWS credentials collected with the same guided UX as local secrets. Owner requirement 2026-07-15." \
  "Collect AWS access key/secret/region/profile and secret-store refs with what-it-is + where-to-get-it help and masked entry;Never write AWS secrets to config.ini in plaintext (route to keychain/AWS profile/secret store);CLI + /admin cloud-deploy wizard")"

mk "feat: Target OS detection/provisioning for Linux vs macOS AWS instances" \
  "Feature" "CLI" "L" "$SPRINT_63" \
  "$(body "The cloud deploy must handle both Linux and macOS (EC2 mac) targets." \
  "Detects/selects the target AMI OS;Provisions and configures correctly for Linux and macOS instances;Documented")"

mk "feat: Cloud deploy status / teardown / rollback from the SRE dashboard" \
  "Feature" "Web UI" "L" "$SPRINT_63" \
  "$(body "Per the Definition of Done, the cloud deploy lifecycle must be operable from the dashboard, not just the CLI." \
  "Dashboard shows cloud deploy status;Teardown and rollback operable from /admin;CLI parity may exist but the dashboard is the required surface")"

mk "feat: Cloud smoke test — provision -> verify chat/RAG over public endpoint -> teardown" \
  "Feature" "Testing" "M" "$SPRINT_63" \
  "$(body "Mirror scripts/smoke-test.sh against a live AWS deployment." \
  "Script provisions, verifies chat + RAG over the public endpoint, then tears down;Documented;Runnable end-to-end")"

# ================= Capstone (LAST) =================
mk "feat: Phase 6 capstone — deploy to AWS from a clean checkout in one command, monitored + self-healing" \
  "Feature" "CLI" "XL" "$SPRINT_63" \
  "$(body "Ultimate finishing touch of Phase 6: from a clean checkout, one command yields a provisioned, deployed, monitored, self-healing nyxGPT reachable over the internet, operable entirely from the SRE dashboard. Implement LAST, after all Phase 6 issues above are closed." \
  "Clean checkout -> one command -> provisioned + deployed + monitored + self-healing app reachable over the internet;Entire lifecycle operable from the SRE dashboard;Documented end-to-end smoke test;Teardown;Depends on all other Phase 6 issues (do not select while any are open)")"

echo "[phase6] Done. Review the created issues on the project board."
