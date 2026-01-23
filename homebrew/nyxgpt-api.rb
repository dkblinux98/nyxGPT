class NyxgptApi < Formula
  desc "nyxGPT FastAPI backend (uvicorn)"
  homepage "http://127.0.0.1:8000/docs"
  tap = Tap.fetch("dkblinux98", "nyxgpt-local")
  url "file://#{tap.path}/dist/nyxgpt-api-1.0.0.md.tar.gz"
  sha256 "5308429da6dfa18a6c76a07db9e7a69fff64b3c80f7fa231b484c2a6ceca0b14"
  version "1.0.0.md"
  license "MIT"

  def install
    (bin/"nyxgpt-api").write <<~EOS
      #!/bin/bash
      set -euo pipefail

      CONFIG_FILE="$HOME/.nyxGPT/config.ini"
      SYS_PY="/usr/bin/python3"

      # Defaults
      REPO_DIR=""
      VENV_PY=""
      HOST="127.0.0.1"
      PORT="8000"

      if [ -f "$CONFIG_FILE" ]; then
        IFS=$'\t' read -r REPO_DIR VENV_PY HOST PORT < <("$SYS_PY" - <<'PY'
import configparser
import os

cfg = configparser.ConfigParser()
cfg.read(os.path.expanduser('~/.nyxGPT/config.ini'), encoding='utf-8')

repo_dir = cfg.get('paths', 'repo_dir', fallback='')
venv_py = cfg.get('paths', 'venv_python', fallback='')
host = cfg.get('api', 'host', fallback='127.0.0.1')
try:
    port = str(cfg.getint('api', 'port', fallback=8000))
except Exception:
    port = '8000'

print(f"{repo_dir}\t{venv_py}\t{host}\t{port}")
PY
)
      fi

      if [ -z "$REPO_DIR" ] || [ -z "$VENV_PY" ]; then
        echo "ERROR: repo_dir or venv_python not set in ~/.nyxGPT/config.ini" >&2
        exit 1
      fi

      if [ ! -x "$VENV_PY" ]; then
        echo "ERROR: venv_python is not executable: $VENV_PY" >&2
        exit 1
      fi

      if [ ! -d "$REPO_DIR" ]; then
        echo "ERROR: repo_dir does not exist: $REPO_DIR" >&2
        exit 1
      fi

      cd "$REPO_DIR"

      echo "nyxgpt-api wrapper starting" >&2
      echo "  repo_dir: $REPO_DIR" >&2
      echo "  venv_python: $VENV_PY" >&2
      echo "  host: $HOST" >&2
      echo "  port: $PORT" >&2

      trap 'echo "nyxgpt-api wrapper received SIGTERM" >&2' TERM
      trap 'echo "nyxgpt-api wrapper received SIGINT" >&2' INT

      "$VENV_PY" -m uvicorn nyxgpt.app:app --host "$HOST" --port "$PORT"
      RC=$?
      echo "nyxgpt-api uvicorn exited with code $RC" >&2
      exit $RC
    EOS
    system "chmod", "0755", bin/"nyxgpt-api"
  end

  service do
    run ["/bin/bash", opt_bin/"nyxgpt-api"]
    keep_alive true
    log_path var/"log/nyxgpt-api.log"
    error_log_path var/"log/nyxgpt-api.err.log"
  end
end
