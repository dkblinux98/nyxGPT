#!/usr/bin/env bash
# sprint-iteration-preservation-proof.sh — prove the ceremony's sprint-creation
# mutation does not wipe board item values (#4166).
#
# WHY THIS EXISTS, AND WHY IT RUNS RATHER THAN BEING REASONED ABOUT
#
# The only API for adding a project iteration is
# `updateProjectV2Field(iterationConfiguration: ...)`, and it REPLACES the
# whole iteration list. The same mutation family with `singleSelectOptions`
# wiped Status on all 1018 board items on 2026-08-10. `ProjectV2Iteration`
# input does carry an optional `id`, so resubmitting every existing iteration
# with its id plus the new one MIGHT preserve item values -- but "might" is not
# evidence, and the blast radius is the owner's whole board.
#
# So #4166 made this binding: before any ceremony code touches project 2, the
# mutation is proven on a THROWAWAY project. This script is that proof, kept in
# the repo so it is re-runnable rather than a one-off transcript, and wired into
# `.github/workflows/sprint-iteration-proof-smoke.yml`.
#
# The scratch project is set up in the ONLY state in which the ceremony ever
# creates an iteration -- every existing iteration completed, none active or
# upcoming -- so the payload under test is the real one, taken verbatim from
# `scripts/agents/lib/release_prereqs.py sprint-plan`.
#
# It proves three things, two of them by fault injection. A proof that only
# shows the good case cannot tell "the ids worked" from "the API ignored the
# whole payload":
#
#   1. POSITIVE  resubmitting every iteration WITH its id, plus the new one,
#                leaves every item's iteration value untouched -- including
#                items in COMPLETED iterations.
#   2. NEGATIVE  dropping `completedIterations` (which the GraphQL
#                `configuration { iterations }` field does not return) wipes
#                exactly the items assigned to those completed iterations, and
#                nothing else.
#   3. NEGATIVE  resubmitting with NO ids recreates the iterations and wipes
#                the remaining item value too.
#
# Usage:
#   scripts/sprint-iteration-preservation-proof.sh [--keep]
#     --keep   do not delete the scratch project (for inspection)
#
# Requires: a token with the `project` scope (GH_TOKEN). The scratch project is
# created under the AUTHENTICATED user, never under the owner's account, and is
# deleted on exit.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREREQS="$ROOT/scripts/agents/lib/release_prereqs.py"

KEEP=0
case "${1:-}" in
  --keep) KEEP=1 ;;
  -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "[proof] unknown argument: $1" >&2; exit 2 ;;
esac

say() { echo "[proof] $*"; }
ok()  { echo "[proof] ok: $*"; }
FAILURES=0
fail() { echo "[proof] FAIL: $*" >&2; FAILURES=$((FAILURES + 1)); }

command -v gh >/dev/null || { echo "[proof] gh is required" >&2; exit 2; }
command -v jq >/dev/null || { echo "[proof] jq is required" >&2; exit 2; }

# The scratch project is created and DELETED. It may only ever be one under the
# token's own account -- never the owner's project 2.
LOGIN="$(gh api user --jq .login)" || { echo "[proof] not authenticated" >&2; exit 2; }
say "scratch project owner: ${LOGIN} (never the owner's account)"

PROJECT_NUMBER=""
cleanup() {
  [[ -n "$PROJECT_NUMBER" ]] || return 0
  if [[ $KEEP -eq 1 ]]; then
    say "scratch project kept: https://github.com/users/${LOGIN}/projects/${PROJECT_NUMBER}"
    return 0
  fi
  gh project delete "$PROJECT_NUMBER" --owner "$LOGIN" >/dev/null 2>&1 \
    && say "scratch project ${PROJECT_NUMBER} deleted" \
    || echo "[proof] WARN: could not delete scratch project ${PROJECT_NUMBER} -- delete it by hand" >&2
}
trap cleanup EXIT

PROJ="$(gh project create --owner "$LOGIN" --title "scratch-4166-iteration-proof-$$" --format json)" \
  || { echo "[proof] could not create the scratch project (needs the 'project' token scope)" >&2; exit 2; }
PROJECT_ID="$(jq -r .id <<<"$PROJ")"
PROJECT_NUMBER="$(jq -r .number <<<"$PROJ")"
say "scratch project ${PROJECT_NUMBER} (${PROJECT_ID})"

# --- the precondition the ceremony provisions into: all iterations completed ---
# Derived from TODAY so the split is the same whenever this runs; a fixed date
# would eventually drift and stop exercising the completed path.
DURATION=18
TODAY="$(date -u +%F)"
read -r S1 S2 S3 <<<"$(python3 - "$TODAY" "$DURATION" <<'PY'
import sys
from datetime import date, timedelta
today, duration = date.fromisoformat(sys.argv[1]), int(sys.argv[2])
# Three back-to-back iterations, the last ending 6 days ago: all completed,
# none active or upcoming -- exactly when the ceremony creates one.
first = today - timedelta(days=3 * duration + 6)
print(*[(first + timedelta(days=duration * k)).isoformat() for k in (0, 1, 2)])
PY
)"
say "scratch iterations: Sprint 1=${S1} Sprint 2=${S2} Sprint 3=${S3} (duration ${DURATION}, today ${TODAY})"

FIELD_ID="$(gh api graphql -f query='
mutation($pid:ID!,$d:Int!,$s1:Date!,$s2:Date!,$s3:Date!){
  createProjectV2Field(input:{projectId:$pid, dataType:ITERATION, name:"Sprint",
    iterationConfiguration:{startDate:$s1, duration:$d, iterations:[
      {title:"Sprint 1", startDate:$s1, duration:$d},
      {title:"Sprint 2", startDate:$s2, duration:$d},
      {title:"Sprint 3", startDate:$s3, duration:$d}]}}){
    projectV2Field { ... on ProjectV2IterationField { id } } } }' \
  -f pid="$PROJECT_ID" -F d="$DURATION" -f s1="$S1" -f s2="$S2" -f s3="$S3" \
  --jq .data.createProjectV2Field.projectV2Field.id)" \
  || { echo "[proof] could not create the Sprint iteration field" >&2; exit 2; }
say "Sprint field: ${FIELD_ID}"

# `gh api graphql -F` cannot carry a nested input object (it coerces to a
# scalar), and the iteration list is exactly that. Build the request body
# instead, which is the same path release_ceremony.sh uses.
graphql() { # graphql <query> <variables-json> [jq-filter]
  local body
  body="$(jq -n --arg q "$1" --argjson v "$2" '{query: $q, variables: $v}')"
  if [[ -n "${3:-}" ]]; then
    printf '%s' "$body" | gh api graphql --input - --jq "$3"
  else
    printf '%s' "$body" | gh api graphql --input - >/dev/null
  fi
}

read_config() {
  graphql 'query($fid:ID!){ node(id:$fid){ ... on ProjectV2IterationField {
      configuration { duration startDay
        iterations { id title startDate duration }
        completedIterations { id title startDate duration } } } } }' \
    "$(jq -n --arg fid "$FIELD_ID" '{fid: $fid}')" '.data.node.configuration'
}

update_field() { # update_field <iterationConfigurationInput JSON>
  graphql 'mutation($fid:ID!,$cfg:ProjectV2IterationFieldConfigurationInput!){
      updateProjectV2Field(input:{fieldId:$fid, iterationConfiguration:$cfg}){
        projectV2Field { ... on ProjectV2IterationField { id } } } }' \
    "$(jq -n --arg fid "$FIELD_ID" --argjson cfg "$1" '{fid: $fid, cfg: $cfg}')"
}

assign_item() { # assign_item <iterationId> <label>
  local item
  item="$(graphql 'mutation($pid:ID!,$t:String!){ addProjectV2DraftIssue(input:{projectId:$pid,title:$t}){
      projectItem { id } } }' "$(jq -n --arg pid "$PROJECT_ID" --arg t "$2" '{pid: $pid, t: $t}')" \
    '.data.addProjectV2DraftIssue.projectItem.id')"
  graphql 'mutation($pid:ID!,$iid:ID!,$fid:ID!,$v:String!){
      updateProjectV2ItemFieldValue(input:{projectId:$pid,itemId:$iid,fieldId:$fid,
        value:{iterationId:$v}}){ projectV2Item { id } } }' \
    "$(jq -n --arg pid "$PROJECT_ID" --arg iid "$item" --arg fid "$FIELD_ID" --arg v "$1" \
       '{pid: $pid, iid: $iid, fid: $fid, v: $v}')"
}

snapshot() {
  graphql 'query($pid:ID!){ node(id:$pid){ ... on ProjectV2 { items(first:100){ nodes {
      content { ... on DraftIssue { title } }
      fieldValueByName(name:"Sprint"){ ... on ProjectV2ItemFieldIterationValue { title } } } } } } }' \
    "$(jq -n --arg pid "$PROJECT_ID" '{pid: $pid}')" \
    '[.data.node.items.nodes[] | {item: .content.title, sprint: (.fieldValueByName.title // "<none>")}] | sort_by(.item)'
}

# The items query lags a write by a second or two (the first draft of this
# proof read an EMPTY board right after assigning three items and "passed"
# comparing nothing to nothing). Every snapshot used as evidence waits for the
# expected item count first.
snapshot_settled() { # snapshot_settled <expected-item-count>
  local snap i
  for i in $(seq 1 20); do
    snap="$(snapshot)"
    [[ "$(jq 'length' <<<"$snap")" == "$1" ]] && { printf '%s' "$snap"; return 0; }
    sleep 2
  done
  echo "[proof] WARN: board still shows $(jq 'length' <<<"$snap") item(s), expected $1" >&2
  printf '%s' "$snap"
  return 1
}

CONFIG="$(read_config)"
say "config: $(jq -c '{active: [.iterations[].title], completed: [.completedIterations[].title]}' <<<"$CONFIG")"
[[ "$(jq '.iterations | length' <<<"$CONFIG")" -eq 0 && "$(jq '.completedIterations | length' <<<"$CONFIG")" -eq 3 ]] \
  || { echo "[proof] the scratch project is not in the all-completed state this proof needs" >&2; exit 2; }

while read -r iter_id iter_title; do
  assign_item "$iter_id" "item-in-${iter_title// /-}"
done < <(jq -r '.completedIterations[] | "\(.id) \(.title)"' <<<"$CONFIG")

BEFORE="$(snapshot_settled 3)" \
  || { echo "[proof] the three test items never appeared -- cannot prove anything" >&2; exit 2; }
echo "--- items BEFORE:"; jq -c '.[]' <<<"$BEFORE"
[[ "$(jq '[.[] | select(.sprint != "<none>")] | length' <<<"$BEFORE")" -eq 3 ]] \
  || { echo "[proof] the test items did not all get a Sprint value -- cannot prove anything" >&2; exit 2; }

# =========================================================================
# 1. POSITIVE — the mutation the ceremony actually runs
# =========================================================================
say "TEST 1 (positive): the payload release_prereqs.py sprint-plan produces"
PLAN="$(printf '%s' "$CONFIG" | python3 "$PREREQS" sprint-plan --today "$TODAY")"
echo "--- plan: $(jq -c '{needed, provisionable, reason, new}' <<<"$PLAN")"
[[ "$(jq -r '.needed and .provisionable' <<<"$PLAN")" == "true" ]] \
  || { echo "[proof] sprint-plan did not ask for a sprint in the all-completed state" >&2; exit 2; }
NEW_TITLE="$(jq -r '.new.title' <<<"$PLAN")"
say "resubmitting $(jq '.iterations | length' <<<"$PLAN") iteration(s), $(jq '[.iterations[] | select(.id)] | length' <<<"$PLAN") with ids; new: ${NEW_TITLE}"

update_field "$(jq -c '{startDate, duration, iterations}' <<<"$PLAN")" \
  || fail "TEST 1: the positive mutation was rejected outright"

AFTER1="$(snapshot_settled 3)"
echo "--- items AFTER the positive mutation:"; jq -c '.[]' <<<"$AFTER1"
if [[ "$BEFORE" == "$AFTER1" ]]; then
  ok "TEST 1: every item's Sprint value is unchanged, including items in completed iterations"
else
  fail "TEST 1: item Sprint values CHANGED -- the ceremony must NOT create sprints"
  diff <(jq -c '.[]' <<<"$BEFORE") <(jq -c '.[]' <<<"$AFTER1") || true
fi
if read_config | jq -e --arg t "$NEW_TITLE" '[.iterations[].title] | index($t) != null' >/dev/null; then
  ok "TEST 1: '${NEW_TITLE}' was created and is active/upcoming (the line gate is now satisfied)"
else
  fail "TEST 1: '${NEW_TITLE}' was not created, or is not active/upcoming"
fi

# =========================================================================
# 2. NEGATIVE — drop the completed iterations, keep every other id
# =========================================================================
CONFIG2="$(read_config)"
NEW_ITER_ID="$(jq -r --arg t "$NEW_TITLE" '.iterations[] | select(.title == $t) | .id' <<<"$CONFIG2")"
assign_item "$NEW_ITER_ID" "item-in-${NEW_TITLE// /-}"
snapshot_settled 4 >/dev/null || true
say "TEST 2 (fault injection): omit completedIterations, keep the active iteration's id"
COMPLETED_TITLES="$(jq -r '[.completedIterations[].title] | join(", ")' <<<"$CONFIG2")"
update_field "$(jq -c '{startDate: ([.iterations[].startDate] | min), duration,
                        iterations: [.iterations[] | {id, title, startDate, duration}]}' <<<"$CONFIG2")"
AFTER2="$(snapshot_settled 4)"
echo "--- items AFTER dropping completedIterations:"; jq -c '.[]' <<<"$AFTER2"
WIPED2="$(jq -r '[.[] | select(.sprint == "<none>")] | length' <<<"$AFTER2")"
KEPT2="$(jq -r --arg t "$NEW_TITLE" '[.[] | select(.sprint == $t)] | length' <<<"$AFTER2")"
if [[ "$WIPED2" -eq 3 && "$KEPT2" -eq 1 ]]; then
  ok "TEST 2: exactly the 3 items in the dropped completed iterations (${COMPLETED_TITLES}) lost their value; the id-carrying one kept it"
else
  fail "TEST 2: expected 3 wiped / 1 kept, got ${WIPED2} wiped / ${KEPT2} kept -- this proof no longer discriminates, so TEST 1 proves nothing"
fi

# =========================================================================
# 3. NEGATIVE — resubmit with no ids at all
# =========================================================================
say "TEST 3 (fault injection): resubmit the same iterations with NO ids"
CONFIG3="$(read_config)"
update_field "$(jq -c '{startDate: ([.iterations[].startDate] | min), duration,
                        iterations: [.iterations[] | {title, startDate, duration}]}' <<<"$CONFIG3")"
AFTER3="$(snapshot_settled 4)"
echo "--- items AFTER an id-less resubmit:"; jq -c '.[]' <<<"$AFTER3"
LIVE3="$(jq -r '[.[] | select(.sprint != "<none>")] | length' <<<"$AFTER3")"
if [[ "$LIVE3" -eq 0 ]]; then
  ok "TEST 3: the last surviving value was wiped too -- omitting the ids recreates every iteration"
else
  fail "TEST 3: an id-less resubmit left ${LIVE3} value(s) -- this proof no longer discriminates"
fi

echo
if [[ $FAILURES -eq 0 ]]; then
  say "PROVEN: resubmitting every iteration (completed included) with its id preserves all item values; omitting either half does not."
  exit 0
fi
echo "[proof] ${FAILURES} check(s) failed -- sprint provisioning is NOT safe as written." >&2
exit 1
