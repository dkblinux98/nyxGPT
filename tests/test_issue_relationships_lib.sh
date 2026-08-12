#!/usr/bin/env bash
set -uo pipefail

# tests/test_issue_relationships_lib.sh
# Tests for the native issue-relationship helpers (#3731) in
# scripts/agents/lib/gh_project.sh: blocking_issues, mark_issue_blocked_by,
# transitive_blocked_by_issues, transitive_blocking_issues,
# related_feature_of and the native-first/transitive _issue_open_gate_refs.
#
# `gh` is stubbed against an in-memory dependency graph, so no network calls
# happen; the real issue_relationships.py subprocess still runs end to end.
#
# Usage: bash tests/test_issue_relationships_lib.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAILURES=0

_assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" != "$actual" ]]; then
    echo "[FAIL] $desc: expected '$expected', got '$actual'" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "[ok] $desc"
  fi
}

# shellcheck disable=SC2034
REPO_OWNER="test-owner"
# shellcheck disable=SC2034
REPO_NAME="test-repo"

# shellcheck source=/dev/null
source "$ROOT_DIR/scripts/agents/lib/gh_project.sh"

# --- Stub graph -------------------------------------------------------
# BLOCKED_BY[n] = space-separated issues blocking n
# BLOCKS[n]     = space-separated issues n blocks
# STATES[n]     = open|closed (default open)
# BODIES[n]     = issue body (for the retired-prose fallback)
declare -A BLOCKED_BY=()
declare -A BLOCKS=()
declare -A STATES=()
declare -A BODIES=()

# POSTed links are recorded to a file: mark_issue_blocked_by is normally
# called inside command substitution (a subshell), where a plain variable
# assignment would be lost -- the pitfall documented in test_gh_project_lib.sh.
POST_FILE="$(mktemp)"
trap 'rm -f "$POST_FILE"' EXIT

_emit_dependency_list() {
  # $1 = space-separated issue numbers -> the REST dependency array shape
  local n out="[]"
  for n in $1; do
    out="$(jq -c --argjson n "$n" --arg s "${STATES[$n]:-open}" '. + [{number: $n, state: $s}]' <<<"$out")"
  done
  echo "$out"
}

# Minimal `gh api` stub covering the three endpoints the helpers touch:
# GET .../dependencies/{blocked_by,blocking}, GET .../issues/N, and the
# POST that creates a link.
gh() {
  local args=("$@") arg path="" method="GET" issue_id=""
  local i
  for ((i = 0; i < ${#args[@]}; i++)); do
    arg="${args[$i]}"
    case "$arg" in
      api) ;;
      -X) method="${args[$((i + 1))]}"; ((i++)) ;;
      -F) issue_id="${args[$((i + 1))]#issue_id=}"; ((i++)) ;;
      --jq) ((i++)) ;;
      repos/*) path="$arg" ;;
    esac
  done

  local num="${path#*issues/}"
  num="${num%%/*}"

  if [[ "$method" == "POST" ]]; then
    # The API rejects a duplicate link; the helper must never get here for one.
    if [[ " ${BLOCKED_BY[$num]:-} " == *" ${issue_id} "* ]]; then
      return 1
    fi
    BLOCKED_BY["$num"]="${BLOCKED_BY[$num]:-} ${issue_id}"
    echo "$num <- $issue_id" >>"$POST_FILE"
    return 0
  fi

  case "$path" in
    */dependencies/blocked_by)
      _emit_dependency_list "${BLOCKED_BY[$num]:-}" | jq -r '.[].number'
      ;;
    */dependencies/blocking)
      _emit_dependency_list "${BLOCKS[$num]:-}" | jq -r '.[].number'
      ;;
    *)
      # `gh api repos/.../issues/N --jq '.id'` (database id — the stub uses
      # the issue number, which is all the caller round-trips) or `.body`.
      local wants="${args[*]}"
      if [[ "$wants" == *".body"* ]]; then
        printf '%s\n' "${BODIES[$num]:-}"
      else
        echo "$num"
      fi
      ;;
  esac
}

# _issue_open_state is the state oracle _issue_open_gate_refs consults.
_issue_open_state() {
  local s="${STATES[$1]:-open}"
  echo "${s^^}"
}

_reset_graph() {
  BLOCKED_BY=()
  BLOCKS=()
  STATES=()
  BODIES=()
  : >"$POST_FILE"
}

# --- blocking_issues / blocked_by_issues ------------------------------
_reset_graph
BLOCKS[3733]="3730"
BLOCKED_BY[3730]="3733"
_assert_eq "blocking_issues lists what an issue blocks" \
  "3730" "$(blocking_issues 3733)"
_assert_eq "blocked_by_issues lists what blocks an issue" \
  "3733" "$(blocked_by_issues 3730)"
_assert_eq "an unrelated issue has no blocking edges" "" "$(blocking_issues 999)"

# --- mark_issue_blocked_by (the relationship WRITE) -------------------
_reset_graph
mark_issue_blocked_by 3730 3733
_assert_eq "the link is created" "3730 <- 3733" "$(cat "$POST_FILE")"
_assert_eq "the graph records it" "3733" "$(blocked_by_issues 3730)"

: >"$POST_FILE"
rc=0
mark_issue_blocked_by 3730 3733 || rc=$?
_assert_eq "re-linking an existing dependency succeeds" "0" "$rc"
_assert_eq "re-linking posts nothing (idempotent)" "" "$(cat "$POST_FILE")"

# --- transitivity -----------------------------------------------------
# 3740 blocks 3733 blocks 3730: a failure filed against a failure gates the
# original issue too.
_reset_graph
BLOCKED_BY[3730]="3733"
BLOCKED_BY[3733]="3740"
BLOCKS[3740]="3733"
BLOCKS[3733]="3730"
_assert_eq "transitive_blocked_by_issues walks the whole chain" \
  "3733
3740" "$(transitive_blocked_by_issues 3730)"
_assert_eq "transitive_blocking_issues walks the other direction" \
  "3730
3733" "$(transitive_blocking_issues 3740)"
_assert_eq "an unblocked issue has an empty gate" "" "$(transitive_blocked_by_issues 3740)"

# A cycle must terminate rather than hang the promotion sweep.
_reset_graph
BLOCKED_BY[1]="2"
BLOCKED_BY[2]="1"
_assert_eq "a dependency cycle terminates and excludes the root" \
  "2" "$(transitive_blocked_by_issues 1)"

# --- related_feature_of: native first, prose only as fallback ---------
_reset_graph
BLOCKS[3733]="3730"
BODIES[3733]="Related feature: #99"
_assert_eq "a native edge wins over a stale body marker" \
  "3730" "$(related_feature_of 3733 "${BODIES[3733]}")"

_reset_graph
BODIES[3733]="Related feature: #99"
_assert_eq "a historical issue falls back to the retired marker" \
  "99" "$(related_feature_of 3733 "${BODIES[3733]}")"

_reset_graph
_assert_eq "an issue related to nothing resolves to nothing" \
  "" "$(related_feature_of 3730 "a plain feature issue")"

_reset_graph
BODIES[3733]="Related feature: #99"
_assert_eq "the body is fetched when the caller does not supply one" \
  "99" "$(related_feature_of 3733)"

# --- _issue_open_gate_refs: native, transitive, state-aware -----------
# 100 <- 200 <- 300, all open: the whole chain gates 100.
_reset_graph
BLOCKED_BY[100]="200"
BLOCKED_BY[200]="300"
_assert_eq "open gates are reported transitively" \
  "200
300" "$(_issue_open_gate_refs 100 "")"

# The direct blocker closed: traversal stops there, so 300 no longer gates.
STATES[200]="closed"
_assert_eq "a closed blocker ends the walk (its own blockers are moot)" \
  "" "$(_issue_open_gate_refs 100 "")"

# Native edges suppress the retired prose parser entirely.
_reset_graph
BLOCKED_BY[100]="200"
_assert_eq "prose gates are ignored when native edges exist" \
  "200" "$(_issue_open_gate_refs 100 "Blocked by: #900")"

# No native edges: the prose fallback still gates historical issues.
_reset_graph
_assert_eq "prose gates still apply to issues with no native edges" \
  "900" "$(_issue_open_gate_refs 100 "Blocked by: #900")"

_reset_graph
STATES[900]="closed"
_assert_eq "a closed prose gate does not park an issue" \
  "" "$(_issue_open_gate_refs 100 "Blocked by: #900")"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "All tests passed."
  exit 0
else
  echo "$FAILURES test(s) failed." >&2
  exit 1
fi
