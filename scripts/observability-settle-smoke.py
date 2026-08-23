#!/usr/bin/env python3
"""Executed proof that the observability step fails on a crash-looping container (#3993).

Owner acceptance of the cloud ``--dev`` path saw ``nyxgpt ops install`` print

    [OK] Observability stack up: Grafana http://localhost:3001, ...

seconds before the *next* step printed ``[FAIL] Could not reconcile Grafana
admin credential``. Grafana was in a permanent crash loop the whole time
(``Error: failed to load provisioning file``, an AppleDouble ``._`` file it
could not parse), and the only fault the operator was shown was a credential
problem Grafana did not have. The step's entire evidence for "up" was ``docker
compose up -d`` exiting 0 -- which is a statement about Docker having *created*
the containers, not about anything inside them surviving its own boot. Three
SSH debugging sessions later the real cause was one ``docker logs`` line away.

No unit test can see this end to end: its whole substance is what a real
``docker compose ps`` reports about a real container that a real Docker engine
keeps restarting. So this script runs it for real, in three halves (the
fault-injection pattern from ``macos-brew-smoke.yml`` and
``self-heal-probe-honesty-smoke.py``):

* **crash-loop (injected)** -- a compose fixture whose ``grafana`` service exits
  1 immediately with ``restart: always``, plus a healthy ``jaeger`` alongside
  it. The script first asserts the *pre-fix* evidence is still satisfied here
  -- ``docker compose up -d`` really does exit 0 over this fixture, which is
  the sole input the old code turned into "Observability stack up" -- and only
  then asserts the shipped step returns ``ok=False``, names ``grafana``, and
  quotes ``grafana``'s own last log line. Without the first assertion this
  check could pass on a runner that never reproduced the defect.
* **healthy** -- the same fixture with both services staying up: the step must
  return ``ok=True`` with "Observability stack up". A check that fails on
  everything is not a check, and this is what keeps the crash-loop half from
  passing vacuously.
* **undetermined** -- ``DOCKER_HOST`` pointed at a socket that does not exist,
  so the Compose probe genuinely cannot run. The verdict must be
  ``undetermined``, never ``settled`` (that is #3993's lie) and never
  ``crashed`` (that is #3812's: reporting an unqueryable probe as an outage).

Needs a working Docker engine; exits 0 with a skip notice where there is none.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The crash-looping service is named `grafana` deliberately: it is the service
# that actually did this in #3993, and the assertions read as the operator's
# own report. The images are alpine, not the real Grafana -- the defect is
# about what the *step* concludes from a restarting container, not about
# Grafana's boot sequence.
CRASH_LINE = "Grafana provisioning error: failed to load provisioning file ._datasources.yml"

CRASH_FIXTURE = f"""services:
  grafana:
    image: alpine:3.20
    profiles: ["monitoring"]
    restart: always
    command: ["sh", "-c", "echo '{CRASH_LINE}'; exit 1"]
  jaeger:
    image: alpine:3.20
    profiles: ["tracing"]
    restart: "no"
    command: ["sh", "-c", "sleep 900"]
"""

HEALTHY_FIXTURE = """services:
  grafana:
    image: alpine:3.20
    profiles: ["monitoring"]
    restart: "no"
    command: ["sh", "-c", "sleep 900"]
  jaeger:
    image: alpine:3.20
    profiles: ["tracing"]
    restart: "no"
    command: ["sh", "-c", "sleep 900"]
"""


# The Docker CLI finds its `compose` plugin under `$HOME/.docker/cli-plugins`
# on a developer Mac (it is system-wide on the CI runners), and every half of
# this script moves $HOME. Point `DOCKER_CONFIG` at the real one before that
# happens, so `docker compose` keeps resolving where a human runs this.
_REAL_DOCKER_CONFIG = Path(os.path.expanduser("~")) / ".docker"


def log(msg: str) -> None:
    print(f"[observability-settle-smoke] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[observability-settle-smoke] FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def _reimport_ops():
    """Re-import nyxgpt after $HOME/$NYXGPT_COMPOSE_FILE/$DOCKER_HOST changed.

    `self_heal.COMPOSE_FILE` is resolved at import time, so each half has to
    start from a clean module table rather than inheriting the previous one's
    path.
    """
    for mod in [m for m in list(sys.modules) if m.startswith("nyxgpt")]:
        del sys.modules[mod]
    import nyxgpt.ops as ops

    return ops


def _write_half(root: Path, name: str, fixture: str) -> Path:
    """Lay out one half's $HOME and compose fixture; return the fixture path."""
    home = root / name
    (home / ".nyxGPT").mkdir(parents=True, exist_ok=True)
    # A config the loader accepts, so its validation noise does not drown the
    # step output this script is asserting on. No observability flags: the
    # crash half checks they are still off afterwards.
    (home / ".nyxGPT" / "config.ini").write_text(
        "[nyxgpt]\nname = nyxgpt\n\n[ollama]\nhost = http://127.0.0.1:11434\n\n"
        "[api]\nhost = 127.0.0.1\nport = 8000\n",
        encoding="utf-8",
    )
    compose_file = home / ".nyxGPT" / "docker-compose.yml"
    compose_file.write_text(fixture, encoding="utf-8")
    if _REAL_DOCKER_CONFIG.is_dir():
        os.environ.setdefault("DOCKER_CONFIG", str(_REAL_DOCKER_CONFIG))
    os.environ["HOME"] = str(home)
    os.environ["NYXGPT_COMPOSE_FILE"] = str(compose_file)
    os.environ.pop("DOCKER_HOST", None)
    return compose_file


def _compose_down(compose_file: Path) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "down", "-v", "--remove-orphans"],
        capture_output=True,
        text=True,
        check=False,
    )


def run_crash_loop_half(root: Path) -> None:
    compose_file = _write_half(root, "home-crash", CRASH_FIXTURE)
    ops = _reimport_ops()
    try:
        # 1. The pre-fix evidence, executed: `docker compose up -d` over this
        #    exact fixture exits 0. That exit code, and nothing else, is what
        #    the old step turned into "[OK] Observability stack up".
        up = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--profile",
                "monitoring",
                "--profile",
                "tracing",
                "up",
                "-d",
                "grafana",
                "jaeger",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if up.returncode != 0:
            die(
                "`docker compose up -d` did not even exit 0 over the crash fixture "
                f"({up.returncode}: {up.stderr.strip()}) -- this runner does not "
                "reproduce the pre-fix condition, so a regression would pass here."
            )
        log(
            "injected: `docker compose up -d` exited 0 over a container that cannot "
            "stay up -- the pre-fix step's sole evidence for 'Observability stack up'"
        )

        # 2. And the container really is crash-looping, not merely slow.
        verdict = ops._observability_settle_verdict(["grafana", "jaeger"])
        if verdict.state != ops.SETTLE_STATE_CRASHED:
            die(
                f"the injected crash loop was not observed (verdict={verdict.state!r}, "
                f"detail={verdict.detail!r}); docker ps -a follows in diagnostics"
            )
        if "grafana" not in verdict.services or "jaeger" in verdict.services:
            die(f"the verdict named the wrong containers: {verdict.services}")
        if CRASH_LINE not in verdict.detail:
            die(
                "the verdict does not carry the container's own reason, so the "
                f"operator still has to go find it: {verdict.detail!r}"
            )
        log(f"injected: verdict={verdict.state} services={verdict.services}")
        log(f"injected: reason carried through -- {verdict.detail}")

        # 3. The shipped step, run end to end (its own `up -d`, its own settle
        #    check): a FAIL naming the container and quoting its last line.
        _compose_down(compose_file)
        ops = _reimport_ops()
        results = ops._start_observability_stack()
        if len(results) != 1 or results[0].ok is not False:
            die(f"the step did not fail on a crash-looping container: {results}")
        if "did not stay up" not in results[0].message or "grafana" not in results[0].message:
            die(f"the failure does not name the container: {results[0].message!r}")
        if CRASH_LINE not in results[0].details:
            die(f"the failure does not quote the container's own line: {results[0].details!r}")
        log(f"injected: step reported -- [FAIL] {results[0].message}")
        log(f"injected: detail -- {results[0].details}")

        # 4. And the config flags that advertise the stack as live stayed off.
        cfg = (Path(os.environ["HOME"]) / ".nyxGPT" / "config.ini").read_text(encoding="utf-8")
        if "enabled = true" in cfg:
            die(f"a stack that never came up was advertised as enabled:\n{cfg}")
        log("injected: monitoring/logging/tracing flags were not flipped on")
    finally:
        _compose_down(compose_file)


def run_healthy_half(root: Path) -> None:
    compose_file = _write_half(root, "home-healthy", HEALTHY_FIXTURE)
    ops = _reimport_ops()
    try:
        results = ops._start_observability_stack()
        if len(results) != 1 or results[0].ok is not True:
            die(f"the step failed on a stack that is genuinely up: {results}")
        if "Observability stack up" not in results[0].message:
            die(f"a settled stack was not reported up: {results[0].message!r}")
        log(f"healthy: step reported -- [OK] {results[0].message}")
    finally:
        _compose_down(compose_file)


def run_undetermined_half(root: Path) -> None:
    compose_file = _write_half(root, "home-unknown", HEALTHY_FIXTURE)
    os.environ["DOCKER_HOST"] = f"unix://{root / 'no-such-docker.sock'}"
    ops = _reimport_ops()
    verdict = ops._observability_settle_verdict(["grafana", "jaeger"])
    if verdict.state != ops.SETTLE_STATE_UNDETERMINED:
        die(
            "a probe that could not run was reported as "
            f"{verdict.state!r} -- 'cannot determine' is neither of the other two "
            f"answers (detail={verdict.detail!r})"
        )
    if not verdict.detail:
        die("the undetermined verdict carries no reason, so the operator cannot act on it")
    log(f"undetermined: verdict={verdict.state}, reason: {verdict.detail}")
    os.environ.pop("DOCKER_HOST", None)
    _compose_down(compose_file)


def main() -> None:
    if shutil.which("docker") is None:
        log("SKIP: no docker on this host")
        return
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        log(f"SKIP: docker engine not usable here ({probe.stderr.strip()[:120]})")
        return

    with tempfile.TemporaryDirectory(prefix="nyx-settle-smoke-") as tmp:
        root = Path(tmp)
        run_crash_loop_half(root)
        run_healthy_half(root)
        run_undetermined_half(root)
    log(
        "PASS: a crash loop fails the step with its own reason; a live stack passes; "
        "an unqueryable probe says so"
    )


if __name__ == "__main__":
    main()
