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

  # `brew uninstall` does not stop a formula's service first, and Homebrew has
  # no uninstall hook that could -- it deletes the keg's files out from under a
  # running process, which goes on serving from memory. `brew untap` then makes
  # the formula name unresolvable, so `brew services stop` has nothing left to
  # act on: the operator ends up with services they cannot name to stop (#3859).
  # nyxGPT also installs LaunchAgents of its own (`com.nyxgpt.*`) that Homebrew
  # never knew about and so could never have removed. The teardown command
  # clears all three populations; this block is the one place the operator is
  # actually standing when they decide to remove nyxGPT (#3854).
  def caveats
    <<~EOS
      Start the stack with:
        nyxgpt up

      Before removing nyxGPT, run the wrapped teardown FIRST:
        nyxgpt ops uninstall

      It stops and deregisters everything the install put on this machine --
      the Homebrew services, the com.nyxgpt.* LaunchAgents nyxGPT installed
      itself, and the containers. Your data in ~/.nyxGPT is preserved.

      Only then remove the artifacts:
        brew uninstall #{name}
        brew untap #{tap || "<your-tap>"}

      Uninstalling first leaves #{name} running from deleted files on its
      port, with no supported command left to stop it.
    EOS
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
