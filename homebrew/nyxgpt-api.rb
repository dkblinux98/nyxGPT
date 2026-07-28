class NyxgptApi < Formula
  desc "nyxGPT FastAPI backend (uvicorn)"
  homepage "http://127.0.0.1:8000/docs"
  tap = Tap.fetch("dkblinux98", "nyxgpt-local")
  # This file is the *source template*; url, sha256, and version are stamped
  # with real values by `nyxgpt ops install` when it copies the formula into
  # the local tap (see _install_homebrew_api in src/nyxgpt/ops.py). The
  # tarball vendors pyproject.toml + src/nyxgpt/ -- `install` below builds a
  # self-contained venv for it inside this keg, so the installed app never
  # depends on the repo checkout or an editable `.venv` (#3406).
  url "file://#{tap.path}/dist/nyxgpt-api-__VERSION__.tar.gz"
  sha256 "__SHA256__"
  version "__VERSION__"
  license "MIT"

  depends_on "python@3.12"

  def install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    venv = libexec/"venv"
    system python, "-m", "venv", venv
    system venv/"bin/pip", "install", "--upgrade", "pip"
    system venv/"bin/pip", "install", buildpath

    (bin/"nyxgpt-api").write <<~EOS
      #!/bin/bash
      set -euo pipefail

      # launchd starts brew services with a minimal PATH (no /opt/homebrew/bin),
      # so docker/kubectl would be invisible to the API's self-heal, deploy, and
      # canary features without this.
      export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

      CONFIG_FILE="$HOME/.nyxGPT/config.ini"
      SYS_PY="/usr/bin/python3"

      # Defaults -- this keg is self-contained (own venv, own vendored source),
      # so only the listen address/port come from config.ini, never a
      # repo_dir/venv_python pointing at a live checkout (#3406).
      HOST="127.0.0.1"
      PORT="8000"

      if [ -f "$CONFIG_FILE" ]; then
        IFS=$'\t' read -r HOST PORT < <("$SYS_PY" - <<'PY'
import configparser
import os

cfg = configparser.ConfigParser()
cfg.read(os.path.expanduser('~/.nyxGPT/config.ini'), encoding='utf-8')

host = cfg.get('api', 'host', fallback='127.0.0.1')
try:
    port = str(cfg.getint('api', 'port', fallback=8000))
except Exception:
    port = '8000'

print(f"{host}\t{port}")
PY
)
      fi

      echo "nyxgpt-api starting (self-contained Cellar venv)" >&2
      echo "  host: $HOST" >&2
      echo "  port: $PORT" >&2

      trap 'echo "nyxgpt-api wrapper received SIGTERM" >&2' TERM
      trap 'echo "nyxgpt-api wrapper received SIGINT" >&2' INT

      "#{venv}/bin/python3" -m uvicorn nyxgpt.app:app --host "$HOST" --port "$PORT"
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

  test do
    # We only validate that the venv and its uvicorn install exist -- the
    # actual API is exercised by integration tests in the repo.
    assert_predicate libexec/"venv/bin/python3", :exist?
    system libexec/"venv/bin/python3", "-c", "import nyxgpt.app"
  end
end
