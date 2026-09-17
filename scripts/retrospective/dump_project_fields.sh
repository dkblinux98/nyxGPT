#!/usr/bin/env bash
set -euo pipefail

# scripts/retrospective/dump_project_fields.sh
#
# Dumps per-issue Project v2 field assignments (Sprint, Module, Status) plus
# the Sprint iteration calendar to data/project_fields.json. Read-only against
# the Project; writes only the JSON snapshot.
#
# This used to be inline in retro_project_fields_dump.yml, which made it the
# one retrospective input with no script behind it: a refresh session that
# fell back to generating data locally could produce every other file and
# structurally could not produce this one (it needs the project-scoped token
# and the Project GraphQL). It now lives here so both the standalone dump and
# retro_data_refresh.yml run the same code.
#
# Usage:
#   dump_project_fields.sh [output-path]
#     output-path defaults to scripts/retrospective/data/project_fields.json
#
# Env:
#   GH_TOKEN         token with read access to the Project (required)
#   PROJECT_OWNER    login that owns the Project v2 board (required)
#   PROJECT_NUMBER   the board's number (required)

_die() { echo "[dump-project-fields] ERROR: $*" >&2; exit 1; }
_log() { echo "[dump-project-fields] $*" >&2; }

usage() {
  awk 'NR <= 2 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
    "${BASH_SOURCE[0]}"
}
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/data/project_fields.json}"

[[ -n "${GH_TOKEN:-}" ]] || _die "GH_TOKEN is required"
[[ -n "${PROJECT_OWNER:-}" ]] || _die "PROJECT_OWNER is required"
[[ -n "${PROJECT_NUMBER:-}" ]] || _die "PROJECT_NUMBER is required"
command -v gh >/dev/null 2>&1 || _die "gh is required"
command -v jq >/dev/null 2>&1 || _die "jq is required"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$(dirname "$OUT")"

FIELDS_Q='query($login:String!,$number:Int!){
  user(login:$login){ projectV2(number:$number){
    fields(first:30){ nodes{
      ... on ProjectV2IterationField { name configuration {
        iterations { title startDate duration }
        completedIterations { title startDate duration } } }
    } }
  } } }'
gh api graphql -f query="$FIELDS_Q" \
  -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER" \
  --jq '{sprints: [.data.user.projectV2.fields.nodes[]
          | select(.name == "Sprint")
          | (.configuration.completedIterations + .configuration.iterations)[]
          | {title, startDate, duration}]}' > "$TMP/sprints.json"

ITEMS_Q='query($login:String!,$number:Int!,$after:String){
  user(login:$login){ projectV2(number:$number){
    items(first:100, after:$after){
      pageInfo{ hasNextPage endCursor }
      nodes{
        type
        content{ ... on Issue { number } ... on PullRequest { number } }
        fieldValues(first:20){ nodes{
          ... on ProjectV2ItemFieldSingleSelectValue {
            name field { ... on ProjectV2SingleSelectField { name } } }
          ... on ProjectV2ItemFieldIterationValue {
            title startDate field { ... on ProjectV2IterationField { name } } }
        } }
      }
    }
  } } }'
after=""
: > "$TMP/items.ndjson"
while :; do
  if [ -n "$after" ]; then
    resp="$(gh api graphql -f query="$ITEMS_Q" -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER" -F after="$after")"
  else
    resp="$(gh api graphql -f query="$ITEMS_Q" -F login="$PROJECT_OWNER" -F number="$PROJECT_NUMBER")"
  fi
  echo "$resp" | jq -c '.data.user.projectV2.items.nodes[]
    | select(.content.number != null)
    | {type, number: .content.number,
       fields: [.fieldValues.nodes[] | select(.field.name != null)
                | {field: .field.name, value: (.name // .title), start: (.startDate // null)}]}' \
    >> "$TMP/items.ndjson"
  has_next="$(echo "$resp" | jq -r '.data.user.projectV2.items.pageInfo.hasNextPage')"
  after="$(echo "$resp" | jq -r '.data.user.projectV2.items.pageInfo.endCursor')"
  [ "$has_next" = "true" ] || break
done

jq -s --slurpfile cal "$TMP/sprints.json" \
  '{generated_at: (now | todate), sprints: $cal[0].sprints, items: .}' \
  "$TMP/items.ndjson" > "$OUT"
_log "wrote $OUT: $(jq -c '{items: (.items | length), sprints: (.sprints | length)}' "$OUT")"
