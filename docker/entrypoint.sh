#!/bin/sh
set -eu

CONFIG_DIR="${HOME:-/root}/.nyxGPT"
mkdir -p "$CONFIG_DIR"

# Tells nyxgpt.app's startup bind-security check (P6-1) that the `--host
# 0.0.0.0` below is this container's own network namespace, not the host's --
# real exposure is gated by Docker's port-publish (NYXGPT_BIND_ADDR) or the
# Kubernetes Service type, neither of which is visible from inside the
# process. Set unconditionally: both Compose and Kubernetes run this same
# entrypoint/image.
export NYXGPT_CONTAINER_RUNTIME=1

if [ -f /etc/nyxgpt/config/config.ini ]; then
    cp /etc/nyxgpt/config/config.ini "$CONFIG_DIR/config.ini"
fi

# Merge the secret-provided API key (Kubernetes Secret / docker `-e`) into the
# config file, since nyxgpt reads auth.api_key from config.ini rather than
# the environment. NYXGPT_AUTH_API_KEY itself should be derived from the
# host's ~/.nyxGPT/config.ini (the single source of truth for this secret)
# via `nyxgpt ops env-sync`, not set independently here.
if [ -n "${NYXGPT_AUTH_API_KEY:-}" ] && [ -f "$CONFIG_DIR/config.ini" ]; then
    sed -i "s|^api_key[[:space:]]*=.*|api_key = ${NYXGPT_AUTH_API_KEY}|" "$CONFIG_DIR/config.ini"
fi

# Same treatment for the GlitchTip DSN (#3990). GlitchTip mints a project key
# per install, so the DSN cannot live in the committed k8s/configmap.yaml the
# way `[ollama] base_url` does -- the Kubernetes install provisions it into the
# nyxgpt-secrets Secret (`_k8s_provision_glitchtip`) and it arrives here as an
# environment variable, while nyxgpt itself reads [error_tracking] dsn from
# config.ini. Without this the api reported NOTHING to the in-cluster
# GlitchTip: the section was absent entirely, so error tracking initialised
# inert and every exception was dropped silently.
#
# Empty is a no-op, not an erase: a `--skip-observability` install (and every
# Compose deploy, which patches config.docker.ini directly instead) leaves the
# variable unset, and blanking a DSN that config.ini already carries would
# turn error tracking off behind the operator's back.
if [ -n "${NYXGPT_ERROR_TRACKING_DSN:-}" ] && [ -f "$CONFIG_DIR/config.ini" ]; then
    sed -i "s|^dsn[[:space:]]*=.*|dsn = ${NYXGPT_ERROR_TRACKING_DSN}|" "$CONFIG_DIR/config.ini"
fi

exec uvicorn nyxgpt.app:app --host 0.0.0.0 --port 8000
