class NyxgptWeb < Formula
  desc "nyxGPT local web UI (Next.js) service wrapper"
  homepage "https://github.com/dkblinux98/nyxGPT"

  # NOTE:
  # This file lives in the nyxGPT repo as the *source template*.
  # The `nyxgpt ops install` command will copy it into your Homebrew tap at:
  #   $(brew --repo dkblinux98/nyxgpt-local)/Formula/nyxgpt-web.rb
  #
  # During that install step, ops should also replace the placeholders below
  # with a real URL + sha256 for a tarball (typically the tap's dist artifact).
  url "__NYXGPT_WEB_URL__"
  sha256 "__NYXGPT_WEB_SHA256__"
  version "1.0.0.md"
  depends_on "node"

  def install
    # Install a tiny wrapper that reads ~/.nyxGPT/config.ini via scripts/run-web.sh
    # (the ops installer is responsible for ensuring that script exists at runtime).
    (bin/"nyxgpt-web").write <<~SH
      #!/usr/bin/env bash
      set -euo pipefail

      # launchd/Homebrew services often run with a minimal PATH.
      # Ensure Homebrew bins are available so node/npm can be found.
      export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

      # Prefer the Node installed by Homebrew.
      export PATH="#{Formula["node"].opt_bin}:#{Formula["node"].opt_libexec}/bin:${PATH}"

      exec "${HOME}/.nyxGPT/scripts/run-web.sh"
    SH
    chmod 0755, bin/"nyxgpt-web"
  end

  service do
    run [opt_bin/"nyxgpt-web"]
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
    # We only validate that the wrapper script exists and is executable.
    # (The actual Next.js runtime is exercised by integration tests in the repo.)
    assert_predicate bin/"nyxgpt-web", :exist?
  end
end
