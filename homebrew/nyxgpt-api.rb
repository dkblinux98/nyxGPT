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

  # The vendored venv contains prebuilt wheels whose compiled extensions carry
  # `@rpath` dylib IDs and no header padding (e.g. tiktoken's Rust `_tiktoken`
  # .so). Homebrew's default post-install relocation rewrites those IDs to the
  # absolute keg path and aborts with `MachO::HeaderPadError` (the load command
  # can't grow without relinking). `preserve_rpath` tells Homebrew to leave
  # `@rpath` IDs untouched -- correct here since Python loads these extensions
  # by file path, so the dylib ID is cosmetic (#3406).
  preserve_rpath

  def install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    venv = libexec/"venv"

    # `--without-pip` is load-bearing (#3753). A plain `python -m venv` runs
    # `ensurepip --upgrade --default-pip` inside the new venv, and ensurepip
    # bootstraps pip from wheels vendored in the `python@3.12` keg -- the one
    # step of this install that depends on Homebrew-managed keg state rather
    # than on our own tarball. On a stock Homebrew macOS it exited 1 and took
    # the whole `brew install` down with it. Nothing here needs ensurepip:
    # pip is placed into the venv directly below, by the same Homebrew python
    # that is already a declared dependency.
    system python, "-m", "venv", "--without-pip", venv
    # `pip --python` (pip 22.3+) installs into *another* interpreter's
    # environment, so the venv gets a real pip -- with the venv's own shebang
    # and install scheme -- without ensurepip ever running. PEP 668's
    # externally-managed marker on the Homebrew keg does not apply: the
    # install target is the venv, which is not externally managed.
    # `--python` is a top-level pip option and must precede the subcommand:
    # placed after `install`, pip exits with "The --python option must be
    # placed before the pip subcommand name" and takes `brew install` down.
    system python, "-m", "pip", "--python", venv/"bin/python", "install", "--upgrade", "pip"
    system venv/"bin/pip", "install", buildpath

    # config_wizard builds its schema from example.config.ini at import time
    # (#3388), so `import nyxgpt.app` -- the wrapper, the `test` block, and the
    # always-on self-heal watchdog -- needs it present. A venv has no repo root
    # above the package, so drop the template next to the installed package
    # where _resolve_example_config_path() finds it with no env var (#3406).
    site_packages = venv/"lib/python3.12/site-packages/nyxgpt"
    cp buildpath/"example.config.ini", site_packages/"example.config.ini"

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

      # `exec` replaces this wrapper's process image with uvicorn instead of
      # running it as a child: without it, launchd/`brew services stop`'s
      # SIGTERM lands on this bash process, whose old `trap ... TERM` only
      # logged the signal without forwarding it or killing the child, so
      # uvicorn never actually stopped -- it was silently orphaned (still
      # bound to the port, still serving the *old* in-memory code) once bash
      # exited or was force-killed after launchd's grace period. The next
      # `brew services start`/`restart` then raced that orphan for the port,
      # so a rebuilt keg's fix could silently never take effect on the
      # running stack (#3472). `exec` here makes uvicorn the tracked PID
      # directly, so a stop signal reaches it and actually shuts it down --
      # matching how nyxgpt-web.rb's wrapper `exec`s into `npm run start`.
      exec "#{venv}/bin/python3" -m uvicorn nyxgpt.app:app --host "$HOST" --port "$PORT"
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
