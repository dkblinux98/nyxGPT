class NyxgptWeb < Formula
  desc "nyxGPT local web UI (Next.js) service wrapper"
  homepage "https://github.com/dkblinux98/nyxGPT"

  # NOTE:
  # This file lives in the nyxGPT repo as the *source template*.
  # The `nyxgpt ops install` command will copy it into your Homebrew tap at:
  #   $(brew --repo dkblinux98/nyxgpt-local)/Formula/nyxgpt-web.rb
  #
  # During that install step, ops replaces the placeholders below with the
  # real URL + sha256 of the generated tarball and the project version
  # (see _install_homebrew_web in src/nyxgpt/ops.py). That tarball vendors
  # the web/ source tree (minus gitignored node_modules/.next); `install`
  # below npm-installs and builds it fresh inside this keg, so the installed
  # app never depends on the repo checkout (#3406).
  url "__NYXGPT_WEB_URL__"
  sha256 "__NYXGPT_WEB_SHA256__"
  version "__VERSION__"
  depends_on "node"

  def install
    system "npm", "ci"
    system "npm", "run", "build"
    # Drop devDependencies now that the production build exists to slim the
    # keg. But `next start` re-reads next.config.ts at boot and needs
    # TypeScript to transpile it, so restore just that one devDep afterward --
    # pruning it caused "Cannot find module 'typescript'" and a crash loop at
    # runtime (#3406).
    system "npm", "prune", "--omit=dev"
    system "npm", "install", "--no-save", "typescript"
    # `Dir["*"]` skips dotfiles/dotdirs -- the `.next` production build
    # output needs its own explicit copy, or `npm run start` below would
    # have nothing to serve.
    libexec.install Dir["*"]
    libexec.install ".next"

    (bin/"nyxgpt-web").write <<~SH
      #!/usr/bin/env bash
      set -euo pipefail

      # launchd/Homebrew services often run with a minimal PATH.
      # Ensure Homebrew bins are available so node/npm can be found.
      export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

      # Prefer the Node installed by Homebrew.
      export PATH="#{Formula["node"].opt_bin}:#{Formula["node"].opt_libexec}/bin:${PATH}"

      CONFIG_FILE="$HOME/.nyxGPT/config.ini"
      SYS_PY="/usr/bin/python3"

      # Defaults -- this keg is self-contained (own build, own node_modules),
      # so only the listen address/port/API override come from config.ini,
      # never a repo_dir pointing at a live checkout (#3406).
      HOST="127.0.0.1"
      PORT="3000"
      API_BASE=""

      if [ -f "$CONFIG_FILE" ]; then
        IFS=$'\t' read -r HOST PORT API_BASE < <("$SYS_PY" - <<'PY'
import configparser
import os

cfg = configparser.ConfigParser()
cfg.read(os.path.expanduser('~/.nyxGPT/config.ini'), encoding='utf-8')

host = cfg.get('web', 'host', fallback='127.0.0.1')
try:
    port = str(cfg.getint('web', 'port', fallback=3000))
except Exception:
    port = '3000'
api_base = cfg.get('web', 'api_base_url', fallback='')

print(f"{host}\t{port}\t{api_base}")
PY
)
      fi

      export HOST="$HOST"
      export PORT="$PORT"
      if [ -n "$API_BASE" ]; then
        export NEXT_PUBLIC_API_BASE="$API_BASE"
      fi

      echo "nyxgpt-web starting (self-contained Cellar build)" >&2
      echo "  host: $HOST" >&2
      echo "  port: $PORT" >&2

      cd "#{libexec}"
      exec npm run start
    SH
    # Best-effort exec bit for anyone invoking the wrapper directly. Homebrew's
    # post-install Cleaner resets keg scripts to 0444 regardless, so this does
    # not survive -- which is why the service below launches the wrapper via
    # `/bin/bash` (like nyxgpt-api.rb): bash reads the script without needing an
    # exec bit, avoiding the launchd exec failure (error 78) (#3406).
    system "chmod", "0755", bin/"nyxgpt-web"
  end

  service do
    run ["/bin/bash", opt_bin/"nyxgpt-web"]
    keep_alive true

    # Homebrew's conventional log locations (arm64):
    #   /opt/homebrew/var/log/nyxgpt-web.log
    #   /opt/homebrew/var/log/nyxgpt-web.err.log
    #
    # If you want everything consolidated under ~/.nyxGPT/logs, you can symlink:
    #   ln -sf /opt/homebrew/var/log/nyxgpt-web.log ~/.nyxGPT/logs/nyxgpt-web.log
    #   ln -sf /opt/homebrew/var/log/nyxgpt-web.err.log ~/.nyxGPT/logs/nyxgpt-web.err.log
    log_path var/"log/nyxgpt-web.log"
    error_log_path var/"log/nyxgpt-web.err.log"
  end

  test do
    # We only validate that the wrapper script and the production build it
    # runs both exist. (The actual Next.js runtime is exercised by
    # integration tests in the repo.)
    assert_predicate bin/"nyxgpt-web", :exist?
    assert_predicate libexec/".next", :exist?
  end
end
