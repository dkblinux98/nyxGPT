#!/usr/bin/env python3
"""Prove the Linux bind posture round-trip with real Docker (#3509/#3721).

#3721 gave Prometheus a route to a loopback-bound native API (the
`host-api-relay` socat service). #3509's acceptance failure is the *other*
half: the reporter had already widened `[api] host` to `0.0.0.0` as a manual
workaround, and nothing ever migrated them back. The relay is deliberately
skipped while the bind is non-loopback, `_prometheus_api_scrape_issue` only
fires when the scrape is *down* (it is up on `0.0.0.0`), so every subsequent
`nyxgpt ops observability` silently re-affirmed an API published on every
interface -- exactly what P6-4 forbids.

Unit tests for this mock `_is_linux` and `_docker_bridge_gateway_ip`, so they
show the branching but never the thing that actually broke: a real container
on a real Linux engine with no route to the host's loopback. This script runs
that for real on the machine it executes on.

Four parts, in order:

  1. Reproduce the gap        A real loopback-only listener; a real container
                              resolving `host.docker.internal` cannot reach it.
  2. Prove the relay closes it  The real `alpine/socat` service from
                              docker-compose.yml, bound to the real bridge
                              gateway, makes the same request succeed -- from
                              the default bridge AND from a user-defined
                              network, which is what Compose actually uses.
  3. Fault injection          The pre-fix signal set (the scrape check plus
                              `_sync_host_relay_env`'s message) is asserted to
                              be SILENT about a widened bind. Without this half
                              the job would pass on a build that never got the
                              fix -- the #3753 lesson.
  4. Prove the fix catches it  `_insecure_api_bind_issue` fires on the widened
                              bind and clears once `[api] host` is reverted,
                              and the relay flips to the `monitoring` profile
                              at the same moment -- i.e. the remediation the
                              finding prints actually works.

Usage: python3 scripts/host-relay-posture-smoke.py
Exit 0 = every part held. Requires Docker and a Linux host; skips (exit 0)
elsewhere, since the failure mode does not exist off Linux.
"""

from __future__ import annotations

import http.server
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

API_PORT = 8000
RELAY_IMAGE = "alpine/socat:1.8.0.3"
CURL_IMAGE = "curlimages/curl:8.11.1"
RELAY_NAME = "nyxgpt-relay-posture-smoke"
NET_NAME = "nyxgpt-relay-posture-smoke-net"

MONITORING_SECTION = "\n[monitoring]\nenabled = true\nprometheus_ui_url = http://localhost:9090\n"


def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)  # type: ignore[call-overload,no-any-return]


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def container_can_reach_host_api(network: str | None = None) -> str:
    """HTTP status a container gets for host.docker.internal:API_PORT ('000' = refused)."""
    cmd = ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway"]
    if network:
        cmd += ["--network", network]
    cmd += [
        CURL_IMAGE,
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "--max-time",
        "5",
        f"http://host.docker.internal:{API_PORT}/metrics",
    ]
    out = run(cmd).stdout.strip()
    return out.splitlines()[-1] if out else "000"


def start_loopback_api() -> http.server.ThreadingHTTPServer:
    """Stand in for the natively-installed nyxgpt-api: loopback only, like uvicorn."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"nyxgpt_up 1\n")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def cleanup() -> None:
    run(["docker", "rm", "-f", RELAY_NAME])
    run(["docker", "network", "rm", NET_NAME])


def main() -> int:
    if platform.system() != "Linux":
        print(f"SKIP: the container->host-loopback gap does not exist on {platform.system()}")
        return 0
    if shutil.which("docker") is None or run(["docker", "info"]).returncode != 0:
        print("SKIP: Docker is not usable on this host")
        return 0

    from nyxgpt import ops

    gateway = ops._docker_bridge_gateway_ip()
    if gateway is None:
        print("SKIP: could not resolve the docker bridge gateway")
        return 0
    print(f"Real docker bridge gateway: {gateway}")

    cleanup()
    server = start_loopback_api()
    try:
        # --- Part 1: reproduce the #3721 gap against a real container ---
        before = container_can_reach_host_api()
        if before != "000":
            fail(
                f"expected a container to have NO route to the host loopback, got HTTP {before}. "
                "This host does not reproduce the gap, so the rest of this job would be vacuous."
            )
        print(
            f"[1] container -> host.docker.internal:{API_PORT} without the relay: {before} (gap reproduced)"
        )

        # --- Part 2: the real relay service closes it ---
        started = run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                RELAY_NAME,
                "--network",
                "host",
                RELAY_IMAGE,
                f"TCP-LISTEN:{API_PORT},fork,reuseaddr,bind={gateway}",
                f"TCP:127.0.0.1:{API_PORT}",
            ]
        )
        if started.returncode != 0:
            fail(f"could not start the relay container: {started.stderr.strip()}")
        time.sleep(3)

        for network in (None, NET_NAME):
            if network:
                run(["docker", "network", "create", network])
            got = container_can_reach_host_api(network)
            label = network or "default bridge"
            if got != "200":
                fail(f"relay is up but a container on the {label} got HTTP {got}, expected 200")
            print(f"[2] container -> host API through the relay ({label}): {got}")

        # --- Parts 3 & 4: the bind-posture round trip, real config, real gateway ---
        tmp = Path(tempfile.mkdtemp())
        cfg, env = tmp / "config.ini", tmp / ".env"

        cfg.write_text(f"[api]\nhost = 0.0.0.0\nport = {API_PORT}\n" + MONITORING_SECTION)
        env.write_text("NYXGPT_HOST_RELAY_PROFILE=disabled\n", encoding="utf-8")
        widened = ops._sync_host_relay_env(cfg_path=cfg, env_path=env)

        # Fault injection: the pre-fix signals must be silent here, or this job
        # would pass on a build that never grew the new check.
        if "0.0.0.0" in widened.message and "revert" in widened.message.lower():
            fail("pre-fix signal check is stale -- rewrite it against the real pre-fix behaviour")
        if ops._prometheus_api_scrape_issue(cfg) is not None:
            fail("the scrape check fired on a widened bind; it cannot be the signal for this")
        print("[3] pre-fix signals are silent on a widened bind (the gap this fix closes)")

        issue = ops._insecure_api_bind_issue(cfg)
        if issue is None:
            fail(
                "doctor did not flag `[api] host = 0.0.0.0` on a Linux host with a live relay path"
            )
        if "127.0.0.1" not in issue or "nyxgpt ops observability" not in issue:
            fail(f"the finding does not name the way back: {issue}")
        if "127.0.0.1" not in (widened.details or ""):
            fail("`ops observability` reported the relay disabled without the revert remediation")
        print(f"[4] doctor flags the widened bind: {issue[:88]}...")

        # Follow the remediation the finding just printed, and prove it works.
        cfg.write_text(f"[api]\nhost = 127.0.0.1\nport = {API_PORT}\n" + MONITORING_SECTION)
        reverted = ops._sync_host_relay_env(cfg_path=cfg, env_path=env)
        if ops._insecure_api_bind_issue(cfg) is not None:
            fail("the finding did not clear after reverting `[api] host` to loopback")
        if "NYXGPT_HOST_RELAY_PROFILE=monitoring" not in env.read_text(encoding="utf-8"):
            fail(f"the relay was not enabled after the revert: {reverted.message}")
        print("[4] after the documented revert: finding clears and the relay enables")
    finally:
        server.shutdown()
        cleanup()

    print(
        "\nOK: the gap reproduces, the relay closes it, and the posture finding fires and clears."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
