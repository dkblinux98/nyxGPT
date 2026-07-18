#!/usr/bin/env bash
#
# reconcile_closed_status.sh — project-hygiene sweep.
#
# The normal flow (review_accept_and_merge.sh) closes a merged issue and moves
# its Status to "In Review" (assigned to the human owner) for stakeholder
# acceptance. Issues that were hand-driven, or that missed that step, can end up
# CLOSED while their board Status is still "Backlog" (or "In Progress") — they
# then clutter the Backlog column even though the work is done.
#
# This script finds every CLOSED issue whose Status is Backlog or In Progress
# and moves it to In Review + assigns the human owner, mirroring the merge flow.
# It changes nothing when DRY_RUN=true (the default) — it just lists what it
# would touch. Terminal statuses (In Review / For Release) are left untouched.
#
# Env:
#   DRY_RUN   "true" (default) = preview only; "false" = apply changes.
#   NYXGPT_CONFIG_FILE / the standard agent config vars (via load_config).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/gh_project.sh"

DRY_RUN="${DRY_RUN:-true}"

load_config
require_gh_auth
project_id="$(get_project_id)"
echo "[reconcile] Project id: ${project_id} (dry_run=${DRY_RUN})" >&2

read -r -d '' QUERY <<'GQL' || true
query($project:ID!, $after:String){
  node(id:$project){
    ... on ProjectV2{
      items(first:100, after:$after){
        pageInfo{ hasNextPage endCursor }
        nodes{
          content{
            __typename
            ... on Issue { number state stateReason title }
          }
          fieldValues(first:50){
            nodes{
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                field{ ... on ProjectV2SingleSelectField { name } }
                name
              }
            }
          }
        }
      }
    }
  }
}
GQL

targets_file="$(mktemp)"
page_file="$(mktemp)"
after=""

while : ; do
  if [[ -n "$after" ]]; then
    graphql "$QUERY" -F project="$project_id" -f after="$after" > "$page_file"
  else
    graphql "$QUERY" -F project="$project_id" > "$page_file"
  fi

  STATUS_FIELD="$STATUS_FIELD" STATUS_BACKLOG="$STATUS_BACKLOG" \
  STATUS_IN_PROGRESS="$STATUS_IN_PROGRESS" \
  python3 - "$page_file" <<'PY' >> "$targets_file"
import json, os, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
status_field = os.environ.get("STATUS_FIELD", "Status")
stuck = {os.environ.get("STATUS_BACKLOG", "Backlog"),
         os.environ.get("STATUS_IN_PROGRESS", "In Progress")}
for it in d["data"]["node"]["items"]["nodes"]:
    c = it.get("content") or {}
    if c.get("__typename") != "Issue":
        continue
    if c.get("state") != "CLOSED":
        continue
    status = None
    for fv in (it.get("fieldValues") or {}).get("nodes", []):
        if fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue":
            f = fv.get("field") or {}
            if f.get("name") == status_field:
                status = fv.get("name")
                break
    if status in stuck:
        title = (c.get("title") or "").replace("\t", " ")[:60]
        print(f"{c['number']}\t{status}\t{c.get('stateReason')}\t{title}")
PY

  has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$page_file")"
  after="$(jq -r '.data.node.items.pageInfo.endCursor' "$page_file")"
  [[ "$has_next" == "true" ]] || break
done

count="$(grep -c . "$targets_file" || true)"
echo "[reconcile] ${count} closed issue(s) stuck in Backlog/In Progress:" >&2
if [[ "$count" -gt 0 ]]; then
  while IFS=$'\t' read -r num status reason title; do
    [[ -n "${num:-}" ]] || continue
    echo "  #${num}  [${status}]  (${reason})  ${title}" >&2
  done < "$targets_file"
fi

if [[ "$DRY_RUN" != "false" ]]; then
  echo "[reconcile] DRY RUN — no changes made. Re-run with dry_run=false to apply." >&2
  rm -f "$targets_file" "$page_file"
  exit 0
fi

while IFS=$'\t' read -r num status reason title; do
  [[ -n "${num:-}" ]] || continue
  echo "[reconcile] #${num}: ${status} -> ${STATUS_IN_REVIEW}; assign @${HUMAN_OWNER}" >&2
  set_issue_status "$num" "$STATUS_IN_REVIEW" \
    || echo "[reconcile] WARN: failed to set Status on #${num}" >&2
  gh issue edit "$num" --repo "${REPO_OWNER}/${REPO_NAME}" --add-assignee "${HUMAN_OWNER}" >/dev/null \
    || echo "[reconcile] WARN: failed to assign #${num}" >&2
done < "$targets_file"

rm -f "$targets_file" "$page_file"
echo "[reconcile] Done — reconciled ${count} issue(s)." >&2
