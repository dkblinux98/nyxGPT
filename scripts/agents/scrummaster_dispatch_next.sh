#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib/gh_project.sh"

# Who any Slack escalation from this script is from (#3911). Set here rather
# than in the workflow because the *report* is the scrummaster's wherever it
# runs: `developer_pull_next_issue.yml` invokes this under the developer
# token, and a dispatch-block report signed by the developer agent would name
# the wrong agent. An explicit AGENT_ROLE in the environment still wins.
export AGENT_ROLE="${AGENT_ROLE:-scrum}"

usage() {
  cat <<'EOF'
Usage:
  scrummaster_dispatch_next.sh [--sprint-scoped]

Runs the #3665 fall-through dispatch loop: select the next eligible
Backlog candidate (developer_pull_next.sh -- the pull, #3883), attempt to
start it (scrummaster_attempt_start, lib/gh_project.sh), and on any skip
exclude that issue and retry with the next candidate -- so one bad-state
issue (e.g. an anomalous assignee, or a deliberate human hold) can no
longer become a permanent head-of-line block on the whole queue.

Selection left this file with #3883. What decides is now the sprint plan
the scrummaster grooms (#3908) plus the pull algorithm: plan order,
relationships eligibility, WIP limit, and a file-overlap check against
in-flight work. This script keeps what it was always actually for -- the
dispatch-pause backstops and the fall-through retry -- and the loop runs in
the developer agent's context, which is where the decision belongs.

Bounded by MAX_ATTEMPTS (default 25) so a systemic problem fails loudly
instead of looping forever.

Before selecting, checks the dispatch-pause backstop, which skips dispatch
entirely (paused=true) rather than selecting a candidate:
  - #3694 cross-issue infrastructure-anomaly pause backstop
    (cross_issue_anomaly_pause_gate, lib/gh_project.sh): while an open,
    unresolved cross-issue anomaly tracking record exists on the release
    issue. Resumes once it is resolved (OWNER `RESOLVE_ANOMALY` comment) or
    its detection window elapses.
It posts/updates its own loud report on the release tracking issue.

The #3687 unresolved-escalation pause (>=2 inferred escalations stopped ALL
dispatch) is RETIRED (#4134). It inferred "escalated" from "open, assigned to
the owner, not in an exempt lane" and was wrong twice; on 2026-08-19 ordinary
merges tripped it and the queue sat idle ~10 hours while three claimable
issues went unworked. Escalation is now explicit -- an escalated issue carries
the `Escalation` label and is skipped here by the start guard
(classify_backlog_claim_state -> `escalated`), so the affected work is paused
and unrelated work keeps dispatching. What the count really stood in for,
escalations piling up with nothing done about the cause, is now addressed by
the blast-radius investigation every escalation performs.

Prints, in $GITHUB_OUTPUT format (`key=value` / `key<<EOF ... EOF`):
  paused=<true if the pause backstop skipped dispatch, else false>
  pause_reason=<"cross_issue_anomaly" | empty when not paused>
  next_issue=<issue number, or empty if nothing started>
  tried<<NYXGPT_TRIED_EOF
  <newline-separated "SKIPPED #<n> reason=<reason>..." lines, may be empty>
  NYXGPT_TRIED_EOF

Options:
  --sprint-scoped  Passed through to developer_pull_next.sh.
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
#
# The pull's reasoning is written to PULL_EXPLAIN_FILE when the caller sets
# one, so the dispatch comment can quote *why* this issue and not the one
# above it. A wrong pull has to be visible to be corrected (D-004).
#
# The pull's stderr is NOT discarded. It used to end in `2>/dev/null || echo
# ""`, which collapsed three different outcomes into one empty string: a
# genuinely empty backlog, a misconfiguration, and an outright crash. The
# caller then reported all three as "Nothing eligible to pull" -- a plausible
# answer that happened to be wrong.
#
# That cost six weeks. From 2026-08-19 every dispatch died inside
# `developer_pull_next.sh`, and the one line that said why --
#
#     [pull] No active sprint on field '' -- conservative stop (#3706).
#
# -- went to /dev/null on every run. The queue selected nothing and looked
# merely idle. nyxGPT's own Definition of Done requires ops state to be
# observable; a dispatcher that hides its reasoning is the inverse of that.
#
# So: stderr flows to the job log, and a non-zero exit is reported as a
# non-zero exit rather than silently becoming "no candidate". The fall-through
# behaviour is unchanged -- an empty result still means "try the next one" --
# but it is now distinguishable from a failure.
_select_next_candidate() {
  local exclude="$1" sprint_scoped="$2" out="" rc=0
  local args=()
  if [[ "$sprint_scoped" == "1" ]]; then args+=("--sprint-scoped"); fi
  if [[ -n "${PULL_EXPLAIN_FILE:-}" ]]; then args+=("--explain" "$PULL_EXPLAIN_FILE"); fi

  out="$(EXCLUDE_ISSUES="$exclude" "$DIR/developer_pull_next.sh" "${args[@]}")" || rc=$?

  if (( rc != 0 )); then
    echo "[select] developer_pull_next.sh exited ${rc} -- treating as no candidate." >&2
    echo "[select] Its own reason is in the [pull] lines above; an empty result here" >&2
    echo "[select] is NOT the same as an empty backlog." >&2
    out=""
  fi
  printf '%s' "$out"
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
# with stubbed _cross_issue_anomaly_check/_select_next_candidate/
# scrummaster_attempt_start.
scrummaster_dispatch_next() {
  local sprint_scoped="${1:-0}"
  local exclude="" tried="" started=""
  local i next_issue output rc reason

  if ! _cross_issue_anomaly_check; then
    echo "paused=true"
    echo "pause_reason=cross_issue_anomaly"
    echo "next_issue="
    printf 'tried<<NYXGPT_TRIED_EOF\n%sNYXGPT_TRIED_EOF\n' ""
    # Names the actual cause (#3694 accuracy requirement).
    _notify_dispatch_block "anomaly-paused" \
      "Scrummaster dispatch paused -- an unresolved cross-issue infrastructure anomaly is open on the release tracking issue (#3694)." \
      "Resolve the anomaly (owner comment RESOLVE_ANOMALY on the release tracking issue, or let its detection window elapse); dispatch resumes automatically."
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

  if [[ -z "$started" && -n "$tried" ]]; then
    _notify_dispatch_block "queue-blocked" \
      "Scrummaster dispatch started nothing -- every eligible Backlog candidate was unclaimable." \
      "Inspect the unclaimable candidate(s) reported on the release tracking issue and resolve manually (#3665 decision matrix)."
  fi
}

# #3695: human-channel (Slack DM) notification for the dispatch-wide
# terminal outcomes above (cross-issue-anomaly pause backstop (#3694),
# queue fully blocked)
# -- both are head-of-line blocks on the whole sprint-autopilot queue, not
# tied to any single issue, so they are attached to (and de-duplicated
# against) RELEASE_ISSUE_NUMBER, the same target sprint_autopilot_kick
# reports to. Silently skipped if RELEASE_ISSUE_NUMBER is not configured.
#
# NOTIFICATIONS, not escalations (#4134 reviewed every owner-facing path):
# they are about the QUEUE, not about any issue, and the release tracking
# issue's own label is load-bearing for the ceremony and the gates. Nothing
# changes hands -- both states clear on their own -- so there is nothing to
# relabel or reassign. Best-effort: never fails the caller's dispatch loop.
_notify_dispatch_block() {
  local state="$1" diagnosis="$2" action="$3"
  [[ -n "${RELEASE_ISSUE_NUMBER:-}" ]] || return 0
  notify_human_escalation "$RELEASE_ISSUE_NUMBER" "$state" "$diagnosis" "$action" \
    "${RELEASE_ISSUE_NUMBER}:${state}" \
    || true
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
