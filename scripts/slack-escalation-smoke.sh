#!/usr/bin/env bash
#
# Executed evidence that an escalation DM really is sent by the agent that
# raised it (#3911, D-006) -- against the live Slack API, not a stub.
#
# WHY THIS EXISTS, AND WHY A UNIT TEST IS NOT ENOUGH
#
# `notify_human_escalation` degrades on purpose: any Slack failure is a
# `_warn` + `return 0`, because losing the agent loop to an unconfigured chat
# integration would be worse than the escalation it was reporting. #3695's
# tests stub `curl`, so they prove the *decision* -- which token, which
# fallback, which marker -- and are blind to whether Slack will honour it.
#
# The open question they cannot answer is a property of the workspace, not of
# this code: **may an agent's user token open a DM with the owner?** A token
# holding `chat:write` but not the scope Slack wants for a fresh IM would fail
# every time, the bot fallback would quietly cover it, and the feature would
# be dead while every test stayed green. That is exactly how #3974 hid for two
# months (JSON sent to four methods, two of which never accepted it), and it
# is why #3911 was reopened: green about the degradation path is not green
# about the feature.
#
# So this asks the live API, twice, in both directions:
#
#   default        each agent token DMs the owner with NO BOT TOKEN PRESENT.
#                  There is nothing to fall back to, so a marker comment can
#                  only mean that identity's own token was accepted.
#   --prove-it-fails
#                  a deliberately invalid agent token WITH the bot token
#                  present. The DM must still land, and the record must say
#                  the bot sent it on the agent's behalf. A fallback nothing
#                  exercises is a fallback nobody knows is broken.
#
# GitHub is fully stubbed (`gh`, `issue_comment`, the dedup lookup): the only
# real network call is to Slack. Nothing is written to any issue.
#
# `workflow_dispatch` only, like its sibling `slack-huddle-smoke.py`: each run
# DMs the owner for real, and an escalation channel that cries wolf on every
# push is a worse outcome than the one this guards.

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/agents/lib/gh_project.sh"

PROVE_IT_FAILS=0
[[ "${1:-}" == "--prove-it-fails" ]] && PROVE_IT_FAILS=1

FAILURES=0
_fail() {
  echo "  FAIL  $1" >&2
  FAILURES=$((FAILURES + 1))
}
_ok() { echo "  ok    $1"; }

# --- the environment the real function reads -------------------------------
REPO_OWNER="${REPO_OWNER:-dkblinux98}"
REPO_NAME="${REPO_NAME:-nyxGPT}"
HUMAN_OWNER="${HUMAN_OWNER:-dkblinux98}"
DEV_AGENT="${DEV_AGENT:-myGPT-developer-agent}"
REVIEW_AGENT="${REVIEW_AGENT:-myGPT-review-agent}"
SCRUM_AGENT="${SCRUM_AGENT:-myGPT-scrummaster-agent}"

# Captured before anything else: the case runner below rewrites the three
# SLACK_USER_TOKEN_* variables to isolate one identity per DM, so the values
# the workflow passed in have to be held somewhere it does not touch.
REAL_TOKEN_DEV="${SLACK_USER_TOKEN_DEV:-}"
REAL_TOKEN_REVIEW="${SLACK_USER_TOKEN_REVIEW:-}"
REAL_TOKEN_SCRUM="${SLACK_USER_TOKEN_SCRUM:-}"

if [[ -z "${SLACK_USER_ID:-}" ]]; then
  echo "::error::SLACK_USER_ID is not set -- there is no owner to DM, so this job would" >&2
  echo "::error::pass by doing nothing. That is the failure mode it exists to catch." >&2
  exit 1
fi

# --- GitHub is stubbed; Slack is not ---------------------------------------
# Nothing here may write to a real issue. `issue_comment` is the function's
# record-of-success, so capturing it is also how we observe the outcome.
MARKERS=()
issue_comment() {
  MARKERS+=("$2")
  return 0
}
# The dedup lookup would otherwise `gh api` a real issue, and would suppress
# the second and third DM of this run as duplicates.
_slack_notify_recent() { return 1; }
gh() {
  echo "::error::the smoke reached the real \`gh\` (args: $*) -- it must not touch GitHub" >&2
  return 1
}

_run_case() {
  local role="$1" token="$2"
  MARKERS=()
  AGENT_ROLE="$role"
  SLACK_USER_TOKEN_DEV="" SLACK_USER_TOKEN_REVIEW="" SLACK_USER_TOKEN_SCRUM=""
  case "$role" in
    dev) SLACK_USER_TOKEN_DEV="$token" ;;
    review) SLACK_USER_TOKEN_REVIEW="$token" ;;
    scrum) SLACK_USER_TOKEN_SCRUM="$token" ;;
  esac
  export AGENT_ROLE SLACK_USER_TOKEN_DEV SLACK_USER_TOKEN_REVIEW SLACK_USER_TOKEN_SCRUM

  notify_human_escalation "3911" "slack-identity-smoke" \
    "Smoke test for #3911 -- no action needed; this run proves an escalation DM arrives from the agent that raised it." \
    "None. Ignore this message." \
    "3911:slack-identity-smoke:${role}:$$"

  printf '%s' "${MARKERS[0]:-}"
}

# ---------------------------------------------------------------------------
if [[ "$PROVE_IT_FAILS" -eq 1 ]]; then
  echo "Planting an invalid agent token; the bot fallback must carry the DM"
  if [[ -z "${SLACK_BOT_TOKEN:-}" ]]; then
    echo "::error::SLACK_BOT_TOKEN is required for the fault-injection half -- it is the" >&2
    echo "::error::fallback under test." >&2
    exit 1
  fi
  marker="$(_run_case dev "xoxp-this-token-is-not-valid")"
  if [[ -z "$marker" ]]; then
    _fail "a refused agent token dropped the escalation instead of falling back to the bot"
  else
    _ok "the DM still reached the owner when the agent's identity was refused"
  fi
  if [[ "$marker" == *"on behalf of @${DEV_AGENT}"* ]]; then
    _ok "the record says the bot sent it on the developer agent's behalf"
  else
    _fail "the record does not name the fallback (marker: ${marker:-<none>})"
  fi

  if [[ "$FAILURES" -gt 0 ]]; then
    echo "The fallback is broken: ${FAILURES} assertion(s) failed." >&2
    exit 1
  fi
  echo "The bot fallback is live, and says so in the record."
  exit 0
fi

# Default half: no bot token at all, so only the agent's own identity can
# possibly deliver. Cleared here rather than left to the workflow, so the
# guarantee holds however this is invoked.
SLACK_BOT_TOKEN=""
export SLACK_BOT_TOKEN

echo "DMing @${HUMAN_OWNER} once per agent identity, with no bot token to fall back on"
ran=0
for spec in "dev:${DEV_AGENT}:${REAL_TOKEN_DEV}" \
  "review:${REVIEW_AGENT}:${REAL_TOKEN_REVIEW}" \
  "scrum:${SCRUM_AGENT}:${REAL_TOKEN_SCRUM}"; do
  role="${spec%%:*}"
  rest="${spec#*:}"
  login="${rest%%:*}"
  token="${rest#*:}"
  if [[ -z "$token" ]]; then
    echo "  skip  ${role}: no token configured for this run"
    continue
  fi
  ran=$((ran + 1))
  marker="$(_run_case "$role" "$token")"
  if [[ -z "$marker" ]]; then
    _fail "${role}: the DM did not go out under @${login}'s own identity (see the [warning] above -- with no bot token, this is the agent token being refused)"
  elif [[ "$marker" == *"as @${login}"* ]]; then
    _ok "${role}: Slack accepted the DM as @${login}"
  else
    _fail "${role}: delivered, but not attributed to @${login} (marker: ${marker})"
  fi
done

if [[ "$ran" -eq 0 ]]; then
  echo "::error::no agent tokens were configured -- this job would have passed without" >&2
  echo "::error::sending anything, which is the outcome it exists to prevent." >&2
  exit 1
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "${FAILURES} identity assertion(s) failed." >&2
  exit 1
fi
echo "Every escalation DM arrived under the identity that raised it."
