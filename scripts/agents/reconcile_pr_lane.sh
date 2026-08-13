#!/usr/bin/env bash
#
# reconcile_pr_lane.sh — sweep merged/closed PR cards into the terminal PR
# lane (STATUS_CLOSED). The backstop half of the #3742 lane invariant.
#
# The merge flow (review_accept_and_merge.sh) and the pull_request:closed
# handler (pr_close_project_status.sh) stamp PR cards at the moment they
# leave review. This sweep exists for everything those two could not reach:
# the strays that predate the invariant (13 + 3 hand-swept on 2026-08-10, 10
# on 2026-08-13), and any card whose stamp failed against a flaky API.
#
# Only PULL REQUEST items are considered — issues are never touched, so this
# can never disturb the acceptance queue. Open PRs are never touched either.
#
# Env:
#   SOURCE_STATUS  Optional. When set, only PR cards currently in this exact
#                  Status are swept (e.g. "In Review"). Unset/empty = every
#                  merged/closed PR card whose Status is not already the
#                  target, which is what the one-time backfill wants.
#   TARGET_STATUS  Status to move swept PR cards to. Default: STATUS_CLOSED.
#   DRY_RUN        "true" (default) = list only; "false" = apply.
#   MAX_PAGES      Project item pages to scan (default 200).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/gh_project.sh"

DRY_RUN="${DRY_RUN:-true}"
SOURCE_STATUS="${SOURCE_STATUS:-}"

load_config
require_gh_auth
require_cmd jq
require_cmd python3

TARGET_STATUS="${TARGET_STATUS:-$STATUS_CLOSED}"

project_id="$(get_project_id)"

# Validate the target option exists before touching anything — a renamed or
# missing board option must fail loudly, not silently sweep nothing.
target_opt="$(single_select_option_id "$STATUS_FIELD" "$TARGET_STATUS")"
[[ -n "$target_opt" && "$target_opt" != "null" ]] \
  || _die "Unknown ${STATUS_FIELD} option: '${TARGET_STATUS}'"

echo "[pr-lane-sweep] project=${project_id} source='${SOURCE_STATUS:-<any active lane>}' target='${TARGET_STATUS}' dry_run=${DRY_RUN}" >&2

read -r -d '' QUERY <<'GQL' || true
query($project:ID!, $after:String){
  node(id:$project){
    ... on ProjectV2{
      items(first:100, after:$after){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id
          content{
            __typename
            ... on PullRequest { number state title }
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
trap 'rm -f "$targets_file" "$page_file"' EXIT

after=""
pages=0
max_pages="${MAX_PAGES:-200}"

while : ; do
  if [[ -n "$after" ]]; then
    graphql "$QUERY" -F project="$project_id" -f after="$after" > "$page_file"
  else
    graphql "$QUERY" -F project="$project_id" > "$page_file"
  fi

  STATUS_FIELD="$STATUS_FIELD" SOURCE_STATUS="$SOURCE_STATUS" \
  TARGET_STATUS="$TARGET_STATUS" \
  python3 - "$page_file" <<'PY' >> "$targets_file"
import json
import os
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
status_field = os.environ.get("STATUS_FIELD", "Status")
source = os.environ.get("SOURCE_STATUS") or ""
target = os.environ["TARGET_STATUS"]

for item in data["data"]["node"]["items"]["nodes"]:
    content = item.get("content") or {}
    if content.get("__typename") != "PullRequest":
        continue
    if content.get("state") not in ("MERGED", "CLOSED"):
        continue

    status = None
    for value in (item.get("fieldValues") or {}).get("nodes", []):
        if value.get("__typename") != "ProjectV2ItemFieldSingleSelectValue":
            continue
        if (value.get("field") or {}).get("name") == status_field:
            status = value.get("name")
            break

    if source:
        # Narrow mode: only the named lane.
        if status != source:
            continue
    elif status in (None, "", target):
        # Backfill mode: everything not already parked in the terminal lane.
        # A card with no Status at all is left alone -- it was never put in
        # an active lane, so there is nothing stranded to reconcile.
        continue

    title = (content.get("title") or "").replace("\t", " ").replace("\n", " ")[:70]
    print(f"{item['id']}\t{content['number']}\t{content['state']}\t{status or ''}\t{title}")
PY

  pages=$((pages + 1))
  has_next="$(jq -r '.data.node.items.pageInfo.hasNextPage' "$page_file")"
  after="$(jq -r '.data.node.items.pageInfo.endCursor // empty' "$page_file")"
  [[ "$has_next" == "true" && -n "$after" ]] || break
  if [[ "$pages" -ge "$max_pages" ]]; then
    _warn "Stopped after ${max_pages} pages (MAX_PAGES) — re-run to continue."
    break
  fi
done

count="$(grep -c . "$targets_file" || true)"
echo "[pr-lane-sweep] ${count} merged/closed PR card(s) to move -> '${TARGET_STATUS}':" >&2
while IFS=$'\t' read -r _item num state status title; do
  [[ -n "${num:-}" ]] || continue
  echo "  PR #${num}  [${state}]  Status='${status:-<unset>}'  ${title}" >&2
done < "$targets_file"

if [[ "$DRY_RUN" != "false" ]]; then
  echo "[pr-lane-sweep] DRY RUN — no changes made. Re-run with DRY_RUN=false to apply." >&2
  exit 0
fi

moved=0
failed=0
while IFS=$'\t' read -r item_id num state status title; do
  [[ -n "${item_id:-}" ]] || continue
  if set_field_with_retry "$item_id" "$STATUS_FIELD" "$TARGET_STATUS"; then
    echo "[pr-lane-sweep] PR #${num} (${state}) '${status:-<unset>}' -> ${TARGET_STATUS}" >&2
    moved=$((moved + 1))
  else
    _warn "Failed to move PR #${num} to '${TARGET_STATUS}'"
    failed=$((failed + 1))
  fi
done < "$targets_file"

echo "[pr-lane-sweep] Done — ${moved} moved, ${failed} failed." >&2
[[ "$failed" -eq 0 ]]
