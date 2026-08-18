#!/usr/bin/env python3
"""Executed proof that an unqueryable Compose probe reports unknown, not absent (#3812).

Owner acceptance on the rc12 Terraform/cloud install saw the Self-Heal
Components panel report **"11 unhealthy"** with every observability component
``state=absent`` -- glitchtip x4, grafana, host-api-relay, jaeger, loki,
otel-collector, prometheus, promtail -- while all eleven containers were up and
healthy and Grafana was reachable in the browser. Nothing was down. The API
process simply could not reach the Docker daemon (its ``systemd --user``
session predated the ``ec2-user`` docker-group change, so every ``docker
compose ps`` exited 125), and ``compose_probe_available()`` -- the flag whose
entire job is to let the UI say "can't check from here" -- answered a *different
question*: "is ``docker`` on PATH and does the compose file exist?". Both were
true. So the flag said "available" over an empty survey, and every desired
service was synthesised as absent.

That is a defect no unit test can see end-to-end, because its whole substance
is what a real ``docker compose ps`` does against a real daemon this process is
not allowed to talk to. So this script runs both halves for real (the
fault-injection pattern from ``macos-brew-smoke.yml``):

* **injected** -- a genuine daemon-unreachable condition: ``DOCKER_HOST`` points
  at a real unix socket owned by root with mode 000, which is exactly the
  permission-denied failure the owner hit. The script first asserts the
  *pre-fix* predicate (docker on PATH + compose file present) is still True
  here -- i.e. the old check would have reported "available" -- and that the
  pre-fix code path really does render every desired service ``absent`` and
  count them unhealthy. Without that, this check could pass on a runner that
  never reproduced the bug. Then it asserts the shipped code reports the
  components ``unknown`` with the reason, zero unhealthy, and heals nothing.
* **restored** -- the same survey with the daemon reachable again, against a
  real container the script starts: the previously-unknown services must now
  carry *real* states (the started one running, the rest genuinely absent), so
  the honesty above is not bought by making every real absence unknown.

Linux-only by construction (it needs a plain Linux docker engine and root to
own the denied socket); it exits 0 with a skip notice elsewhere.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The service actually started in the restored half. Any monitoring-profile
# service would do; prometheus is the cheapest that stays up on its own once
# its bind-mount directory is reconciled.
STARTED_SERVICE = "prometheus"
PROJECT = "nyxprobehonesty"
START_TIMEOUT = 90.0


def log(msg: str) -> None:
    print(f"[self-heal-probe-honesty-smoke] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[self-heal-probe-honesty-smoke] FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def _reimport_nyxgpt():
    """Re-import nyxgpt after $HOME/$DOCKER_HOST changed.

    ``self_heal.COMPOSE_FILE`` is resolved at import time, so every half has to
    start from a clean module table rather than inheriting the previous one's
    path.
    """
    for mod in [m for m in list(sys.modules) if m.startswith("nyxgpt")]:
        del sys.modules[mod]
    import nyxgpt.self_heal as self_heal

    return self_heal


def sync_home(home: Path) -> None:
    """Populate `home`/.nyxGPT the way `nyxgpt ops install` does, monitoring on.

    The compose file has to be the shipped one and it has to really be there:
    "the file exists but the daemon is unreachable" is the precise condition
    #3588's existence checks could not distinguish from a healthy stack.
    """
    os.environ["HOME"] = str(home)
    (home / ".nyxGPT").mkdir(parents=True, exist_ok=True)
    (home / ".nyxGPT" / "config.ini").write_text(
        "[api]\nhost = 127.0.0.1\nport = 8000\n\n[monitoring]\nenabled = true\n",
        encoding="utf-8",
    )
    for mod in [m for m in list(sys.modules) if m.startswith("nyxgpt")]:
        del sys.modules[mod]
    import nyxgpt.ops as ops

    for result in ops._sync_packaged_resources():
        if not result.ok:
            die(f"could not sync packaged resources into {home}: {result.message}")


def make_denied_socket(path: Path) -> None:
    """A real AF_UNIX socket this process is genuinely not allowed to connect to.

    Root-owned, mode 000 -- the same "permission denied while trying to connect
    to the Docker daemon socket" the owner's `systemd --user` session hit,
    reproduced without touching the runner's own docker access.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    subprocess.run(["sudo", "chown", "root:root", str(path)], check=True)
    subprocess.run(["sudo", "chmod", "000", str(path)], check=True)
    if os.access(path, os.R_OK | os.W_OK):
        die(f"{path} is still accessible to this process -- the injection has no teeth")


def run_injected_half(root: Path) -> None:
    home = root / "home-injected"
    sync_home(home)
    denied = root / "denied" / "docker.sock"
    make_denied_socket(denied)
    os.environ["DOCKER_HOST"] = f"unix://{denied}"

    self_heal = _reimport_nyxgpt()
    import nyxgpt.ops as ops

    # 1. The condition really is the one that fooled the old check: docker on
    #    PATH, compose file present. If either were false here, the injection
    #    would be reproducing #3588's case instead of #3812's.
    if shutil.which("docker") is None or not self_heal.COMPOSE_FILE.exists():
        die(
            "the injected condition is not #3812's: docker on PATH="
            f"{shutil.which('docker')!r}, compose file exists="
            f"{self_heal.COMPOSE_FILE.exists()} at {self_heal.COMPOSE_FILE}"
        )
    log("injected: docker IS on PATH and the compose file IS present (the pre-fix check's inputs)")

    # 2. The survey genuinely cannot run.
    probe = self_heal.compose_probe()
    if probe.available:
        die(
            "`docker compose ps` succeeded against a root-owned mode-000 socket -- "
            "this runner does not reproduce the daemon-unreachable condition, so a "
            "regression of #3812 would pass here silently."
        )
    log(f"injected: reproduced -- probe unavailable, reason: {probe.reason}")

    desired = self_heal._desired_compose_services(self_heal._enabled_observability_profiles())
    if not desired:
        die("no observability services are desired -- the check would be vacuous")

    # 3. The pre-fix rendering, run for real: with the survey empty and the
    #    flag saying "available", every desired service was marked absent and
    #    counted unhealthy. This is the "11 unhealthy" the owner saw.
    pre_fix_rows = self_heal._absent_desired_statuses(set(), desired)
    pre_fix_unhealthy = self_heal._record_health_check(pre_fix_rows)
    if pre_fix_unhealthy != len(desired) or any(r.state != "absent" for r in pre_fix_rows):
        die("could not reproduce the pre-fix rendering; the injection proves nothing")
    log(
        f"injected: pre-fix behaviour reproduced -- {len(pre_fix_rows)} services rendered "
        f"'absent' and counted as {pre_fix_unhealthy} unhealthy, with nothing actually down"
    )

    # 4. The shipped behaviour: unknown, with the reason, counted apart.
    data = self_heal.status()
    compose_rows = [c for c in data["components"] if c["source"] == "compose"]
    absent = [c["service"] for c in compose_rows if c["state"] == "absent"]
    if absent:
        die(f"components still reported absent while the probe could not run: {absent}")
    if not compose_rows or any(c["known"] is not False for c in compose_rows):
        die(f"expected every compose row unknown, got: {compose_rows}")
    if data["unhealthy_count"] != 0:
        die(f"unknown components were counted as unhealthy: {data['unhealthy_count']}")
    if data["unknown_count"] != len(desired):
        die(f"expected {len(desired)} unknown, got {data['unknown_count']}")
    if data["compose_probe_available"] is not False or not data["compose_probe_reason"]:
        die(f"status() did not surface the probe failure: {data['compose_probe_reason']!r}")
    if "permission denied" not in data["compose_probe_reason"].lower():
        die(
            "the reason does not name the actual docker error, so an operator still "
            f"cannot act on it: {data['compose_probe_reason']!r}"
        )
    log(
        f"injected: shipped behaviour -- {data['unknown_count']} unknown, "
        f"{data['unhealthy_count']} unhealthy, reason on screen: {data['compose_probe_reason']}"
    )

    # 5. The same reason reaches the Infrastructure Status page.
    infra = ops.infra_status()
    if infra["compose_probe_available"] is not False or not infra["compose_probe_reason"]:
        die(f"infra_status() did not surface the probe failure: {infra}")

    # 6. Nothing is healed on a state nobody could read.
    healed = self_heal.heal_now()
    if healed["healed"]:
        die(f"self-heal acted on components it could not determine: {healed['healed']}")
    if len(healed["undetermined"]) != len(desired):
        die(f"heal pass did not report the undetermined components: {healed['undetermined']}")
    log("injected: heal pass acted on nothing and reported every component undetermined")


def compose(home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "COMPOSE_PROJECT_NAME": PROJECT}
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


def run_restored_half(root: Path) -> None:
    """The other half: with the daemon reachable, the survey reports real states."""
    home = root / "home-restored"
    os.environ.pop("DOCKER_HOST", None)
    # Set process-wide, not just for the bring-up: the probe under test shells
    # out to `docker compose ps` itself, and it has to look in the same project
    # the container was started in.
    os.environ["COMPOSE_PROJECT_NAME"] = PROJECT
    sync_home(home)

    self_heal = _reimport_nyxgpt()
    import nyxgpt.ops as ops

    for result in ops._ensure_observability_volume_dirs():
        if not result.ok:
            die(f"could not reconcile a bind-mount directory: {result.details}")

    compose(home, "up", "-d", STARTED_SERVICE)
    try:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            rows = [s for s in self_heal.compose_probe().statuses if s.service == STARTED_SERVICE]
            if rows and rows[0].state == "running":
                break
            time.sleep(3)

        probe = self_heal.compose_probe()
        if not probe.available or probe.reason:
            die(f"the probe reports unavailable with the daemon reachable: {probe.reason!r}")

        data = self_heal.status()
        by_service = {c["service"]: c for c in data["components"]}
        started = by_service.get(STARTED_SERVICE)
        if not started or started["known"] is not True or started["state"] != "running":
            die(f"a running container was not reported running: {started}")
        if data["unknown_count"] != 0:
            die(f"components were reported unknown with a reachable daemon: {data}")

        # And a genuinely torn-down service is still reported absent -- #3812's
        # fix must not launder every real absence into "unknown" (that would
        # hide #3356, the torn-down-profile case this panel exists for).
        genuinely_absent = [
            c["service"]
            for c in data["components"]
            if c["state"] == "absent" and c["known"] is True
        ]
        if not genuinely_absent:
            die(f"no desired-but-absent service was reported absent: {data['components']}")
        log(
            f"restored: {STARTED_SERVICE} reported running, and "
            f"{len(genuinely_absent)} genuinely-absent service(s) still reported absent"
        )
    finally:
        compose(home, "down", "-v", "--remove-orphans", check=False)


def main() -> None:
    if platform.system() != "Linux":
        log(f"skipping: needs a plain Linux docker engine (this is {platform.system()})")
        return
    if shutil.which("docker") is None:
        die("docker is not installed; this check cannot run")

    original_home = os.environ.get("HOME", "")
    original_docker_host = os.environ.get("DOCKER_HOST")
    # `ignore_cleanup_errors`: dockerd creates the bind-mount sources as
    # root:root, so the sudo sweep below -- not TemporaryDirectory -- is what
    # actually removes this tree.
    with tempfile.TemporaryDirectory(
        prefix="nyxgpt-probe-honesty-", ignore_cleanup_errors=True
    ) as tmp:
        root = Path(tmp)
        try:
            run_injected_half(root)
            run_restored_half(root)
        finally:
            os.environ["HOME"] = original_home
            os.environ.pop("COMPOSE_PROJECT_NAME", None)
            if original_docker_host is None:
                os.environ.pop("DOCKER_HOST", None)
            else:
                os.environ["DOCKER_HOST"] = original_docker_host
            # Root-owned by construction (the mode-000 socket) and by dockerd
            # (the bind-mount sources): TemporaryDirectory cannot remove either.
            subprocess.run(["sudo", "rm", "-rf", str(root)], check=False)
    log("PASS: an unqueryable probe reports unknown-with-reason; a reachable one reports reality")


if __name__ == "__main__":
    main()
