#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

usage() {
  cat <<'EOF'
Usage:
  scrummaster_dispatch_next.sh [--sprint-scoped]

Runs the #3665 fall-through dispatch loop: select the next eligible
Backlog candidate (scrummaster_next_issue.sh --select-only), attempt to
start it (scrummaster_attempt_start, lib/gh_project.sh), and on any skip
exclude that issue and retry with the next candidate -- so one bad-state
issue (e.g. an anomalous assignee, or a deliberate human hold) can no
longer become a permanent head-of-line block on the whole queue.

Bounded by MAX_ATTEMPTS (default 25) so a systemic problem fails loudly
instead of looping forever.

Before selecting, checks two dispatch-pause backstops, either of which
skips dispatch entirely (paused=true) rather than selecting a candidate:
  - #3687 unresolved-escalation pause backstop (escalation_pause_gate,
    lib/gh_project.sh): with >=2 unresolved escalated issues. Resumes
    automatically on a later run once the count drops below 2.
  - #3694 cross-issue infrastructure-anomaly pause backstop
    (cross_issue_anomaly_pause_gate, lib/gh_project.sh): while an open,
    unresolved cross-issue anomaly tracking record exists on the release
    issue. Resumes once it is resolved (OWNER `RESOLVE_ANOMALY` comment) or
    its detection window elapses.
Either gate posts/updates its own loud report on the release tracking
issue; pause_reason distinguishes which one fired.

Prints, in $GITHUB_OUTPUT format (`key=value` / `key<<EOF ... EOF`):
  paused=<true if either pause backstop skipped dispatch, else false>
  pause_reason=<"escalation" | "cross_issue_anomaly" | empty when not paused>
  next_issue=<issue number, or empty if nothing started>
  tried<<NYXGPT_TRIED_EOF
  <newline-separated "SKIPPED #<n> reason=<reason>..." lines, may be empty>
  NYXGPT_TRIED_EOF

Options:
  --sprint-scoped  Passed through to scrummaster_next_issue.sh.
  -h, --help       Show this help
EOF
}

SPRINT_SCOPED=0
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--sprint-scoped" ]]; then
  SPRINT_SCOPED=1
fi

MAX_ATTEMPTS="${MAX_ATTEMPTS:-25}"

# Selects the next Backlog candidate not in `exclude` (comma-separated
# issue numbers). Split out so tests can stub it without a real gh/GraphQL
# round trip.
_select_next_candidate() {
  local exclude="$1" sprint_scoped="$2"
  local args=()
  [[ "$sprint_scoped" == "1" ]] && args+=("--sprint-scoped")
  EXCLUDE_ISSUES="$exclude" "$DIR/scrummaster_next_issue.sh" --select-only "${args[@]}" 2>/dev/null || echo ""
}

# Wraps escalation_pause_gate (lib/gh_project.sh). Split out so tests can
# stub it without a real gh round trip. Returns 0 if dispatch may proceed,
# 1 if paused.
_escalation_pause_check() {
  escalation_pause_gate
}

# Wraps cross_issue_anomaly_pause_gate (lib/gh_project.sh, #3694). Split out
# so tests can stub it without a real gh round trip. Returns 0 if dispatch
# may proceed, 1 if paused.
_cross_issue_anomaly_check() {
  cross_issue_anomaly_pause_gate
}

# Runs the fall-through loop described above and prints paused=/next_issue=/
# tried to stdout in $GITHUB_OUTPUT format. Split out from the script's
# direct-execution guard so tests can source this file and call it directly
# with stubbed _escalation_pause_check/_select_next_candidate/
# scrummaster_attempt_start.
scrummaster_dispatch_next() {
  local sprint_scoped="${1:-0}"
  local exclude="" tried="" started=""
  local i next_issue output rc reason

  if ! _escalation_pause_check; then
    echo "paused=true"
    echo "pause_reason=escalation"
    echo "next_issue="
    printf 'tried<<NYXGPT_TRIED_EOF\n%sNYXGPT_TRIED_EOF\n' ""
    return 0
  fi
  if ! _cross_issue_anomaly_check; then
    echo "paused=true"
    echo "pause_reason=cross_issue_anomaly"
    echo "next_issue="
    printf 'tried<<NYXGPT_TRIED_EOF\n%sNYXGPT_TRIED_EOF\n' ""
    return 0
  fi
  echo "paused=false"
  echo "pause_reason="

  for ((i = 1; i <= MAX_ATTEMPTS; i++)); do
    next_issue="$(_select_next_candidate "$exclude" "$sprint_scoped")"
    [[ -n "$next_issue" ]] || break

    set +e
    output="$(scrummaster_attempt_start "$next_issue")"
    rc=$?
    set -e
    echo "$output" >&2

    if [[ "$rc" -eq 0 ]]; then
      started="$next_issue"
      break
    fi

    reason="$(echo "$output" | grep -o 'SKIPPED #[0-9]* reason=.*' | tail -1)"
    [[ -n "$reason" ]] || reason="SKIPPED #$next_issue reason=unknown (rc=$rc)"
    tried="${tried}${reason}"$'\n'
    exclude="${exclude:+$exclude,}$next_issue"
  done

  echo "next_issue=${started}"
  printf 'tried<<NYXGPT_TRIED_EOF\n%sNYXGPT_TRIED_EOF\n' "$tried"
}

# Only run for real when executed directly -- tests source this file to
# reuse _select_next_candidate/scrummaster_dispatch_next with stubs, which
# must not trigger a live config load / gh auth check.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  load_config

  if [[ -z "${GH_TOKEN:-}" ]]; then
    if [[ -n "${SCRUMMASTER_AGENT_TOKEN:-}" ]]; then
      export GH_TOKEN="$SCRUMMASTER_AGENT_TOKEN"
    else
      echo "[error] SCRUMMASTER_AGENT_TOKEN not found in config file: $CONFIG_FILE" >&2
      exit 1
    fi
  fi

  require_gh_auth

  scrummaster_dispatch_next "$SPRINT_SCOPED"
fi
