#!/usr/bin/env bash
# File the Phase 6 issue set from product_management/PHASE_6_PLAN.md
# (2026-07-31 rewrite) into the owner-created milestone and the two 2-week
# sprints (owner decision 2026-07-31: split by effort and dependencies).
#
# HOW TO RUN:
#   Via .github/workflows/file_phase6_issues.yml (workflow_dispatch), which
#   runs this script under SCRUMMASTER_AGENT_TOKEN so every issue is authored
#   by the scrummaster agent per the identity policy. Dry-run first.
#
# WHAT IT DOES:
#   - Preflight: resolves the open "Phase 6*" milestone by title prefix
#     (owner-created; this script never creates milestones), lists the
#     project's Sprint iterations, and fails fast if the two target sprint
#     titles are missing.
#   - Duplicate guard: aborts if an open issue already carries the P6-1
#     title (re-running would file duplicates).
#   - Files 17 issues via create_issue.sh with explicit Status/Priority/
#     Effort/Module/Sprint/Milestone per the plan, in dependency order so
#     the autopilot's lowest-number-first selection respects sequencing.
#     Later bodies reference earlier issues by real number.
#
# SPRINT SPLIT (effort- and dependency-aware):
#   Early sprint  — hardening gate, decision records, and platform work with
#                   no cloud dependency: P6-1..P6-7, P6-10, P6-14
#                   (6xS, 2xM, 1xL)
#   Late sprint   — the cloud build chain, blocked on the gate/decisions:
#                   P6-8, P6-9, P6-12, P6-13, P6-11, P6-15, P6-17, P6-16
#                   (3xXL, 3xM, 2xL; P6-16 capstone filed LAST)
#
# ENV:
#   DRY_RUN=1        (default) print the filing plan, create nothing
#   SPRINT_EARLY     iteration title for the first sprint  (default: Sprint 7)
#   SPRINT_LATE      iteration title for the second sprint (default: Sprint 8)

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"
load_config
require_gh_auth

DRY_RUN="${DRY_RUN:-1}"
SPRINT_EARLY="${SPRINT_EARLY:-Sprint 7}"
SPRINT_LATE="${SPRINT_LATE:-Sprint 8}"

# --- Preflight 1: milestone (owner-created; matched by title prefix so the
# --- owner's exact wording/dash/version-suffix never matters) ---
echo "[phase6] Open milestones:"
gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=open&per_page=50" \
  --jq '.[] | "  - \(.title) (due: \(.due_on // "none"))"'
MILESTONE="$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=open&per_page=50" \
  --jq '[.[] | select(.title | startswith("Phase 6"))][0].title // empty')"
[[ -n "$MILESTONE" ]] || _die "No open milestone with title starting 'Phase 6'. Owner must create it first."
echo "[phase6] Target milestone: '$MILESTONE'"

# --- Preflight 2: sprint iterations must exist ---
ITER_JSON="$(graphql "query(\$owner:String!, \$number:Int!) {
  user(login:\$owner) {
    projectV2(number:\$number) {
      fields(first:20) {
        nodes {
          ... on ProjectV2IterationField {
            name
            configuration { iterations { title startDate duration } }
          }
        }
      }
    }
  }
}" -F owner="$PROJECT_OWNER" -F number="$PROJECT_NUMBER")"
echo "[phase6] Sprint iterations on the project:"
echo "$ITER_JSON" | jq -r '.data.user.projectV2.fields.nodes[] | select(.name=="Sprint") |
  .configuration.iterations[] | "  - \(.title): \(.startDate) + \(.duration)d"'
for s in "$SPRINT_EARLY" "$SPRINT_LATE"; do
  found="$(echo "$ITER_JSON" | jq -r --arg t "$s" '.data.user.projectV2.fields.nodes[] |
    select(.name=="Sprint") | .configuration.iterations[] | select(.title==$t) | .title' | head -1)"
  [[ -n "$found" ]] || _die "Sprint iteration '$s' not found on the project. Owner must create it first."
done
echo "[phase6] Sprint split: early='$SPRINT_EARLY' late='$SPRINT_LATE'"

# --- Duplicate guard --- (REST Search API -- its own rate pool, separate
# from both core REST and GraphQL)
P61_TITLE="feat: refuse non-loopback API bind without auth enabled"
dupe="$(gh api search/issues \
  --method GET \
  -f q="\"$P61_TITLE\" in:title state:open type:issue repo:${REPO_OWNER}/${REPO_NAME}" \
  --jq '.items[0].number // empty')"
if [[ -n "$dupe" ]]; then
  _die "Open issue #$dupe already carries the P6-1 title — Phase 6 appears already filed. Aborting (re-running duplicates)."
fi

# --- Filing helper ---
declare -A NUM
ref() { # ref P6-1 -> "#3498 (P6-1)" once filed, "P6-1" in dry-run / before filing
  local k="$1"
  if [[ -n "${NUM[$k]:-}" ]]; then echo "#${NUM[$k]} ($k)"; else echo "$k"; fi
}

mk() {
  local key="$1" sprint="$2" module="$3" effort="$4" title="$5" body="$6"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo ""
    echo "[dry-run] $key -> '$title'"
    echo "          Sprint='$sprint' Module='$module' Effort='$effort' Priority='P1 - High' Label=Feature"
    echo "          Milestone='$MILESTONE' Status=Backlog body=${#body} chars"
    NUM[$key]=""
    return 0
  fi
  local n
  n="$("$DIR/create_issue.sh" \
    --title "$title" \
    --label "Feature" \
    --module "$module" \
    --effort "$effort" \
    --priority "P1 - High" \
    --sprint "$sprint" \
    --milestone "$MILESTONE" \
    --body "$body")"
  [[ "$n" =~ ^[0-9]+$ ]] || _die "create_issue.sh did not return an issue number for $key (got: '$n')"
  NUM[$key]="$n"
  echo "[phase6] $key filed as #$n ($sprint, $module, $effort)"
}

PLAN_REF="product_management/PHASE_6_PLAN.md (2026-07-31 rewrite)"

# =====================================================================
# EARLY SPRINT — hardening gate, decisions, cloud-independent platform
# =====================================================================

mk P6-1 "$SPRINT_EARLY" security S \
  "$P61_TITLE" \
"## Problem / Motivation
The API is unauthenticated by default and deploy configs can bind \`0.0.0.0\`. Exposure without auth must be impossible, not just discouraged — env-sync's warning text exists, enforcement doesn't. This is part of the Phase 6 hardening gate: no cloud-exposure work merges before it.

## Acceptance Criteria
- [ ] API startup with a non-loopback \`api.host\` and \`[auth] enabled != true\` refuses to start with an actionable error (what to set, pointer to the wizard)
- [ ] Loopback binds are unaffected
- [ ] Deployment checklist documents auth-on as the deploy default
- [ ] Re-verify every config/secrets write path still lands 0600/0700 perms (closes the audit S3 residue)
- [ ] Tests for the refusal matrix (host x auth) and the perms sweep

## Technical Details
- Files affected: \`src/nyxgpt/app.py\` startup path, config handling, \`docs/deployment-checklist.md\`, tests
- Part of the hardening gate (with P6-2, P6-3): blocks all cloud-exposure work

## Related Issues/PRs
- Spec: P6-1 in $PLAN_REF"

mk P6-2 "$SPRINT_EARLY" security S \
  "feat: security scanning in CI - bandit, pip-audit, npm audit" \
"## Problem / Motivation
No security scanning runs in CI. Part of the Phase 6 hardening gate.

## Acceptance Criteria
- [ ] Push/PR workflow running bandit (Python SAST), pip-audit, and \`npm audit\` (web)
- [ ] Fails on high-severity findings
- [ ] Baseline/suppression file for accepted findings with justification comments
- [ ] Documented in the developer runbook

## Technical Details
- Workflow-file note: agents cannot write \`.github/workflows/*\` — deliver the proposed YAML in-repo for owner-side application (hand-carry pattern per #3454/#3479)
- Part of the hardening gate (with P6-1, P6-3)

## Related Issues/PRs
- Spec: P6-2 in $PLAN_REF"

mk P6-3 "$SPRINT_EARLY" testing S \
  "feat: standalone push/PR CI gate - full pytest suite plus blocking mypy" \
"## Problem / Motivation
pytest/mypy today run only inside the agent workflows, and only over \`tests/unit/\`. There is no independent CI gate on push/PR. Part of the Phase 6 hardening gate.

## Acceptance Criteria
- [ ] An independent workflow on push/PR runs the full \`tests/\` suite (unit + integration where environment-feasible) and a blocking \`mypy src/\`
- [ ] Green on the current release branch before merge (fix or explicitly skip-with-comment anything red)
- [ ] Documented in the developer runbook

## Technical Details
- Same workflow-file hand-carry note as P6-2: deliver proposed YAML for owner-side application
- Part of the hardening gate (with P6-1, P6-2)

## Related Issues/PRs
- Spec: P6-3 in $PLAN_REF"

mk P6-4 "$SPRINT_EARLY" documentation S \
  "feat: decision - private access mechanism for cloud deployments" \
"## Problem / Motivation
Standing owner decision (2026-07-15): every deployment, local or AWS, is reachable only from the owner's workstation over a locked path — never a public endpoint, for the app AND the observability tools. The concrete mechanism is undecided and blocks the infra builds.

## Acceptance Criteria
- [ ] A decision record under \`product_management/\` comparing SSH tunnel, WireGuard, Tailscale, and owner-IP-scoped security groups against the private-access principle
- [ ] Picks one with rationale, spelling out how the owner reaches app + observability UIs and what \"returns the URL\" means under it
- [ ] Reviewed/approved by the owner on this issue before $(ref P6-8)/$(ref P6-11)-class work starts

## Technical Details
- Decision issue — blocks the Terraform AWS modules and cloud deploy issues

## Related Issues/PRs
- Spec: P6-4 in $PLAN_REF"

mk P6-5 "$SPRINT_EARLY" cli S \
  "feat: nyxgpt up and down aliases with health-wait and URL print" \
"## Problem / Motivation
\`nyxgpt ops install\`/\`down\` are the shipped one-command story; the original Phase 6 \`up\`/\`down\` naming plus bring-up UX polish remain undone.

## Acceptance Criteria
- [ ] \`nyxgpt up\` = alias for the full reconcile (mode flags pass through), then waits for component health (reusing self-heal probes) and prints the web URL
- [ ] \`nyxgpt down\` = alias for teardown
- [ ] Both idempotent
- [ ] Docs/help updated
- [ ] No behavior forked from \`ops\` (thin aliases, single code path)

## Technical Details
- Files affected: \`src/nyxgpt/cli.py\` (or equivalent entrypoint), \`src/nyxgpt/ops.py\`, docs

## Related Issues/PRs
- Spec: P6-5 in $PLAN_REF
- Builds on #3406/#3414 (ops install reconcile)"

mk P6-6 "$SPRINT_EARLY" cli M \
  "feat: guided secrets setup - masked input and per-key help, CLI + admin wizard" \
"## Problem / Motivation
The first-run wizard exists but secrets entry lacks the guided treatment. DSN collection is obsolete (auto-provisioned per #3411/#3458), shrinking the original scope.

## Acceptance Criteria
- [ ] For each secret still human-provided (\`[auth] api_key\` — with an offer to generate, \`[openai] api_key\`, \`[github] pat\`): plain-language name, what-it's-for, exactly where to obtain it, masked entry (\`getpass\`), format validation, 0600 write
- [ ] Idempotent (\`--reconfigure\` to force)
- [ ] The same guided step exists in the \`/admin\` wizard per the Definition of Done
- [ ] Tests for prompt/skip/validate flows

## Technical Details
- Files affected: first-run wizard (CLI + \`web/src/app/admin\`), config write paths, tests

## Related Issues/PRs
- Spec: P6-6 in $PLAN_REF"

mk P6-7 "$SPRINT_EARLY" documentation S \
  "feat: decision - AWS compute substrate, EC2 single-box vs EKS" \
"## Problem / Motivation
The AWS compute substrate is undecided and blocks the Terraform modules, cloud deploy, and target-OS provisioning work.

## Acceptance Criteria
- [ ] Decision record with recommendation + rationale: cost, ops burden, fit with the canary/k8s substrate (#3409/#3419), and the private-access mechanism from $(ref P6-4)
- [ ] Owner-approved on this issue

## Technical Details
- Decision issue — blocks the Terraform AWS modules, cloud deploy, and OS-provisioning issues

## Related Issues/PRs
- Spec: P6-7 in $PLAN_REF"

mk P6-10 "$SPRINT_EARLY" security M \
  "feat: cloud secrets via SSM Parameter Store or Secrets Manager" \
"## Problem / Motivation
Cloud deploys must never bake credentials into AMIs, user-data, tfvars, or config files.

## Acceptance Criteria
- [ ] API key and credentials sourced from AWS secret storage on cloud deploys
- [ ] Local deploys unchanged
- [ ] Rotation documented
- [ ] Tests with mocked AWS clients

## Technical Details
- Blocked by: $(ref P6-7) (substrate decision)
- Testable with mocked AWS clients — no live infra dependency, so it fits the early sprint once the decision lands

## Related Issues/PRs
- Spec: P6-10 in $PLAN_REF"

mk P6-14 "$SPRINT_EARLY" cli L \
  "feat: Linux-native install path - systemd units with OS dispatch" \
"## Problem / Motivation
The native install path is macOS-only (brew + launchd). Linux runs the stack (CI terraform smoke proves it) but has no native-first story.

## Acceptance Criteria
- [ ] \`platform.system()\` dispatch in ops
- [ ] systemd unit generation/management mirroring the launchd path (install/start/stop/restart/status/logs per service, log paths feeding the same \`~/.nyxGPT/logs\` shipping)
- [ ] Self-heal's native probes work on Linux
- [ ] Doctor checks are OS-appropriate
- [ ] Docs gain a Linux install section
- [ ] CI exercise of the systemd path (container or unit-level as feasible)

## Technical Details
- Files affected: \`src/nyxgpt/ops.py\`, self-heal probes, doctor, docs, CI
- Pairs with $(ref P6-12) (shares the OS-dispatch layer); no cloud dependency, so it anchors the early sprint's L slot

## Related Issues/PRs
- Spec: P6-14 in $PLAN_REF"

# =====================================================================
# LATE SPRINT — the cloud build chain (dependency-forced), capstone LAST
# =====================================================================

mk P6-8 "$SPRINT_LATE" cli XL \
  "feat: Terraform AWS modules - VPC, subnets, security groups, compute" \
"## Problem / Motivation
Cloud-native AWS provisioning (standing owner decision 2026-07-15). Provision the chosen substrate with least-privilege networking implementing the private-access mechanism.

## Acceptance Criteria
- [ ] Provider modules provisioning the $(ref P6-7) substrate
- [ ] Least-privilege SGs implementing the $(ref P6-4) access mechanism (no 0.0.0.0/0 ingress anywhere)
- [ ] Builds on the local terraform layout (\`nyxgpt-tf-*\` naming/conventions)
- [ ] \`terraform validate\` + a plan-level test in CI
- [ ] Wrapped entirely behind \`nyxgpt\` commands

## Technical Details
- Blocked by the hardening gate $(ref P6-1)/$(ref P6-2)/$(ref P6-3) and decisions $(ref P6-4)/$(ref P6-7) — do not select before they close

## Related Issues/PRs
- Spec: P6-8 in $PLAN_REF
- Builds on #3410/#3428 (local terraform substrate)"

mk P6-9 "$SPRINT_LATE" cli M \
  "feat: Terraform remote state - S3 backend with DynamoDB locking" \
"## Problem / Motivation
Cloud Terraform needs shared, locked remote state.

## Acceptance Criteria
- [ ] S3 backend + DynamoDB lock provisioning and migration from local state
- [ ] Documented recovery story
- [ ] Wrapped setup (no raw \`terraform init\` instructions)

## Technical Details
- Blocked by: $(ref P6-8)

## Related Issues/PRs
- Spec: P6-9 in $PLAN_REF"

mk P6-12 "$SPRINT_LATE" cli L \
  "feat: target-OS provisioning for Linux and macOS AWS instances" \
"## Problem / Motivation
Cloud provisioning must configure the stack correctly on Linux AMIs and EC2 Mac.

## Acceptance Criteria
- [ ] Cloud provisioning configures the stack correctly on Linux AMIs and EC2 Mac
- [ ] Documented support matrix
- [ ] CI coverage where feasible (Linux at minimum)

## Technical Details
- Blocked by: $(ref P6-7) (substrate decision)
- Pairs with $(ref P6-14) (shares the OS-dispatch layer)

## Related Issues/PRs
- Spec: P6-12 in $PLAN_REF"

mk P6-13 "$SPRINT_LATE" cli M \
  "feat: guided AWS credential collection for cloud deploy" \
"## Problem / Motivation
Cloud deploy needs AWS credentials collected with the same guided treatment as the local secrets flow.

## Acceptance Criteria
- [ ] Extends $(ref P6-6)'s guided flow to AWS access key/secret/region/profile + secret-store references
- [ ] Masked entry with what-it-is/where-to-get-it help
- [ ] AWS secrets never written to \`config.ini\` — routed to AWS profile / OS keychain / secret store
- [ ] CLI + \`/admin\` cloud wizard parity

## Technical Details
- Blocked by: $(ref P6-6), $(ref P6-10)

## Related Issues/PRs
- Spec: P6-13 in $PLAN_REF"

mk P6-11 "$SPRINT_LATE" cli XL \
  "feat: nyxgpt cloud deploy - provision AWS and deploy the full stack" \
"## Problem / Motivation
One command from provisioned infra to a running, monitored stack over the private access path.

## Acceptance Criteria
- [ ] One command: apply infra, deploy the full app + observability onto the provisioned instance, wire the $(ref P6-4) access path, wait for health, print the (tunnel/loopback) URL
- [ ] Idempotent re-runs
- [ ] \`nyxgpt cloud destroy\` counterpart
- [ ] Smoke-verifiable end to end

## Technical Details
- Blocked by: $(ref P6-8), $(ref P6-9), $(ref P6-10)

## Related Issues/PRs
- Spec: P6-11 in $PLAN_REF"

mk P6-15 "$SPRINT_LATE" sre L \
  "feat: cloud deploy lifecycle from the SRE dashboard" \
"## Problem / Motivation
Cloud deploy must be visible (and possibly operable) from \`/admin\` per the Definition of Done.

## Acceptance Criteria
- [ ] Cloud deploy status visible from \`/admin\`
- [ ] Lifecycle controls (deploy/teardown/rollback) reconciled with the owner's #3410 status-only precedent for the local Infrastructure page — an explicit owner decision on this issue settles whether cloud gets controls or status-plus-CLI-pointers
- [ ] Whatever is decided is fully implemented and tested

## Technical Details
- Blocked by: $(ref P6-11)

## Related Issues/PRs
- Spec: P6-15 in $PLAN_REF
- Owner precedent: #3410 (Infrastructure page is status-only)"

mk P6-17 "$SPRINT_LATE" testing M \
  "feat: cloud smoke test - provision, verify chat and RAG, teardown" \
"## Problem / Motivation
The cloud path needs an end-to-end smoke test mirroring the local one.

## Acceptance Criteria
- [ ] Mirrors \`scripts/smoke-test.sh\` against a live cloud deployment over the private access path (chat round-trip, RAG ingest+query, observability reachable)
- [ ] Leaves no billed resources behind on success or failure
- [ ] Wrapped invocation

## Technical Details
- Blocked by: $(ref P6-11)
- $(ref P6-16) (capstone) runs this script green as one of its ACs

## Related Issues/PRs
- Spec: P6-17 in $PLAN_REF"

mk P6-16 "$SPRINT_LATE" cli XL \
  "feat: Phase 6 capstone - clean checkout to monitored AWS deploy in one command" \
"## Problem / Motivation
The Phase 6 end-to-end acceptance: from a clean checkout, one command yields a provisioned, deployed, monitored, self-healing nyxGPT reachable only via the private access path.

## Acceptance Criteria
- [ ] Clean checkout -> one command -> provisioned, deployed, monitored, self-healing app reachable only via the private access path
- [ ] Operable per the $(ref P6-15) decision
- [ ] Documented end-to-end smoke test ($(ref P6-17)'s script) run green
- [ ] Teardown verified

## Technical Details
- **Sequencing: select LAST — depends on every other Phase 6 issue.** Do not select while any other Phase 6 issue is open.

## Related Issues/PRs
- Spec: P6-16 in $PLAN_REF"

# --- Summary ---
echo ""
if [[ "$DRY_RUN" == "1" ]]; then
  echo "[phase6] DRY RUN complete — 17 issues would be filed into '$MILESTONE'"
  echo "[phase6]   $SPRINT_EARLY: P6-1 P6-2 P6-3 P6-4 P6-5 P6-6 P6-7 P6-10 P6-14"
  echo "[phase6]   $SPRINT_LATE:  P6-8 P6-9 P6-12 P6-13 P6-11 P6-15 P6-17 P6-16"
else
  echo "[phase6] Filed 17 issues into '$MILESTONE':"
  for k in P6-1 P6-2 P6-3 P6-4 P6-5 P6-6 P6-7 P6-10 P6-14 P6-8 P6-9 P6-12 P6-13 P6-11 P6-15 P6-17 P6-16; do
    echo "  $k -> #${NUM[$k]}"
  done
fi
