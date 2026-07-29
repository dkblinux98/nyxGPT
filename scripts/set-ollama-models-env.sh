#!/usr/bin/env bash
set -euo pipefail

# nyxGPT native Ollama shared-model-store env setter (see issue #3431)
#
# Points the native Homebrew `ollama` service at the same model store
# Compose/Terraform's `ollama` container uses (~/.nyxGPT/volumes/ollama/models,
# bind-mounted from /root/.ollama/models) via `launchctl setenv OLLAMA_MODELS`
# -- never a symlink (owner constraint). `launchctl setenv` only applies to
# the current login session's launchd domain, so this also runs as a
# RunAtLoad LaunchAgent (ops/launchagents/com.nyxgpt.ollama-env.plist) to
# reapply it at every login, alongside Homebrew's own `ollama` LaunchAgent
# (also RunAtLoad), which then inherits it when `ollama serve` starts.

OLLAMA_MODELS_DIR="$HOME/.nyxGPT/volumes/ollama/models"
mkdir -p "$OLLAMA_MODELS_DIR"
exec launchctl setenv OLLAMA_MODELS "$OLLAMA_MODELS_DIR"
