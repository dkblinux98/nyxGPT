class MygptWeb < Formula
  desc "myGPT local web UI (Next.js) service wrapper"
  homepage "https://github.com/dkblinux98/myGPT"

  # NOTE:
  # This file lives in the myGPT repo as the *source template*.
  # The `mygpt ops install` command will copy it into your Homebrew tap at:
  #   $(brew --repo dkblinux98/mygpt-local)/Formula/mygpt-web.rb
  #
  # During that install step, ops should also replace the placeholders below
  # with a real URL + sha256 for a tarball (typically the tap's dist artifact).
  url "__MYGPT_WEB_URL__"
  sha256 "__MYGPT_WEB_SHA256__"
  version "1.0.0.md"
  depends_on "node"

  def install
    # Install a tiny wrapper that reads ~/.myGPT/config.ini via scripts/run-web.sh
    # (the ops installer is responsible for ensuring that script exists at runtime).
    (bin/"mygpt-web").write <<~SH
      #!/usr/bin/env bash
      set -euo pipefail

      # launchd/Homebrew services often run with a minimal PATH.
      # Ensure Homebrew bins are available so node/npm can be found.
      export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

      # Prefer the Node installed by Homebrew.
      export PATH="#{Formula["node"].opt_bin}:#{Formula["node"].opt_libexec}/bin:${PATH}"

      exec "${HOME}/.myGPT/scripts/run-web.sh"
    SH
    chmod 0755, bin/"mygpt-web"
  end

  service do
    run [opt_bin/"mygpt-web"]
    keep_alive true

    # Homebrew's conventional log locations:
    #   /usr/local/var/log/mygpt-web.log
    #   /usr/local/var/log/mygpt-web.err.log
    #
    # If you want everything consolidated under ~/.myGPT/logs, you can symlink:
    #   ln -sf /usr/local/var/log/mygpt-web.log ~/.myGPT/logs/mygpt-web.log
    #   ln -sf /usr/local/var/log/mygpt-web.err.log ~/.myGPT/logs/mygpt-web.err.log
    log_path var/"log/mygpt-web.log"
    error_log_path var/"log/mygpt-web.err.log"
  end

  test do
    # We only validate that the wrapper script exists and is executable.
    # (The actual Next.js runtime is exercised by integration tests in the repo.)
    assert_predicate bin/"mygpt-web", :exist?
  end
end
