#!/bin/bash
# SessionStart hook: GitHub identity wiring for interactive Claude sessions.
#
# Owner decision (2026-07-06): ad-hoc administrative actions performed in
# interactive Claude sessions run as myGPT-scrummaster-agent, so that the
# defined agents -- not the human owner's user -- author the activity.
#
# Local sessions:  export GH_TOKEN from SCRUMMASTER_AGENT_TOKEN in
#                  ~/.nyxGPT/config.ini so every gh command defaults to the
#                  scrummaster identity. Role scripts in scripts/agents/
#                  still override GH_TOKEN with their own role token.
# Remote sessions: the Claude Code on the web egress proxy injects the
#                  connected user's GitHub credentials into every
#                  api.github.com request and OVERRIDES any Authorization
#                  header, so agent PATs cannot take effect. Emit a warning
#                  into session context instead of pretending otherwise.
#
# This hook must never block session start: no -e, always exit 0.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  echo "ATTRIBUTION NOTICE (remote session): GitHub writes from this session are"
  echo "authored as the connected user, not as a nyxGPT agent -- the environment"
  echo "proxy overrides Authorization headers on api.github.com. Per project"
  echo "policy, prefer read-only GitHub access here and route writes through the"
  echo "GitHub Actions triggers (READY_FOR_NEXT_ISSUE, RETRY_IMPLEMENTATION,"
  echo "@claude mentions) or a local Claude Code session, which run as the"
  echo "proper agent identities."
  exit 0
fi

# --- Local session: default gh identity = myGPT-scrummaster-agent ---
CONFIG_FILE="${NYXGPT_CONFIG_FILE:-$HOME/.nyxGPT/config.ini}"

if [ -n "${GH_TOKEN:-}" ]; then
  echo "gh identity: GH_TOKEN already set in environment; leaving it untouched."
  exit 0
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "gh identity: $CONFIG_FILE not found; gh will use its own auth (likely the"
  echo "human owner). Add SCRUMMASTER_AGENT_TOKEN to the [github] section to give"
  echo "administrative sessions the scrummaster-agent identity."
  exit 0
fi

# Same key=value convention as scripts/agents/lib/gh_project.sh load_config
# (sections ignored, key matched case-insensitively, quotes stripped).
token="$(sed -nE 's/^[[:space:]]*[Ss][Cc][Rr][Uu][Mm][Mm][Aa][Ss][Tt][Ee][Rr]_[Aa][Gg][Ee][Nn][Tt]_[Tt][Oo][Kk][Ee][Nn][[:space:]]*=[[:space:]]*//p' "$CONFIG_FILE" | head -1 | tr -d '"' | tr -d '[:space:]')"

if [ -z "$token" ]; then
  echo "gh identity: SCRUMMASTER_AGENT_TOKEN not found in $CONFIG_FILE; gh will"
  echo "use its own auth (likely the human owner)."
  exit 0
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  printf 'export GH_TOKEN=%q\n' "$token" >> "$CLAUDE_ENV_FILE"
  echo "gh identity: myGPT-scrummaster-agent (GH_TOKEN wired from"
  echo "SCRUMMASTER_AGENT_TOKEN in $CONFIG_FILE). Administrative gh commands in"
  echo "this session are authored by the scrummaster agent. Role scripts in"
  echo "scripts/agents/ still apply their own role tokens."
fi

exit 0
