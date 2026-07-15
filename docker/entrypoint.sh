#!/bin/sh
set -eu

CONFIG_DIR="${HOME:-/root}/.nyxGPT"
mkdir -p "$CONFIG_DIR"

if [ -f /etc/nyxgpt/config/config.ini ]; then
    cp /etc/nyxgpt/config/config.ini "$CONFIG_DIR/config.ini"
fi

# Merge the secret-provided API key (Kubernetes Secret / docker `-e`) into the
# config file, since nyxgpt reads auth.api_key from config.ini rather than
# the environment.
if [ -n "${NYXGPT_AUTH_API_KEY:-}" ] && [ -f "$CONFIG_DIR/config.ini" ]; then
    sed -i "s|^api_key[[:space:]]*=.*|api_key = ${NYXGPT_AUTH_API_KEY}|" "$CONFIG_DIR/config.ini"
fi

exec uvicorn nyxgpt.app:app --host 0.0.0.0 --port 8000
