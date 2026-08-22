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
# `sed` writing to a temp file and copying back, rather than `sed -i`: GNU sed
# takes an optional suffix for `-i`, BSD/macOS sed requires one, so `sed -i
# "s|...|"` reads the script as the backup suffix there and exits non-zero
# under `set -e`. In the image this only ever runs under GNU sed -- but
# `tests/unit/test_docker_entrypoint.py` runs this script directly, so on a
# developer's Mac the regression test for this merge failed on the sed dialect
# rather than on the merge (#3983). `cat >` rather than `mv` keeps the file's
# inode, ownership and mode -- config.ini may be a bind mount.
if [ -n "${NYXGPT_AUTH_API_KEY:-}" ] && [ -f "$CONFIG_DIR/config.ini" ]; then
    merged="$(mktemp)"
    sed "s|^api_key[[:space:]]*=.*|api_key = ${NYXGPT_AUTH_API_KEY}|" \
        "$CONFIG_DIR/config.ini" > "$merged"
    cat "$merged" > "$CONFIG_DIR/config.ini"
    rm -f "$merged"
fi

exec uvicorn nyxgpt.app:app --host 0.0.0.0 --port 8000
