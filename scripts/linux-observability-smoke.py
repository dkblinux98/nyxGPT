#!/usr/bin/env python3
"""Executed proof that observability actually works on a plain Linux docker engine (#3721).

Two things break the Linux-native observability path in ways no unit test can
see, because both are properties of the *engine*, not of our code:

1. **Bind-mount ownership.** dockerd runs as root and creates a missing
   bind-mount source as ``root:root``. ``prom/prometheus`` then runs as uid
   65534, cannot write ``/prometheus``, panics on
   ``open /prometheus/queries.active: permission denied`` and crash-loops.
   Docker Desktop on macOS presents bind mounts as owned by whatever uid the
   container asks for, so this is invisible there.
2. **Container -> host loopback.** ``host.docker.internal:host-gateway``
   resolves to the docker bridge gateway (e.g. ``172.17.0.1``) on a plain
   Linux engine, and a natively-installed uvicorn bound to ``127.0.0.1`` does
   not listen there -- so prometheus cannot scrape the native API and every
   Grafana panel renders "No data". Docker Desktop proxies that name to the
   host's loopback, so this is invisible on macOS too.

Both were reported as one acceptance failure (#3721): the operator's only
escape hatch was ``[api] host = 0.0.0.0``, which publishes the API on every
interface and trips app.py's P6-1 bind-security gate -- the exact posture P6-4
requires us not to need.

The script therefore runs the monitoring profile twice against two separate
``$HOME``\\s, and asserts both halves (the fault-injection pattern from
``macos-brew-smoke.yml``):

* **injected** -- start the stack with the ownership reconcile skipped, exactly
  as ``nyxgpt ops observability`` did before #3721. Prometheus MUST fail to
  come up. If it comes up, this check has no teeth and the script fails loudly
  rather than passing vacuously.
* **fixed** -- start it through the real reconcile steps
  (``_ensure_observability_volume_dirs`` + ``_sync_host_relay_env``, both of
  which ``_reconcile_grafana_provisioning`` now performs). Prometheus MUST be
  running AND its ``nyxgpt-api`` target MUST report healthy -- i.e. it really
  scraped a loopback-bound API through the relay -- with no ``0.0.0.0:8000``
  listener anywhere on the host.

Linux-only by construction; it exits 0 with a skip notice elsewhere.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Ports are the compose defaults; the whole point is to exercise the shipped
# configuration rather than a bespoke one.
API_PORT = 8000
PROM_PORT = 9090

# Prometheus needs at least one completed scrape interval (15s in
# docker/prometheus.yml) before a target reports anything but "unknown".
TARGET_TIMEOUT = 90.0
# The injected half only has to prove prometheus does NOT stay up. It panics
# within a second of each start, so this only has to outlast a restart or two.
CRASH_TIMEOUT = 45.0


def log(msg: str) -> None:
    print(f"[linux-observability-smoke] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[linux-observability-smoke] FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


class _MetricsHandler(BaseHTTPRequestHandler):
    """Stand-in for the natively-installed API's /metrics endpoint.

    Deliberately minimal: the defects under test are the engine's bind-mount
    ownership and its container->host routing, neither of which can tell a
    uvicorn from a stdlib HTTP server. What matters is that it is bound to
    127.0.0.1 and nothing else, which is the condition that makes the relay
    necessary in the first place.
    """

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        body = b"# HELP nyxgpt_smoke_up 1\n# TYPE nyxgpt_smoke_up gauge\nnyxgpt_smoke_up 1\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # noqa: ARG002 - stdlib signature
        return


def start_native_api() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", API_PORT), _MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/metrics", timeout=5) as resp:
        if resp.status != 200:
            die(f"the stand-in native API answered {resp.status} on its own loopback")
    log(f"stand-in native API listening on 127.0.0.1:{API_PORT} (loopback only)")
    return server


def compose(
    home: Path, project: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run `docker compose` against `home`'s synced compose file.

    HOME is overridden per half because docker-compose.yml interpolates
    `${HOME}` into every bind-mount source -- that is what keeps the injected
    half's root-owned directory from contaminating the fixed half.
    """
    env = {**os.environ, "HOME": str(home), "COMPOSE_PROJECT_NAME": project}
    cmd = [
        "docker",
        "compose",
        "-f",
        str(home / ".nyxGPT" / "docker-compose.yml"),
        "--profile",
        "monitoring",
        *args,
    ]
    cp = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and cp.returncode != 0:
        die(f"{' '.join(cmd)} exited {cp.returncode}\n{cp.stdout}\n{cp.stderr}")
    return cp


def container_state(project: str, service: str) -> str:
    cp = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", f"{project}-{service}-1"],
        capture_output=True,
        text=True,
    )
    return cp.stdout.strip() if cp.returncode == 0 else "absent"


def sync_home(home: Path) -> None:
    """Populate `home`/.nyxGPT the way `nyxgpt ops install` does, with a
    loopback `[api] host` -- the configuration the relay exists to serve."""
    os.environ["HOME"] = str(home)
    (home / ".nyxGPT").mkdir(parents=True, exist_ok=True)
    (home / ".nyxGPT" / "config.ini").write_text(
        f"[api]\nhost = 127.0.0.1\nport = {API_PORT}\n", encoding="utf-8"
    )
    # Imported per-half AFTER $HOME is set: ops resolves ~/.nyxGPT at call
    # time, but self_heal.COMPOSE_FILE is a module-level constant.
    for mod in [m for m in list(sys.modules) if m.startswith("nyxgpt")]:
        del sys.modules[mod]
    import nyxgpt.ops as ops

    for result in ops._sync_packaged_resources():
        if not result.ok:
            die(f"could not sync packaged resources into {home}: {result.message}")
    return None


def wait_for_prometheus_target(timeout: float) -> tuple[str, str]:
    """Poll prometheus for the `nyxgpt-api` target, returning (health, lastError)."""
    deadline = time.monotonic() + timeout
    last = ("unreachable", "prometheus API never answered")
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PROM_PORT}/api/v1/targets", timeout=5
            ) as resp:
                payload = json.load(resp)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last = ("unreachable", f"{type(e).__name__}: {e}")
            time.sleep(3)
            continue
        for target in payload.get("data", {}).get("activeTargets", []):
            if target.get("labels", {}).get("job") == "nyxgpt-api":
                last = (target.get("health", "unknown"), target.get("lastError", ""))
                if last[0] == "up":
                    return last
        time.sleep(3)
    return last


def run_injected_half(root: Path) -> None:
    """Pre-#3721 behaviour: start the stack with no ownership reconcile."""
    home = root / "home-injected"
    project = "nyxsmokebroken"
    sync_home(home)

    volumes = home / ".nyxGPT" / "volumes"
    if (volumes / "prometheus").exists():
        die("the injected half must let dockerd create the bind-mount source itself")
    log("injected: starting prometheus with no ownership reconcile (dockerd creates /prometheus)")
    compose(home, project, "up", "-d", "prometheus")

    deadline = time.monotonic() + CRASH_TIMEOUT
    saw_failure = False
    while time.monotonic() < deadline:
        if container_state(project, "prometheus") in {"restarting", "exited", "dead"}:
            saw_failure = True
            break
        time.sleep(3)

    logs = subprocess.run(
        ["docker", "logs", f"{project}-prometheus-1"], capture_output=True, text=True
    )
    combined = logs.stdout + logs.stderr
    compose(home, project, "down", "-v", "--remove-orphans", check=False)

    if not saw_failure:
        die(
            "prometheus stayed up WITHOUT the ownership reconcile -- this check has no "
            "teeth on this runner, so a regression of #3721 would pass silently. "
            "Investigate before trusting the fixed half below."
        )
    if "permission denied" not in combined:
        die(
            "prometheus failed, but not with the permission error #3721 is about; "
            f"logs:\n{combined[-2000:]}"
        )
    owner = (volumes / "prometheus").stat().st_uid
    log(
        f"injected: reproduced -- {volumes / 'prometheus'} owned by uid {owner}, prometheus "
        "crash-looped on 'permission denied' (this is the pre-#3721 behaviour)"
    )


def run_fixed_half(root: Path) -> None:
    """Post-#3721 behaviour: the reconcile steps `_reconcile_grafana_provisioning`
    performs, then the same bring-up, then a real scrape of the native API."""
    home = root / "home-fixed"
    project = "nyxsmokefixed"
    sync_home(home)
    import nyxgpt.ops as ops

    for result in ops._ensure_observability_volume_dirs():
        log(f"fixed: volume dirs -- {'OK' if result.ok else 'FAIL'}: {result.message}")
        if not result.ok:
            die(f"could not reconcile a bind-mount directory: {result.details}")

    relay = ops._sync_host_relay_env()
    log(f"fixed: host relay -- {'OK' if relay.ok else 'FAIL'}: {relay.message}")
    if "enabled" not in relay.message:
        die(
            "the host-api-relay was not enabled on a Linux host with a loopback "
            f"[api] host -- prometheus will have no route to the native API: {relay.message}"
        )

    compose(home, project, "up", "-d", "prometheus", "host-api-relay")
    health, last_error = wait_for_prometheus_target(TARGET_TIMEOUT)

    state = container_state(project, "prometheus")
    listeners = subprocess.run(["ss", "-lnt"], capture_output=True, text=True).stdout
    logs = subprocess.run(
        ["docker", "logs", f"{project}-prometheus-1"], capture_output=True, text=True
    )
    compose(home, project, "down", "-v", "--remove-orphans", check=False)

    if state != "running":
        die(
            f"prometheus is {state} after the reconcile; logs:\n{(logs.stdout + logs.stderr)[-2000:]}"
        )
    if health != "up":
        die(
            f"prometheus reached the stack but its nyxgpt-api target is {health!r} "
            f"(lastError: {last_error!r}). The relay is not carrying "
            f"host.docker.internal:{API_PORT} to the host loopback.\n{listeners}"
        )

    # The whole reason the relay exists rather than `[api] host = 0.0.0.0`:
    # the API must stay unreachable from the LAN (P6-4). Only the Local
    # Address:Port column is inspected -- `ss`'s Peer column reads `0.0.0.0:*`
    # for every listening socket, including a loopback-only one.
    for line in listeners.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        if local.rsplit(":", 1)[-1] != str(API_PORT):
            continue
        if local.rsplit(":", 1)[0] in {"0.0.0.0", "*", "[::]", "::"}:
            die(f"something is listening on all interfaces for port {API_PORT}: {line.strip()}")
    log(
        "fixed: prometheus running, nyxgpt-api target UP, and no 0.0.0.0 listener on "
        f"port {API_PORT} -- the API stayed loopback-only"
    )


def main() -> int:
    if platform.system() != "Linux":
        log(f"skipping: {platform.system()} is not the platform these defects occur on")
        return 0
    if shutil.which("docker") is None:
        die("docker is required -- this smoke exists to exercise a real docker engine")

    root = Path(tempfile.mkdtemp(prefix="nyx-obs-smoke-"))
    real_home = os.environ.get("HOME", "")
    api = start_native_api()
    try:
        run_injected_half(root)
        run_fixed_half(root)
    finally:
        api.shutdown()
        os.environ["HOME"] = real_home
        # dockerd left root-owned directories behind in the injected half.
        # Remove them through a container so the script needs no sudo of its own.
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{root}:/scrub", "alpine:3.22", "rm", "-rf", "/scrub"],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(root, ignore_errors=True)

    log("PASS: the Linux observability path works with [api] host = 127.0.0.1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
