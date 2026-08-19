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

    # Round 2 of #3753. With ensurepip gone the install died one step later,
    # inside pip's own startup:
    #
    #   File ".../pip/_vendor/truststore/_macos.py", line 22, in <module>
    #     _mac_version_info = tuple(map(int, _mac_version.split(".")))
    #   ValueError: invalid literal for int() with base 10: ''
    #
    # `platform.mac_ver()` returns no release in this build environment
    # (`_mac_ver_xml()` could not read SystemVersion.plist, so mac_ver() fell
    # through to its default empty release), and two things pip does on every
    # install parse that string unguarded: truststore, its default TLS backend
    # since pip 24.2, and `packaging.tags.mac_platforms`, which computes the
    # wheel tags to resolve against. So pip cannot start at all -- not even
    # with `--no-index`, since `InstallCommand.run` builds its session before
    # it looks at the flag. pip means to degrade gracefully here but only
    # catches `ImportError`, so a `ValueError` out of the module body escapes
    # and no combination of pip options reaches the fallback.
    #
    # Repair the value rather than chase the cause: the reason mac_ver() is
    # empty is macOS-side and not observable from here, but the recipe only
    # needs it to be answerable. A `sitecustomize` on PYTHONPATH runs at
    # interpreter startup, so one file covers every interpreter that asks --
    # this python running `pip download`, the venv python that runs pip out of
    # the downloaded wheel, and pip's build-isolation subprocesses. It lives in
    # buildpath, so it is gone once the keg is built and nothing ships it.
    shim = buildpath/"brew-build-shim"
    shim.mkpath
    (shim/"sitecustomize.py").write <<~'PY'
      # nyxgpt-mac-ver-shim: begin (#3753)
      """Repair platform.mac_ver() for every tool this build runs.

      `site` imports this at interpreter startup because the formula's install
      block puts its directory on PYTHONPATH; see that block for the failure
      it exists to prevent.
      """
      import os
      import platform
      import sys


      def _is_version(value):
          """True for the dotted-integer string mac_ver() promises to return."""
          return bool(value) and all(part.isdigit() for part in value.split("."))


      def _chain_to_the_real_sitecustomize():
          """Run the sitecustomize this interpreter would otherwise have imported.

          PYTHONPATH precedes site-packages, so this file shadows a brewed
          python's own sitecustomize if it has one. Dropping that silently
          would change the build environment in ways unrelated to the fix.
          """
          here = os.path.dirname(os.path.abspath(__file__))
          for entry in sys.path:
              directory = entry or os.curdir
              candidate = os.path.join(directory, "sitecustomize.py")
              if os.path.abspath(directory) == here or not os.path.isfile(candidate):
                  continue
              with open(candidate, "rb") as handle:
                  source = handle.read()
              exec(compile(source, candidate, "exec"), {"__file__": candidate})
              return


      def _repair_mac_ver():
          """Give mac_ver() a usable release when the OS lookup came back empty."""
          try:
              current = platform.mac_ver()[0]
          except Exception:
              current = ""
          if _is_version(current):
              return

          try:
              import subprocess

              release = subprocess.run(
                  ["/usr/bin/sw_vers", "-productVersion"],
                  capture_output=True,
                  text=True,
                  timeout=30,
                  check=False,
              ).stdout.strip()
          except Exception:
              release = ""

          if not _is_version(release):
              # A floor, not a guess at the real version: >= 10.8 clears
              # truststore's minimum, >= 10.16 is what makes it load
              # Security.framework by absolute path instead of find_library,
              # and 11.0 is the tag every Apple Silicon wheel is published
              # under -- so pip still resolves binary wheels rather than
              # falling back to building each one from source.
              release = "11.0"

          machine = platform.machine()
          platform.mac_ver = lambda *args, **kwargs: (release, ("", "", ""), machine)
          print(
              "nyxgpt: platform.mac_ver() returned no release in this build "
              "environment; reporting " + release + " so pip can start (#3753)",
              file=sys.stderr,
          )


      # Guarded so the shim can be imported and exercised by the test suite
      # without patching the interpreter it is being read into.
      if __name__ == "sitecustomize":
          try:
              _chain_to_the_real_sitecustomize()
          except Exception as exc:
              print("nyxgpt: sitecustomize chain skipped: %r" % (exc,), file=sys.stderr)
          if sys.platform == "darwin":
              _repair_mac_ver()
      # nyxgpt-mac-ver-shim: end
    PY
    ENV.prepend_path "PYTHONPATH", shim

    # `--without-pip` is load-bearing (#3753). A plain `python -m venv` runs
    # `ensurepip --upgrade --default-pip` inside the new venv, and ensurepip
    # bootstraps pip from wheels vendored in the `python@3.12` keg -- the one
    # step of this install that depends on Homebrew-managed keg state rather
    # than on our own tarball. On a stock Homebrew macOS it exited 1 and took
    # the whole `brew install` down with it. Nothing here needs ensurepip:
    # pip is placed into the venv directly below, by the same Homebrew python
    # that is already a declared dependency.
    system python, "-m", "venv", "--without-pip", venv

    # Round 3 of #3753 (#3788). The bootstrap used to hand the *keg's* pip an
    # install to perform (`pip --python <venv-python> install --upgrade pip`).
    # On python@3.12 3.12.14 that dies:
    #
    #   File ".../pip/_internal/req/req_install.py", line 779, in install
    #     from pip._internal.operations.install.wheel import install_wheel
    #   File ".../pip/_internal/commands/install.py", line 97, in
    #       _prevent_import_hook
    #     raise ImportError(f"No module named {module!r}")
    #   ImportError: No module named 'pip._internal.operations.install.wheel'
    #
    # pip 26.2 pre-imports its lazily-imported modules just before writing
    # anything, so a distribution it is about to install cannot shadow them.
    # `_eagerly_import_modules()` swallows an ImportError there and records
    # the name in `_MISSING_MODULES`; the audit hook it then installs turns
    # the *real* import, moments later, into the ImportError above. So the
    # visible failure is pip's guard, but the fault it is reporting is that
    # this pip installation cannot import its own wheel installer -- which no
    # option, ordering or `--upgrade`-free spelling of an install through
    # that pip can route around.
    #
    # Stop asking the keg's pip to install anything. It only *downloads* a
    # pip wheel, a code path that never touches `operations.install.wheel`;
    # the venv is then populated by running pip out of that wheel under the
    # venv's own interpreter. A wheel is a zip whose root is the `pip`
    # package, so `python pip-X.whl/pip install pip-X.whl` is pip's own
    # documented bootstrap -- the same zipimport trick ensurepip uses, with
    # none of ensurepip and none of the keg's pip. Everything after this line
    # runs through the venv's own freshly-unpacked, complete pip.
    wheelhouse = buildpath/"pip-bootstrap"
    system python, "-m", "pip", "download", "--no-deps", "--only-binary", ":all:",
           "--disable-pip-version-check", "--dest", wheelhouse, "pip"
    pip_wheel = Dir.glob(wheelhouse/"pip-*.whl").first
    odie "keg venv bootstrap: `pip download` produced no pip wheel in #{wheelhouse}" if pip_wheel.nil?
    system venv/"bin/python", "#{pip_wheel}/pip", "install", "--no-index",
           "--disable-pip-version-check", pip_wheel
    system venv/"bin/pip", "install", buildpath

    # config_wizard builds its schema from example.config.ini at import time
    # (#3388), so `import nyxgpt.app` -- the wrapper, the `test` block, and the
    # always-on self-heal watchdog -- needs it present. A venv has no repo root
    # above the package, so drop the template next to the installed package
    # where _resolve_example_config_path() finds it with no env var (#3406).
    site_packages = venv/"lib/python3.12/site-packages/nyxgpt"
    cp buildpath/"example.config.ini", site_packages/"example.config.ini"

    # Expose the CLI itself, not just the API service wrapper (#3850). `pip
    # install` above creates the `nyxgpt` console script declared in
    # pyproject.toml, but it lands in the keg's venv, which is on nobody's
    # PATH -- so a keg that installed perfectly still answered
    # `zsh: command not found: nyxgpt`, and every documented operation
    # (`nyxgpt up`, `nyxgpt ops ...`) was unreachable on the artifact path.
    # A symlink rather than another bash wrapper: the console script's
    # shebang already names this keg's venv interpreter by absolute path, so
    # nothing needs re-deriving, and libexec is the one tree Homebrew's
    # Cleaner leaves alone -- the target keeps the 0755 pip gave it. The link
    # cannot drift from what was installed, which a hand-written wrapper can.
    bin.install_symlink venv/"bin/nyxgpt"

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

  # Homebrew generates its own post-install message for any formula carrying a
  # `service` block -- "To start ... now and restart at login: brew services
  # start ..." -- and with no `caveats` here that generated line was the only
  # instruction an operator ever received. The owner followed it and got a
  # stack with no Ollama, no Cassandra and no observability (#3854): `brew
  # services start` starts the one service it names and nothing else, so it
  # never reaches `ops.install()` -- which is what installs and starts Ollama
  # (`_ensure_native_ollama_service`), creates the `nyxgpt-cassandra` container
  # (`_ensure_cassandra_container`), brings the observability Compose profiles
  # up (they are opt-*out*, behind `--skip-observability`) and syncs the
  # packaged resources into ~/.nyxGPT (`_sync_packaged_resources`).
  #
  # The `service` block stays: it is what makes restart-at-login work, and what
  # `nyxgpt ops restart`/self-heal drive (`brew services restart nyxgpt-api`).
  # Removing it to silence the competing guidance would cost real behavior to
  # fix a text problem. What was actually missing is any statement of which
  # command comes first, so this block names `nyxgpt up` and says plainly what
  # the per-service command does not do.
  #
  # `#{name}` rather than a literal: the rc channel installs this same recipe
  # as `nyxgpt-api@<line>rc`, and naming the stable formula there would print a
  # command the operator does not have.
  #
  # Asserted on the real `brew install` output by macos-brew-smoke.yml's
  # "Prove the install tells the operator to run `nyxgpt up`" step.
  #
  # It also carries the teardown guidance (#3859), which arrived on v3.0.0 as a
  # second `caveats` block. Ruby keeps only the last definition of a method, so
  # two blocks in one formula is not two messages -- it is one silently winning
  # and the other never printed, with `ruby -c` clean either way. They are one
  # block for that reason: `brew uninstall` does not stop a formula's service
  # first and Homebrew has no uninstall hook that could, so it deletes the keg's
  # files out from under a running process that goes on serving from memory;
  # `brew untap` then makes the formula name unresolvable, leaving services the
  # operator cannot name to stop. nyxGPT's own `com.nyxgpt.*` LaunchAgents
  # Homebrew never knew about. Caveats are the one place the operator is
  # actually standing when they decide to remove nyxGPT.
  def caveats
    <<~EOS
      To start nyxGPT, run:
        nyxgpt up

      That brings up the whole stack -- this API, the web UI, Ollama, Cassandra
      and the observability services -- waits for it to report healthy, and
      prints the web UI URL. `nyxgpt down` stops it again, and `nyxgpt ops
      status` shows what is running.

      If you were pointed at `brew services start #{name}`, note
      that it starts only this one service. It does not install or start
      Ollama, it does not create the Cassandra container, and it does not
      start observability. A stack brought up that way reports
      `ollama: unreachable` and `cassandra: unreachable`, and the web UI
      fails to load sessions. Use it to control this individual service once
      `nyxgpt up` has set the stack up -- not instead of it.

      `nyxgpt up` starts the Homebrew services for you, so they still restart
      at login.

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

  test do
    # The venv and its uvicorn install have to exist -- the actual API is
    # exercised by integration tests in the repo.
    assert_predicate libexec/"venv/bin/python3", :exist?
    system libexec/"venv/bin/python3", "-c", "import nyxgpt.app"

    # And the product has to be operable, not merely importable (#3850). The
    # two assertions above are both true of a keg that ships no reachable CLI
    # at all, which is precisely the keg that shipped: `nyxgpt up` answered
    # `command not found` on the owner's Mac while this block stayed green.
    # So run the command the docs tell the operator to run, through `bin` --
    # a CLI that exists only inside the keg's venv fails here.
    assert_predicate bin/"nyxgpt", :exist?
    # `\d` and not the formula's own version: the tarball vendors pyproject.toml
    # as-is, so the package metadata the CLI reports is the project version,
    # which is deliberately not the candidate version stamped onto the formula.
    reported = shell_output("#{bin}/nyxgpt --version")
    assert_match(/\Anyxgpt \d/, reported)
    # 0.0.0 is nyxgpt.version's "could not be determined" sentinel: the CLI
    # ran, but could not read its own installed metadata, and there is no
    # checkout in a keg to fall back to.
    refute_match(/\Anyxgpt 0\.0\.0/, reported)
  end
end
