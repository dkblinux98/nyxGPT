#!/usr/bin/env bash
# Wrapper script for collect_workflow_logs.py
# Collect and analyze historical GitHub Actions workflow run logs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/collect_workflow_logs.py"

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed" >&2
    exit 1
fi

# Check if Python script exists
if [[ ! -f "$PYTHON_SCRIPT" ]]; then
    echo "Error: $PYTHON_SCRIPT not found" >&2
    exit 1
fi

# Pass all arguments to Python script
exec python3 "$PYTHON_SCRIPT" "$@"
