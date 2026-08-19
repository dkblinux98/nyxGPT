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
      AUTH_KEY=""

      if [ -f "$CONFIG_FILE" ]; then
        IFS=$'\t' read -r HOST PORT API_BASE AUTH_KEY < <("$SYS_PY" - <<'PY'
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
# The proxy in web/src/lib/apiProxy.ts attaches X-API-Key from
# NYXGPT_AUTH_API_KEY; without it every proxied call 401s the moment
# [auth] enabled is turned on (#3632).
try:
    auth_on = cfg.getboolean('auth', 'enabled', fallback=False)
except Exception:
    auth_on = False
api_key = cfg.get('auth', 'api_key', fallback='').strip() if auth_on else ''

print(f"{host}\t{port}\t{api_base}\t{api_key}")
PY
)
      fi

      export HOST="$HOST"
      export PORT="$PORT"
      if [ -n "$API_BASE" ]; then
        export NEXT_PUBLIC_API_BASE="$API_BASE"
      fi
      if [ -n "$AUTH_KEY" ]; then
        export NYXGPT_AUTH_API_KEY="$AUTH_KEY"
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

  # See ../nyxgpt-api.rb's caveats block for the full account (#3854): a
  # formula with a `service` block and no `caveats` leaves Homebrew's generated
  # "brew services start ..." line as the only instruction the operator gets,
  # and following it produces a stack with no Ollama, no Cassandra and no
  # observability. This formula needs the same message for the same reason --
  # the owner is told it twice, once per keg.
  #
  # The one difference: the `nyxgpt` CLI ships in the nyxgpt-api keg
  # (`bin.install_symlink venv/bin/nyxgpt` there), so a machine that installed
  # only the web formula does not have the command this text names. Say so,
  # and derive the counterpart's name from this one so the rc channel points at
  # `nyxgpt-api@<line>rc` and the stable channel at `nyxgpt-api`.
  def caveats
    api_formula = name.sub("nyxgpt-web", "nyxgpt-api")
    <<~EOS
      To start nyxGPT, run:
        nyxgpt up

      That brings up the whole stack -- this web UI, the API, Ollama, Cassandra
      and the observability services -- waits for it to report healthy, and
      prints the web UI URL. `nyxgpt down` stops it again, and `nyxgpt ops
      status` shows what is running.

      The `nyxgpt` command is installed by the #{api_formula} formula:
        brew install #{api_formula}

      If you were pointed at `brew services start #{name}`, note
      that it starts only this one service. It does not start the API this UI
      talks to, it does not install or start Ollama, it does not create the
      Cassandra container, and it does not start observability. A web UI
      brought up that way fails to load sessions. Use it to control this
      individual service once `nyxgpt up` has set the stack up -- not instead
      of it.

      `nyxgpt up` starts the Homebrew services for you, so they still restart
      at login.
    EOS
  end

  test do
    assert_predicate bin/"nyxgpt-web", :exist?
    assert_predicate libexec/".next", :exist?

    # File existence is also true of a keg that crash-loops the moment launchd
    # starts it, and this formula has shipped exactly that: `npm prune
    # --omit=dev` took typescript away, `next start` could not transpile
    # next.config.ts, and `.next` was present throughout (#3406). So start the
    # wrapper the service runs and require the server to answer (#3860) --
    # what the two assertions above cannot distinguish is a keg that serves
    # from one that dies in a restart loop.
    port = free_port
    # The wrapper reads $HOME/.nyxGPT/config.ini for its host/port, so writing
    # one is both how this test claims a free port and a test of that parsing.
    # `brew test` runs with HOME pointed at a sandbox, so this never touches a
    # real machine's config.
    ENV["HOME"] = testpath
    (testpath/".nyxGPT").mkpath
    (testpath/".nyxGPT/config.ini").write <<~INI
      [web]
      host = 127.0.0.1
      port = #{port}
    INI

    # Its own process group: the wrapper `exec`s npm, which runs `next` as a
    # child, so signalling the group is what actually stops the server.
    pid = spawn "/bin/bash", bin/"nyxgpt-web", pgroup: true
    begin
      code = "000"
      60.times do
        sleep 2
        # `|| true`: until the port is listening curl exits non-zero, which
        # shell_output would raise on -- a starting server is not a failure,
        # a server that never answers is (and stays "000" here).
        code = shell_output(
          "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:#{port}/ || true",
        ).strip
        break if code == "200"
      end
      assert_equal "200", code
    ensure
      Process.kill "TERM", -Process.getpgid(pid)
      Process.wait pid
    end
  end
end
