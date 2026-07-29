#!/usr/bin/env bash
set -euo pipefail

# nyxGPT native Ollama shared-model-store env setter (see issue #3431)
#
# Points the native Homebrew `ollama` service at the same model store
# Compose/Terraform's `ollama` container uses (~/.nyxGPT/volumes/ollama/models,
# bind-mounted from /root/.ollama/models) via `launchctl setenv OLLAMA_MODELS`
# -- never a symlink (owner constraint). `launchctl setenv` only applies to
# the current login session's launchd domain, so this also runs as a
# RunAtLoad LaunchAgent (ops/launchagents/com.nyxgpt.ollama-env.plist).
#
# Homebrew's own `ollama` LaunchAgent is a *separate* RunAtLoad agent, and
# launchd does not guarantee relative ordering between independent RunAtLoad
# agents. If it wins that race, `ollama serve` starts before this script has
# set OLLAMA_MODELS and silently falls back to Ollama's default
# `~/.ollama/models` store for the rest of the session. Restarting the brew
# service here unconditionally, every login, closes that race in both
# orderings: if Homebrew's agent already started `ollama serve` with the
# wrong env, this forces it to restart with the correct one; if it hasn't
# started yet, `brew services restart` just starts it. Cheap and idempotent.

OLLAMA_MODELS_DIR="$HOME/.nyxGPT/volumes/ollama/models"
mkdir -p "$OLLAMA_MODELS_DIR"
launchctl setenv OLLAMA_MODELS "$OLLAMA_MODELS_DIR"

# launchd agents run with a minimal PATH; add Homebrew's common prefixes so
# `brew` is discoverable (mirrors scripts/run-web.sh's node/npm handling).
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-}"

if command -v brew >/dev/null 2>&1; then
  brew services restart ollama >/dev/null 2>&1 || true
fi
